"""Interactive chess analysis server.

Serves the board page and runs Stockfish on any position the page sends.
Run with:  python server.py   then open http://localhost:5050
"""
import atexit
import json
import os
import time
import io
import math
import sqlite3
import threading

import chess
import chess.engine
import chess.pgn
from flask import Flask, Response, jsonify, request, send_from_directory

from blunders import win_pct, classify, CAP
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
engine = chess.engine.SimpleEngine.popen_uci(ENGINE_PATH)
engine.configure({"Threads": ENGINE_THREADS, "Hash": ENGINE_HASH_MB})
engine_lock = threading.Lock()  # one engine, so one analysis at a time
atexit.register(engine.quit)


def query(sql, params=()):
    conn = sqlite3.connect(DB_PATH)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def clamp(cp):
    return max(-CAP, min(CAP, cp))


def move_label(drop, played, best):
    """chess.com-style label for one move. drop = % win chance lost by the mover."""
    if played == best:
        return "Best"
    if drop < 2:
        return "Excellent"
    if drop < 10:
        return "Good"
    return classify(drop).capitalize()  # Inaccuracy / Mistake / Blunder


def move_accuracy(drop):
    """Lichess per-move accuracy: 100 for a perfect move, falling as win chance is lost."""
    return max(0.0, min(100.0, 103.1668 * math.exp(-0.04354 * drop) - 3.1669))


def review_moves(rows):
    """One review per move (or None if that move hasn't been analyzed)."""
    reviews = []
    for k, (ply, played, best, ev) in enumerate(rows):
        if ev is None or best is None:
            reviews.append(None)
            continue
        ev_after = rows[k + 1][3] if k + 1 < len(rows) else None
        if ev_after is None:
            # last move of the game: no position after it to compare with
            reviews.append({"label": "Best" if played == best else None,
                            "drop": 0, "played": played, "best": best,
                            "accuracy": None})
            continue
        sign = 1 if ply % 2 == 1 else -1  # who made this move: White on odd plies
        drop = max(0.0, sign * (win_pct(clamp(ev)) - win_pct(clamp(ev_after))))
        reviews.append({"label": move_label(drop, played, best),
                        "drop": round(drop), "played": played, "best": best,
                        "accuracy": move_accuracy(drop)})
    return reviews


def summarize(reviews):
    """Counts of each label and average accuracy, for White and Black."""
    summary = {}
    for color, parity in (("white", 0), ("black", 1)):
        mine = [r for i, r in enumerate(reviews) if r and i % 2 == parity]
        counts = {}
        for r in mine:
            if r["label"]:
                counts[r["label"]] = counts.get(r["label"], 0) + 1
        accs = [r["accuracy"] for r in mine if r["accuracy"] is not None]
        summary[color] = {"counts": counts,
                          "accuracy": round(sum(accs) / len(accs), 1) if accs else None}
    return summary


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/games")
def list_games():
    sql = "SELECT id, date, white, black, my_color, result FROM games"
    if request.args.get("analyzed", "1") == "1":
        sql += " WHERE analyzed = 1"
    sql += " ORDER BY date DESC, id DESC LIMIT 500"
    games = []
    for gid, date, white, black, color, result in query(sql):
        opponent = black if color == "white" else white
        games.append({"id": gid, "label": f"{date} vs {opponent} ({color}, {result})"})
    return jsonify(games)


@app.route("/api/game/<int:game_id>")
def get_game(game_id):
    found = query("SELECT date, white, black, my_color, result, opening, pgn "
                  "FROM games WHERE id = ?", (game_id,))
    if not found:
        return jsonify({"error": "game not found"}), 404
    date, white, black, my_color, result, opening, pgn = found[0]
    rows = query("SELECT ply, move_played, best_move, eval_cp FROM positions "
                 "WHERE game_id = ? ORDER BY ply", (game_id,))

    game = chess.pgn.read_game(io.StringIO(pgn))
    board = game.board()
    start_fen = board.fen()
    moves = []
    for move in game.mainline_moves():
        san = board.san(move)
        board.push(move)
        moves.append({"san": san,
                      "from": chess.square_name(move.from_square),
                      "to": chess.square_name(move.to_square),
                      "fen": board.fen()})

    reviews = review_moves(rows)
    return jsonify({"date": date, "white": white, "black": black,
                    "my_color": my_color, "result": result, "opening": opening,
                    "start_fen": start_fen, "moves": moves,
                    "reviews": reviews, "summary": summarize(reviews)})


def result_for_me(result, my_color):
    if result == "1/2-1/2":
        return "draw"
    white_won = result == "1-0"
    return "win" if white_won == (my_color == "white") else "loss"


def verdict(avg_drop, all_best):
    if all_best:
        return "Best"
    if avg_drop < 2:
        return "Excellent"
    if avg_drop < 10:
        return "Good"
    return classify(avg_drop).capitalize()


@app.route("/api/position-stats")
def position_stats():
    """Every move played in this exact position across your games."""
    try:
        board = chess.Board(request.args.get("fen", ""))
    except ValueError:
        return jsonify({"error": "invalid position"}), 400
    key = database.position_key(board.fen())
    to_move = "white" if board.turn == chess.WHITE else "black"

    rows = query("""
        SELECT p.move_played, p.best_move, p.eval_cp, nxt.eval_cp,
               g.my_color, g.result, g.date
        FROM positions p
        JOIN games g ON g.id = p.game_id
        LEFT JOIN positions nxt ON nxt.game_id = p.game_id AND nxt.ply = p.ply + 1
        WHERE p.pos_key = ?""", (key,))

    groups = {"mine": {}, "opponents": {}}
    for move, best, ev, ev_after, my_color, result, date in rows:
        side = "mine" if my_color == to_move else "opponents"
        m = groups[side].setdefault(move, {"move": move, "games": 0, "win": 0, "draw": 0,
                                           "loss": 0, "last": "", "drops": [], "best": []})
        m["games"] += 1
        m[result_for_me(result, my_color)] += 1
        m["last"] = max(m["last"], date or "")
        if ev is not None and best is not None:
            m["best"].append(move == best)
            if ev_after is not None:
                sign = 1 if to_move == "white" else -1
                m["drops"].append(max(0.0, sign * (win_pct(clamp(ev)) - win_pct(clamp(ev_after)))))

    out = {}
    for side, moves in groups.items():
        lst = []
        for m in moves.values():
            analyzed = len(m["best"])
            m["verdict"] = None
            if analyzed:
                avg = sum(m["drops"]) / len(m["drops"]) if m["drops"] else 0
                m["verdict"] = verdict(avg, all(m["best"]))
            m["analyzed"] = analyzed
            # convert to UCI so the board can play it when you click the row
            try:
                m["uci"] = board.parse_san(m["move"]).uci()
            except ValueError:
                m["uci"] = None
            del m["drops"], m["best"]
            lst.append(m)
        lst.sort(key=lambda m: (-m["games"], m["move"]))
        out[side] = lst
    return jsonify({"to_move": to_move, **out})


# ---------- Openings ----------
def my_stats_for_keys(keys):
    """Your games reaching each position: {key: {games, win, draw, loss}}."""
    stats = {}
    keys = list(set(keys))
    for i in range(0, len(keys), 500):  # SQLite limits how many ? one query can have
        chunk = keys[i:i + 500]
        rows = query(f"""
            SELECT p.pos_key, g.my_color, g.result, COUNT(DISTINCT g.id)
            FROM positions p JOIN games g ON g.id = p.game_id
            WHERE p.pos_key IN ({",".join("?" * len(chunk))})
            GROUP BY p.pos_key, g.my_color, g.result""", chunk)
        for key, my_color, result, n in rows:
            s = stats.setdefault(key, {"games": 0, "win": 0, "draw": 0, "loss": 0})
            s["games"] += n
            s[result_for_me(result, my_color)] += n
    return stats


@app.route("/api/openings/names")
def opening_names():
    """Position -> opening name, so the board can name any position instantly."""
    _, _, names = openings.load()
    return jsonify(names)


@app.route("/api/openings/families")
def opening_families():
    ops, by_family, _ = openings.load()
    roots = {}
    for fam, members in by_family.items():
        main = next((o for o in members if not o["variation"]), None) or members[0]
        roots[fam] = main
    stats = my_stats_for_keys([o["key"] for o in roots.values()])
    out = []
    for fam, root in roots.items():
        members = by_family[fam]
        ecos = sorted({o["eco"] for o in members})
        out.append({"family": fam, "variations": len(members),
                    "eco": ecos[0] if len(ecos) == 1 else f"{ecos[0]}-{ecos[-1]}",
                    "moves": " ".join(san_with_numbers(root["san"])),
                    "mine": stats.get(root["key"], {"games": 0, "win": 0, "draw": 0, "loss": 0})})
    out.sort(key=lambda f: (-f["mine"]["games"], f["family"]))
    return jsonify(out)


def san_with_numbers(san, start=0):
    """['d4','Nf6','c4'] -> ['1. d4', 'Nf6', '2. c4']; from a later move, '4... dxc4' style."""
    out = []
    for i in range(start, len(san)):
        if i % 2 == 0:
            out.append(f"{i // 2 + 1}. {san[i]}")
        elif i == start:
            out.append(f"{i // 2 + 1}... {san[i]}")
        else:
            out.append(san[i])
    return out


@app.route("/api/openings/family")
def opening_family():
    ops, by_family, _ = openings.load()
    members = by_family.get(request.args.get("name", ""))
    if not members:
        return jsonify({"error": "unknown opening"}), 404
    stats = my_stats_for_keys([o["key"] for o in members])
    ids = {o["id"] for o in members}
    children = {}
    for o in members:
        parent = o["parent"] if o["parent"] in ids else None
        children.setdefault(parent, []).append(o)

    # depth-first: main line of the family first, then its variations
    def order(o):
        return (bool(o["variation"]), len(o["uci"]), o["name"])
    rows = []
    def walk(o, depth, parent_len):
        rows.append({"new": " ".join(san_with_numbers(o["san"], parent_len)),"id": o["id"], "eco": o["eco"], "name": o["name"], "key": o["key"],
                     "variation": o["variation"] or o["family"], "depth": depth,
                     "san": o["san"], "uci": o["uci"], "moves": " ".join(san_with_numbers(o["san"])),
                     "mine": stats.get(o["key"], {"games": 0, "win": 0, "draw": 0, "loss": 0})})
        for c in sorted(children.get(o["id"], []), key=order):
            walk(c, depth + 1, len(o["san"]))
    for root in sorted(children.get(None, []), key=order):
        walk(root, 0, 0)
    return jsonify({"family": members[0]["family"], "lines": rows})


@app.route("/api/openings/search")
def opening_search():
    """Variations whose name contains every word you typed."""
    ops, _, _ = openings.load()
    words = request.args.get("q", "").lower().split()
    if not words:
        return jsonify([])
    hits = [o for o in ops if all(w in o["name"].lower() for w in words)]
    hits.sort(key=lambda o: (len(o["uci"]), o["name"]))
    hits = hits[:60]
    stats = my_stats_for_keys([o["key"] for o in hits])
    return jsonify([{"id": o["id"], "eco": o["eco"], "name": o["name"], "family": o["family"],
                     "key": o["key"], "san": o["san"], "uci": o["uci"],
                     "moves": " ".join(san_with_numbers(o["san"])),
                     "mine": stats.get(o["key"], {"games": 0, "win": 0, "draw": 0, "loss": 0})}
                    for o in hits])


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
            analysis = engine.analysis(
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
        infos = engine.analyse(board, chess.engine.Limit(time=ANALYSIS_TIME),
                               multipv=NUM_LINES)

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
