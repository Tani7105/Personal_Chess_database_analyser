import sqlite3
import sys
import chess
import chess.engine

DB_PATH = "data/chess.db"
ENGINE_PATH = "stockfish"
TIME_PER_POSITION = 0.3  # seconds; raise for stronger analysis

def open_engine(threads=1, hash_mb=128):
    engine = chess.engine.SimpleEngine.popen_uci(ENGINE_PATH)
    engine.configure({"Threads": threads, "Hash": hash_mb})
    return engine

def analyze_game(conn, engine, game_id):
    """Run Stockfish on every position of one game and mark it analyzed."""
    positions = conn.execute(
        "SELECT id, fen FROM positions WHERE game_id = ? ORDER BY ply",
        (game_id,)).fetchall()

    # Analyze everything first, without touching the database...
    results = []
    for pos_id, fen in positions:
        board = chess.Board(fen)
        info = engine.analyse(board, chess.engine.Limit(time=TIME_PER_POSITION))
        best = board.san(info["pv"][0])
        eval_cp = info["score"].white().score(mate_score=10000)
        results.append((best, eval_cp, pos_id))

    # ...then save it all at once, so the database is only locked for milliseconds
    conn.executemany(
        "UPDATE positions SET best_move = ?, eval_cp = ? WHERE id = ?", results)
    conn.execute("UPDATE games SET analyzed = 1 WHERE id = ?", (game_id,))
    conn.commit()

def analyze_games(limit, threads=1):
    conn = sqlite3.connect(DB_PATH, timeout=30)
    engine = open_engine(threads)

    games = conn.execute("""
        SELECT id FROM games WHERE analyzed = 0
        ORDER BY date DESC, id DESC LIMIT ?""", (limit,)).fetchall()

    for i, (game_id,) in enumerate(games, start=1):
        analyze_game(conn, engine, game_id)
        print(f"Analyzed game {i}/{len(games)}")

    engine.quit()
    conn.close()

if __name__ == "__main__":
    # python analyze.py [number_of_games] [threads]
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    threads = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    analyze_games(n, threads)
