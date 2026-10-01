import sqlite3
import chess.pgn

USERNAME = "GodLike7105"
DB_PATH = "data/chess.db"

def create_tables(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS games (
        id INTEGER PRIMARY KEY,
        link TEXT UNIQUE,
        date TEXT,
        white TEXT,
        black TEXT,
        my_color TEXT,
        result TEXT,
        time_control TEXT,
        eco TEXT,
        opening TEXT,
        pgn TEXT,
        analyzed INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS positions (
        id INTEGER PRIMARY KEY,
        game_id INTEGER,
        ply INTEGER,
        fen TEXT,
        move_played TEXT,
        best_move TEXT,
        eval_cp INTEGER,
        FOREIGN KEY (game_id) REFERENCES games(id)
    );
    CREATE INDEX IF NOT EXISTS idx_fen ON positions(fen);
    """)

def import_pgn_file(conn, path):
    added = 0
    with open(path) as f:
        while True:
            game = chess.pgn.read_game(f)
            if game is None:
                break
            h = game.headers
            my_color = "white" if h.get("White", "").lower() == USERNAME.lower() else "black"
            opening = h.get("ECOUrl", "").split("/")[-1].replace("-", " ")

            cur = conn.execute("""
                INSERT OR IGNORE INTO games
                (link, date, white, black, my_color, result, time_control, eco, opening, pgn)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (h.get("Link"), h.get("Date"), h.get("White"), h.get("Black"),
                 my_color, h.get("Result"), h.get("TimeControl"),
                 h.get("ECO"), opening, str(game)))

            if cur.rowcount == 0:
                continue  # game already in the database

            game_id = cur.lastrowid
            board = game.board()
            for ply, move in enumerate(game.mainline_moves(), start=1):
                conn.execute(
                    "INSERT INTO positions (game_id, ply, fen, move_played) VALUES (?, ?, ?, ?)",
                    (game_id, ply, board.fen(), board.san(move)))
                board.push(move)
            added += 1
    conn.commit()
    return added

if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    create_tables(conn)
    n = import_pgn_file(conn, "data/my_games.pgn")
    print(f"Imported {n} new games")
    conn.close()