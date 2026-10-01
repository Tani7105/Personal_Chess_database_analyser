"""Interactive chess analysis server.

Serves the board page and runs Stockfish on any position the page sends.
Run with:  python server.py   then open http://localhost:5050
"""
import atexit
import io
import math
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
