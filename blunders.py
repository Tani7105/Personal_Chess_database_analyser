import math
import sqlite3
from itertools import groupby

DB_PATH = "data/chess.db"
CAP = 1000  # cap evals at +/-10 pawns

def win_pct(cp):
    """Centipawns -> White's win probability (0-100), Lichess formula."""
    return 50 + 50 * (2 / (1 + math.exp(-0.00368208 * cp)) - 1)

def classify(drop):
    if drop >= 30:
        return "BLUNDER"
    if drop >= 20:
        return "Mistake"
    if drop >= 10:
        return "Inaccuracy"
    return None

def find_mistakes():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT p.game_id, p.ply, p.move_played, p.best_move, p.eval_cp,
               g.my_color, g.date, g.white, g.black
        FROM positions p JOIN games g ON p.game_id = g.id
        WHERE g.analyzed = 1
        ORDER BY p.game_id, p.ply""").fetchall()

    totals = {"BLUNDER": 0, "Mistake": 0, "Inaccuracy": 0}
    for game_id, moves in groupby(rows, key=lambda r: r[0]):
        moves = list(moves)
        _, _, _, _, _, my_color, date, white, black = moves[0]
        opponent = black if my_color == "white" else white
        sign = 1 if my_color == "white" else -1
        my_parity = 1 if my_color == "white" else 0

        found = []
        for before, after in zip(moves, moves[1:]):
            ply, played, best, eval_before = before[1], before[2], before[3], before[4]
            if ply % 2 != my_parity or played == best:
                continue
            eb = max(-CAP, min(CAP, eval_before))
            ea = max(-CAP, min(CAP, after[4]))
            drop = sign * (win_pct(eb) - win_pct(ea))
            label = classify(drop)
            if label:
                totals[label] += 1
                move_no = (ply + 1) // 2
                found.append(f"  Move {move_no}: {label} - played {played}, "
                             f"best was {best} (lost {drop:.0f}% win chance)")

        if found:
            print(f"\n{date} vs {opponent} (you were {my_color})")
            print("\n".join(found))

    print(f"\nTotal: {totals['BLUNDER']} blunders, {totals['Mistake']} mistakes, "
          f"{totals['Inaccuracy']} inaccuracies")
    conn.close()

if __name__ == "__main__":
    find_mistakes()