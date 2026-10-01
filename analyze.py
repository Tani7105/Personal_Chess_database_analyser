import sqlite3
import sys
import chess
import chess.engine

DB_PATH = "data/chess.db"
TIME_PER_POSITION = 0.3  # seconds; raise for stronger analysis

def analyze_games(limit):
    conn = sqlite3.connect(DB_PATH)
    engine = chess.engine.SimpleEngine.popen_uci("stockfish")

    games = conn.execute("""
        SELECT id FROM games WHERE analyzed = 0
        ORDER BY date DESC, id DESC LIMIT ?""", (limit,)).fetchall()

    for i, (game_id,) in enumerate(games, start=1):
        positions = conn.execute(
            "SELECT id, fen FROM positions WHERE game_id = ? ORDER BY ply",
            (game_id,)).fetchall()

        for pos_id, fen in positions:
            board = chess.Board(fen)
            info = engine.analyse(board, chess.engine.Limit(time=TIME_PER_POSITION))
            best = board.san(info["pv"][0])
            eval_cp = info["score"].white().score(mate_score=10000)
            conn.execute(
                "UPDATE positions SET best_move = ?, eval_cp = ? WHERE id = ?",
                (best, eval_cp, pos_id))

        conn.execute("UPDATE games SET analyzed = 1 WHERE id = ?", (game_id,))
        conn.commit()  # save after every game
        print(f"Analyzed game {i}/{len(games)}")

    engine.quit()
    conn.close()

if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    analyze_games(n)