import sqlite3
import chess.pgn

USERNAME = "GodLike7105"
DB_PATH = "data/chess.db"

def enable_wal(conn):
    """WAL mode lets the board read while another script writes.
    Switching needs a moment when nothing else has the database open, so just try."""
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass  # another script is using it right now; try again next time

def create_tables(conn):
    enable_wal(conn)
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
    migrate(conn)

def position_key(fen):
    """The position without the move counters, so the same position reached
    at a different move number (or by a different move order) still matches."""
    return " ".join(fen.split()[:4])

def migrate(conn):
    """Add the pos_key column and indexes to databases created before they existed."""
    cols = [row[1] for row in conn.execute("PRAGMA table_info(positions)")]
    if "pos_key" not in cols:
        conn.execute("ALTER TABLE positions ADD COLUMN pos_key TEXT")
    if conn.execute("SELECT 1 FROM positions WHERE pos_key IS NULL LIMIT 1").fetchone():
        conn.create_function("position_key", 1, position_key, deterministic=True)
        conn.execute("UPDATE positions SET pos_key = position_key(fen) WHERE pos_key IS NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pos_key ON positions(pos_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_game_ply ON positions(game_id, ply)")
    conn.commit()

def insert_game(conn, game):
    """Store one parsed game and its positions. Returns the new game id,
    or None if the game is already in the database. Caller commits."""
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
        return None  # game already in the database

    game_id = cur.lastrowid
    board = game.board()
    for ply, move in enumerate(game.mainline_moves(), start=1):
        fen = board.fen()
        conn.execute(
            "INSERT INTO positions (game_id, ply, fen, pos_key, move_played) VALUES (?, ?, ?, ?, ?)",
            (game_id, ply, fen, position_key(fen), board.san(move)))
        board.push(move)
    return game_id

def import_pgn_file(conn, path):
    added = 0
    with open(path) as f:
        while True:
            game = chess.pgn.read_game(f)
            if game is None:
                break
            if insert_game(conn, game):
                added += 1
    conn.commit()
    return added

if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    create_tables(conn)
    n = import_pgn_file(conn, "data/my_games.pgn")
    print(f"Imported {n} new games")
    conn.close()
