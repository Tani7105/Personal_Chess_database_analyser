"""Interactive chess analysis server.

Serves the board page and runs Stockfish on any position the page sends.
Run with:  python server.py   then open http://localhost:5050
"""
import atexit
import io
import sqlite3
import threading

import chess
import chess.engine
import chess.pgn
from flask import Flask, jsonify, request, send_from_directory

from blunders import win_pct, classify, CAP

DB_PATH = "data/chess.db"
ENGINE_PATH = "stockfish"
ANALYSIS_TIME = 1.0   # seconds Stockfish thinks per position in the live board
NUM_LINES = 3         # how many top lines to show

app = Flask(__name__, static_folder="static")
engine = chess.engine.SimpleEngine.popen_uci(ENGINE_PATH)
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


def find_flags(rows, my_color):
    """Same scoring as blunders.py. Returns a list of flagged moves (0-based move index)."""
    sign = 1 if my_color == "white" else -1
    my_parity = 1 if my_color == "white" else 0
    flags = []
    for k in range(len(rows) - 1):
        ply, played, best, ev = rows[k]
        ev_after = rows[k + 1][3]
        if ply % 2 != my_parity or played == best or ev is None or ev_after is None:
            continue
        drop = sign * (win_pct(clamp(ev)) - win_pct(clamp(ev_after)))
        label = classify(drop)
        if label:
            flags.append({"index": k, "label": label, "drop": round(drop),
                          "played": played, "best": best})
    return flags


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

    return jsonify({"date": date, "white": white, "black": black,
                    "my_color": my_color, "result": result, "opening": opening,
                    "start_fen": start_fen, "moves": moves,
                    "flags": find_flags(rows, my_color)})


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


if __name__ == "__main__":
    # Port 5050 because macOS uses 5000 for AirPlay
    app.run(port=5050, threaded=True)
