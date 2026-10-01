"""Interactive chess analysis server.

Serves the board page and runs Stockfish on any position the page sends.
Run with:  python server.py   then open http://localhost:5050
"""
import atexit
import json
import os
import time
import sqlite3
import threading

import chess
import chess.engine
import chess.pgn
from flask import Flask, Response, jsonify, request, send_from_directory

import insights
import update
import database
import openings

DB_PATH = "data/chess.db"
ENGINE_PATH = "stockfish"
ANALYSIS_TIME = 1.0   # seconds for the one-shot /api/analyze endpoint
NUM_LINES = 3         # how many top lines to show
# Live board: keep searching deeper until one of these limits, then stop to save battery
LIVE_MAX_DEPTH = 26
LIVE_MAX_SECONDS = 15
# Give the live engine most of the Mac's cores (leave 2 for everything else)
ENGINE_THREADS = max(1, (os.cpu_count() or 4) - 2)
ENGINE_HASH_MB = 256

app = Flask(__name__, static_folder="static")
# Stockfish starts the first time you analyze something and shuts down after
# ENGINE_IDLE_SECONDS without use, so the server costs almost nothing while idle.
ENGINE_IDLE_SECONDS = 600
engine_lock = threading.Lock()  # one engine, so one analysis at a time
_engine = None
_engine_last_used = 0.0


def get_engine():
    """The running engine, started if needed. Call while holding engine_lock."""
    global _engine, _engine_last_used
    if _engine is not None and _engine.protocol.returncode.done():
        _engine = None  # Stockfish crashed or was killed; start a fresh one
    if _engine is None:
        _engine = chess.engine.SimpleEngine.popen_uci(ENGINE_PATH)
        _engine.configure({"Threads": ENGINE_THREADS, "Hash": ENGINE_HASH_MB})
        print("Engine started", flush=True)
    _engine_last_used = time.monotonic()
    return _engine


def engine_done():
    """Call when a search ends (still holding engine_lock)."""
    global _engine_last_used
    _engine_last_used = time.monotonic()


def _quit_engine():
    global _engine
    if _engine is not None:
        try:
            _engine.quit()
        except Exception:
            pass
        _engine = None
        print("Engine stopped (idle)", flush=True)


def _idle_watcher():
    while True:
        time.sleep(min(30, ENGINE_IDLE_SECONDS / 4))
        if _engine is None or time.monotonic() - _engine_last_used < ENGINE_IDLE_SECONDS:
            continue
        if engine_lock.acquire(blocking=False):  # never stop it in the middle of a search
            try:
                if time.monotonic() - _engine_last_used >= ENGINE_IDLE_SECONDS:
                    _quit_engine()
            finally:
                engine_lock.release()


threading.Thread(target=_idle_watcher, daemon=True).start()
atexit.register(_quit_engine)


def db():
    return sqlite3.connect(DB_PATH, timeout=30)


def with_db(fn, *args):
    conn = db()
    try:
        return fn(conn, *args)
    finally:
        conn.close()


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/games")
def list_games():
    return jsonify(with_db(insights.list_games, request.args.get("analyzed", "1") == "1"))


@app.route("/api/game/<int:game_id>")
def get_game(game_id):
    payload = with_db(insights.game_payload, game_id)
    if payload is None:
        return jsonify({"error": "game not found"}), 404
    return jsonify(payload)


@app.route("/api/position-stats")
def position_stats():
    """Every move played in this exact position across your games."""
    try:
        return jsonify(with_db(insights.position_stats, request.args.get("fen", "")))
    except ValueError:
        return jsonify({"error": "invalid position"}), 400


# ---------- Openings ----------
@app.route("/api/openings/names")
def opening_names():
    """Position -> opening name, so the board can name any position instantly."""
    _, _, names = openings.load()
    return jsonify(names)


@app.route("/api/openings/families")
def opening_families():
    return jsonify(with_db(insights.opening_families))


@app.route("/api/openings/family")
def opening_family():
    data = with_db(insights.opening_family, request.args.get("name", ""))
    if data is None:
        return jsonify({"error": "unknown opening"}), 404
    return jsonify(data)


@app.route("/api/openings/search")
def opening_search():
    """Lines whose name contains every word you typed."""
    return jsonify(with_db(insights.opening_search, request.args.get("q", "")))


# ---------- Live streaming analysis ----------
# The browser opens /api/analyze/stream for each position and receives the engine's
# lines as they get deeper. Opening a new stream stops the previous search at once.
live = {"generation": 0, "analysis": None}
live_lock = threading.Lock()


def line_json(board, info):
    score = info["score"].white()
    pv = info["pv"][:10]
    return {"cp": score.score(), "mate": score.mate(), "depth": info.get("depth"),
            "first_uci": pv[0].uci(), "san": board.variation_san(pv)}


@app.route("/api/analyze/stream")
def analyze_stream():
    try:
        board = chess.Board(request.args.get("fen", ""))
    except ValueError:
        return jsonify({"error": "invalid position"}), 400

    # tell any running search to stop, and take a ticket for this one
    with live_lock:
        live["generation"] += 1
        my_gen = live["generation"]
        if live["analysis"] is not None:
            live["analysis"].stop()

    def events():
        if board.is_game_over():
            yield f"data: {json.dumps({'game_over': board.result()})}\n\n"
            return
        with engine_lock:  # wait for the previous search to wind down (milliseconds)
            if my_gen != live["generation"]:
                return  # you already moved on to another position
            analysis = get_engine().analysis(
                board, chess.engine.Limit(depth=LIVE_MAX_DEPTH, time=LIVE_MAX_SECONDS),
                multipv=NUM_LINES)
            with live_lock:
                live["analysis"] = analysis
            lines, last_sent, last_depth = {}, 0.0, 0
            try:
                for info in analysis:
                    if my_gen != live["generation"]:
                        break
                    if "pv" not in info or "score" not in info or not info["pv"]:
                        continue
                    lines[info.get("multipv", 1)] = line_json(board, info)
                    depth = info.get("depth", 0)
                    now = time.monotonic()
                    # send right away for the first lines and each new depth, otherwise at most ~8x a second
                    if 1 in lines and (last_sent == 0 or depth > last_depth or now - last_sent > 0.12):
                        payload = [lines[k] for k in sorted(lines)]
                        yield f"data: {json.dumps({'lines': payload, 'depth': lines[1]['depth']})}\n\n"
                        last_sent, last_depth = now, depth
                if lines and my_gen == live["generation"]:
                    payload = [lines[k] for k in sorted(lines)]
                    yield f"data: {json.dumps({'lines': payload, 'depth': lines[1]['depth'], 'done': True})}\n\n"
            finally:
                analysis.stop()
                engine_done()
                with live_lock:
                    if live["analysis"] is analysis:
                        live["analysis"] = None

    return Response(events(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/analyze", methods=["POST"])
def analyze():
    fen = (request.get_json(silent=True) or {}).get("fen", "")
    try:
        board = chess.Board(fen)
    except ValueError:
        return jsonify({"error": "invalid position"}), 400

    if board.is_game_over():
        return jsonify({"lines": [], "game_over": board.result()})

    with engine_lock:
        infos = get_engine().analyse(board, chess.engine.Limit(time=ANALYSIS_TIME),
                                     multipv=NUM_LINES)
        engine_done()

    lines = []
    for info in infos:
        if not info.get("pv"):
            continue
        score = info["score"].white()
        pv = info["pv"][:10]
        lines.append({"cp": score.score(), "mate": score.mate(),
                      "first_uci": pv[0].uci(),
                      "san": board.variation_san(pv)})
    return jsonify({"lines": lines})


# ---------- Syncing new games from chess.com ----------
sync_state = {"running": False, "message": ""}
sync_state_lock = threading.Lock()


def background_analysis():
    def log(msg):
        sync_state["message"] = msg
    try:
        update.analyze_recent(threads=1, log=log)
    except update.Busy:
        log("Analysis is already running in another window")
    except Exception as e:
        log(f"Analysis failed: {e}")
    finally:
        sync_state["running"] = False


@app.route("/api/sync", methods=["POST"])
def sync_games():
    try:
        new_ids = update.pull_new_games()
    except update.Busy:
        return jsonify({"new": 0, "busy": True})
    except update.requests.RequestException:
        return jsonify({"error": "Could not reach chess.com. Check your internet connection."}), 502
    except Exception as e:
        return jsonify({"error": f"Sync failed: {e}"}), 500

    with sync_state_lock:
        if not sync_state["running"]:
            sync_state.update(running=True, message="Checking for games to analyze...")
            threading.Thread(target=background_analysis, daemon=True).start()
    return jsonify({"new": len(new_ids)})


@app.route("/api/sync/status")
def sync_status():
    return jsonify(sync_state)


if __name__ == "__main__":
    # one-time upgrade of older databases (adds the position lookup column)
    _conn = sqlite3.connect(DB_PATH, timeout=60)
    print("Checking database...")
    database.create_tables(_conn)
    _conn.close()
    # read the opening names in the background (takes a couple of seconds)
    threading.Thread(target=openings.load, daemon=True).start()
    # Port 5050 because macOS uses 5000 for AirPlay
    app.run(port=5050, threaded=True)
