"""Game reviews, position history and opening stats, computed from the database.

Shared by server.py (the live app) and export_demo.py (the public demo site),
so both always show the same numbers. Every function takes an open sqlite3
connection and returns plain dicts/lists ready to turn into JSON.
"""
import io
import math

import chess
import chess.pgn

import database
import openings
from blunders import win_pct, classify, CAP

EMPTY_STATS = {"games": 0, "win": 0, "draw": 0, "loss": 0}


# ---------- Scoring single moves ----------
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


def result_for_me(result, my_color):
    if result == "1/2-1/2":
        return "draw"
    white_won = result == "1-0"
    return "win" if white_won == (my_color == "white") else "loss"


def verdict(avg_drop, all_best):
    """Stockfish's overall opinion of a move you've played several times."""
    if all_best:
        return "Best"
    if avg_drop < 2:
        return "Excellent"
    if avg_drop < 10:
        return "Good"
    return classify(avg_drop).capitalize()


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


# ---------- Games ----------
def list_games(conn, analyzed_only=True, limit=500):
    sql = "SELECT id, date, white, black, my_color, result FROM games"
    if analyzed_only:
        sql += " WHERE analyzed = 1"
    sql += " ORDER BY date DESC, id DESC LIMIT ?"
    games = []
    for gid, date, white, black, color, result in conn.execute(sql, (limit,)):
        opponent = black if color == "white" else white
        games.append({"id": gid, "label": f"{date} vs {opponent} ({color}, {result})"})
    return games


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


def game_payload(conn, game_id):
    """Everything the board needs to show and review one game, or None."""
    found = conn.execute("SELECT date, white, black, my_color, result, opening, pgn "
                         "FROM games WHERE id = ?", (game_id,)).fetchall()
    if not found:
        return None
    date, white, black, my_color, result, opening, pgn = found[0]
    rows = conn.execute("SELECT ply, move_played, best_move, eval_cp FROM positions "
                        "WHERE game_id = ? ORDER BY ply", (game_id,)).fetchall()

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
    return {"date": date, "white": white, "black": black,
            "my_color": my_color, "result": result, "opening": opening,
            "start_fen": start_fen, "moves": moves,
            "reviews": reviews, "summary": summarize(reviews)}


# ---------- Your moves in a position ----------
POSITION_SQL = """
    SELECT p.pos_key, p.move_played, p.best_move, p.eval_cp, nxt.eval_cp,
           g.my_color, g.result, g.date
    FROM positions p
    JOIN games g ON g.id = p.game_id
    LEFT JOIN positions nxt ON nxt.game_id = p.game_id AND nxt.ply = p.ply + 1"""


def _aggregate(rows, key):
    """Turn one position's rows into {to_move, mine: [...], opponents: [...]}."""
    board = chess.Board(key + " 0 1")
    to_move = "white" if board.turn == chess.WHITE else "black"
    sign = 1 if to_move == "white" else -1
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
                m["drops"].append(max(0.0, sign * (win_pct(clamp(ev)) - win_pct(clamp(ev_after)))))

    out = {"to_move": to_move}
    for side, moves in groups.items():
        lst = []
        for m in moves.values():
            analyzed = len(m["best"])
            m["verdict"] = None
            if analyzed:
                avg = sum(m["drops"]) / len(m["drops"]) if m["drops"] else 0
                m["verdict"] = verdict(avg, all(m["best"]))
            m["analyzed"] = analyzed
            # UCI so the board can play the move when you click its row
            try:
                m["uci"] = board.parse_san(m["move"]).uci()
            except ValueError:
                m["uci"] = None
            del m["drops"], m["best"]
            lst.append(m)
        lst.sort(key=lambda m: (-m["games"], m["move"]))
        out[side] = lst
    return out


def position_stats(conn, fen):
    """Every move played in this exact position across your games."""
    key = database.position_key(chess.Board(fen).fen())
    rows = conn.execute(POSITION_SQL + " WHERE p.pos_key = ?", (key,)).fetchall()
    return _aggregate([r[1:] for r in rows], key)


def all_position_stats(conn, min_games=2):
    """Position stats for every position you've reached at least min_games times."""
    keys = [k for (k,) in conn.execute(
        "SELECT pos_key FROM positions GROUP BY pos_key HAVING COUNT(*) >= ?", (min_games,))]
    wanted = set(keys)
    rows_by_key = {}
    for row in conn.execute(POSITION_SQL):
        if row[0] in wanted:
            rows_by_key.setdefault(row[0], []).append(row[1:])
    return {k: _aggregate(rows_by_key[k], k) for k in keys}


# ---------- Openings ----------
def my_stats_for_keys(conn, keys):
    """Your games reaching each position: {key: {games, win, draw, loss}}."""
    stats = {}
    keys = list(set(keys))
    for i in range(0, len(keys), 500):  # SQLite limits how many ? one query can have
        chunk = keys[i:i + 500]
        rows = conn.execute(f"""
            SELECT p.pos_key, g.my_color, g.result, COUNT(DISTINCT g.id)
            FROM positions p JOIN games g ON g.id = p.game_id
            WHERE p.pos_key IN ({",".join("?" * len(chunk))})
            GROUP BY p.pos_key, g.my_color, g.result""", chunk)
        for key, my_color, result, n in rows:
            s = stats.setdefault(key, dict(EMPTY_STATS))
            s["games"] += n
            s[result_for_me(result, my_color)] += n
    return stats


def opening_families(conn):
    ops, by_family, _ = openings.load()
    roots = {}
    for fam, members in by_family.items():
        roots[fam] = next((o for o in members if not o["variation"]), None) or members[0]
    stats = my_stats_for_keys(conn, [o["key"] for o in roots.values()])
    out = []
    for fam, root in roots.items():
        ecos = sorted({o["eco"] for o in by_family[fam]})
        out.append({"family": fam, "variations": len(by_family[fam]),
                    "eco": ecos[0] if len(ecos) == 1 else f"{ecos[0]}-{ecos[-1]}",
                    "moves": " ".join(san_with_numbers(root["san"])),
                    "mine": stats.get(root["key"], dict(EMPTY_STATS))})
    out.sort(key=lambda f: (-f["mine"]["games"], f["family"]))
    return out


def opening_family(conn, name, stats=None):
    """All lines of one family as a depth-first tree, or None if unknown."""
    ops, by_family, _ = openings.load()
    members = by_family.get(name)
    if not members:
        return None
    if stats is None:
        stats = my_stats_for_keys(conn, [o["key"] for o in members])
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
        rows.append({"id": o["id"], "eco": o["eco"], "name": o["name"], "family": o["family"],
                     "key": o["key"], "variation": o["variation"] or o["family"], "depth": depth,
                     "san": o["san"], "uci": o["uci"],
                     "moves": " ".join(san_with_numbers(o["san"])),
                     "new": " ".join(san_with_numbers(o["san"], parent_len)),
                     "mine": stats.get(o["key"], dict(EMPTY_STATS))})
        for c in sorted(children.get(o["id"], []), key=order):
            walk(c, depth + 1, len(o["san"]))
    for root in sorted(children.get(None, []), key=order):
        walk(root, 0, 0)
    return {"family": members[0]["family"], "lines": rows}


def opening_search(conn, text, limit=60):
    """Lines whose name contains every word you typed."""
    ops, _, _ = openings.load()
    words = text.lower().split()
    if not words:
        return []
    hits = [o for o in ops if all(w in o["name"].lower() for w in words)]
    hits.sort(key=lambda o: (len(o["uci"]), o["name"]))
    hits = hits[:limit]
    stats = my_stats_for_keys(conn, [o["key"] for o in hits])
    return [{"id": o["id"], "eco": o["eco"], "name": o["name"], "family": o["family"],
             "key": o["key"], "san": o["san"], "uci": o["uci"],
             "moves": " ".join(san_with_numbers(o["san"])),
             "mine": stats.get(o["key"], dict(EMPTY_STATS))}
            for o in hits]
