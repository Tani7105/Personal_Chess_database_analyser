"""Pull your newest chess.com games into the database and analyze them.

Usage:
  python update.py               fetch + import new games, analyze recent ones
  python update.py --force       analyze even when running on battery
  python update.py --no-analyze  only fetch and import

Also used by server.py (Sync button) and the hourly background job.
"""
import fcntl
import io
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import date, timedelta

import chess.pgn
import requests

from database import DB_PATH, USERNAME, create_tables, insert_game
from analyze import open_engine, analyze_game

HEADERS = {"User-Agent": "chess-db personal project"}
MONTHS_TO_CHECK = 2     # current + previous month covers games across a month boundary
MAX_ANALYZE = 20        # cap per run so one run never turns into hours of work
RECENT_DAYS = 7         # auto-analysis only touches games from the last week
PULL_LOCK = "data/pull.lock"
ANALYZE_LOCK = "data/analyze.lock"


class Busy(Exception):
    """Another update is already doing this step."""


@contextmanager
def file_lock(path):
    f = open(path, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        raise Busy()
    try:
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def on_ac_power():
    """True if the Mac is plugged in (or if we can't tell)."""
    try:
        out = subprocess.run(["pmset", "-g", "batt"], capture_output=True,
                             text=True, timeout=5).stdout
        return "AC Power" in out
    except Exception:
        return True


def fetch_recent_games(months=MONTHS_TO_CHECK):
    url = f"https://api.chess.com/pub/player/{USERNAME}/games/archives"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    games = []
    for month_url in resp.json()["archives"][-months:]:
        r = requests.get(month_url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        games += [g for g in r.json().get("games", []) if "pgn" in g]
    return games


def pull_new_games():
    """Fetch the last couple of months from chess.com and store games we don't have.
    Returns the ids of newly added games."""
    with file_lock(PULL_LOCK):
        games = fetch_recent_games()
        conn = sqlite3.connect(DB_PATH, timeout=30)
        try:
            create_tables(conn)
            known = {row[0] for row in conn.execute("SELECT link FROM games")}
            new_ids = []
            for g in games:
                if g.get("url") in known:
                    continue  # already stored; skip parsing it
                game = chess.pgn.read_game(io.StringIO(g["pgn"]))
                if game is None:
                    continue
                game_id = insert_game(conn, game)
                if game_id:
                    new_ids.append(game_id)
            conn.commit()
            return new_ids
        finally:
            conn.close()


def analyze_recent(force=False, threads=1, log=print):
    """Analyze unanalyzed games from the last RECENT_DAYS days (newest first),
    at most MAX_ANALYZE per run. Skipped on battery unless force=True."""
    if not force and not on_ac_power():
        log("On battery: new games saved, analysis waits until you're plugged in")
        return 0
    with file_lock(ANALYZE_LOCK):
        cutoff = (date.today() - timedelta(days=RECENT_DAYS)).strftime("%Y.%m.%d")
        conn = sqlite3.connect(DB_PATH, timeout=30)
        try:
            ids = [row[0] for row in conn.execute(
                "SELECT id FROM games WHERE analyzed = 0 AND date >= ? "
                "ORDER BY date DESC, id DESC LIMIT ?", (cutoff, MAX_ANALYZE))]
            if not ids:
                log("All recent games are analyzed")
                return 0
            engine = open_engine(threads)
            try:
                for i, game_id in enumerate(ids, start=1):
                    log(f"Analyzing new game {i}/{len(ids)}...")
                    analyze_game(conn, engine, game_id)
            finally:
                engine.quit()
            log(f"Analyzed {len(ids)} new game{'s' if len(ids) != 1 else ''}")
            return len(ids)
        finally:
            conn.close()


if __name__ == "__main__":
    args = sys.argv[1:]
    stamp = time.strftime("%Y-%m-%d %H:%M")
    try:
        new_ids = pull_new_games()
        print(f"[{stamp}] {len(new_ids)} new game(s) imported")
    except Busy:
        print(f"[{stamp}] Another update is already pulling games; skipping")
        sys.exit(0)
    except requests.RequestException as e:
        print(f"[{stamp}] Could not reach chess.com: {e}")
        sys.exit(1)

    if "--no-analyze" not in args:
        try:
            analyze_recent(force="--force" in args,
                           log=lambda m: print(f"[{stamp}] {m}"))
        except Busy:
            print(f"[{stamp}] Analysis already running elsewhere; skipping")
