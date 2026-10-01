"""Named chess openings, from lichess's public-domain list (openings/*.tsv).

Each opening is a named line of moves, e.g.
  E04  Catalan Opening: Open Defense  1. d4 Nf6 2. c4 e6 3. g3 d5 4. Bg2 dxc4 5. Nf3
Openings are grouped into families ("Catalan Opening") and arranged in a tree:
a variation's parent is the longest other line in its family that it extends.
"""
import csv
import glob
import io
import os
from functools import lru_cache

import chess
import chess.pgn

from database import position_key

OPENINGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "openings")


@lru_cache(maxsize=1)
def load():
    openings = []
    for path in sorted(glob.glob(os.path.join(OPENINGS_DIR, "*.tsv"))):
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f, delimiter="\t"):
                game = chess.pgn.read_game(io.StringIO(row["pgn"]))
                board = game.board()
                san, uci = [], []
                for move in game.mainline_moves():
                    san.append(board.san(move))
                    uci.append(move.uci())
                    board.push(move)
                name = row["name"]
                family, _, variation = name.partition(":")
                openings.append({
                    "id": len(openings),
                    "eco": row["eco"],
                    "name": name,
                    "family": family.strip(),
                    "variation": variation.strip(),
                    "pgn": row["pgn"],
                    "san": san,
                    "uci": uci,
                    "key": position_key(board.fen()),
                })

    # tree: parent = longest other line in the same family that this one extends
    by_family = {}
    for o in openings:
        by_family.setdefault(o["family"], []).append(o)
    for members in by_family.values():
        members.sort(key=lambda o: (len(o["uci"]), o["id"]))
        for i, o in enumerate(members):
            o["parent"] = None
            for p in reversed(members[:i]):
                if len(p["uci"]) < len(o["uci"]) and o["uci"][:len(p["uci"])] == p["uci"]:
                    o["parent"] = p["id"]
                    break

    # one name per position: the first listed line that reaches it
    names = {}
    for o in openings:
        names.setdefault(o["key"], {"name": o["name"], "eco": o["eco"], "family": o["family"]})
    return openings, by_family, names
