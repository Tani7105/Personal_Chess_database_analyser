"""Build the public demo site (a static copy of the app) into docs/ for GitHub Pages.

    python export_demo.py               # 40 most recent analyzed games
    python export_demo.py --games 60

The demo has no server. Everything server.py would compute is written out as
JSON files, and Stockfish runs in the visitor's browser instead. Opponents'
usernames are replaced with "Opponent".
"""
import argparse
import json
import os
import shutil
import sqlite3

import insights
import openings
from database import DB_PATH, USERNAME

STATIC = "static"
FILES_FROM_STATIC = ["app.js"]
VENDOR = os.path.join(STATIC, "vendor")
REPO_URL = "https://github.com/Tani7105/Personal_Chess_database_anaylser"


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"), ensure_ascii=False)


def shard_of(key):
    """FNV-1a hash of the position, same as shardOf() in api-demo.js."""
    h = 0x811C9DC5
    for byte in key.encode("utf-8"):
        h ^= byte
        h = (h * 0x01000193) & 0xFFFFFFFF
    return f"{h & 0xFF:02x}"


def anonymize(payload):
    for side in ("white", "black"):
        if payload[side].lower() != USERNAME.lower():
            payload[side] = "Opponent"
    return payload


def game_label(g):
    opening = (g["opening"] or "").strip()
    opening = (opening[:38] + "...") if len(opening) > 40 else opening
    color = g["my_color"]
    return f"{g['date']} vs Opponent, {opening} ({color}, {g['result']})" if opening \
        else f"{g['date']} vs Opponent ({color}, {g['result']})"


def build_site(out):
    """Copy the board page, its code and libraries into the output folder."""
    with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as f:
        html = f.read()
    html = html.replace('href="/static/', 'href="./').replace('src="/static/', 'src="./')
    banner = (f'<div class="demoBanner">Live demo with a sample of my games. Stockfish runs in your '
              f'browser, so analysis starts after a short download. '
              f'<a href="{REPO_URL}">Source on GitHub</a></div>')
    html = html.replace("<main>", banner + "\n<main>", 1)
    html = html.replace("</style>", """  .demoBanner { background: #3d4a2c; color: #e4ecd8; font-size: 13px; padding: 7px 20px; }
  .demoBanner a { color: #fff; font-weight: bold; }
</style>""", 1)
    with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    for name in FILES_FROM_STATIC:
        shutil.copy(os.path.join(STATIC, name), os.path.join(out, name))
    # the demo's api.js reads exported files and runs Stockfish in the browser
    shutil.copy(os.path.join(STATIC, "api-demo.js"), os.path.join(out, "api.js"))
    shutil.copytree(VENDOR, os.path.join(out, "vendor"), dirs_exist_ok=True)
    open(os.path.join(out, ".nojekyll"), "w").close()  # serve files as-is on GitHub Pages


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--games", type=int, default=40, help="how many recent analyzed games to include")
    parser.add_argument("--min-games", type=int, default=2,
                        help="include position stats for positions reached at least this many times")
    parser.add_argument("--out", default="docs")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    data_dir = os.path.join(args.out, "data")
    os.makedirs(data_dir, exist_ok=True)  # files are overwritten in place

    # Games
    rows = conn.execute("""SELECT id, date, my_color, result, opening FROM games
                           WHERE analyzed = 1 ORDER BY date DESC, id DESC LIMIT ?""",
                        (args.games,)).fetchall()
    games, game_keys = [], set()
    for gid, date, my_color, result, opening in rows:
        payload = anonymize(insights.game_payload(conn, gid))
        write_json(os.path.join(data_dir, "games", f"{gid}.json"), payload)
        games.append({"id": gid, "label": game_label(
            {"date": date, "my_color": my_color, "result": result, "opening": opening})})
        game_keys.update(k for (k,) in conn.execute(
            "SELECT pos_key FROM positions WHERE game_id = ?", (gid,)))
    write_json(os.path.join(data_dir, "games.json"), games)
    # remove games left over from an earlier export with a different sample
    keep = {f"{g['id']}.json" for g in games}
    for name in os.listdir(os.path.join(data_dir, "games")):
        if name not in keep:
            try:
                os.remove(os.path.join(data_dir, "games", name))
            except OSError:
                pass
    print(f"Games: {len(games)}")

    # Your moves in each position: positions you've reached often, plus every
    # position in the sample games, split into 256 small files
    stats = insights.all_position_stats(conn, args.min_games)
    missing = game_keys - stats.keys()
    for key in missing:
        rows = conn.execute(insights.POSITION_SQL + " WHERE p.pos_key = ?", (key,)).fetchall()
        stats[key] = insights._aggregate([r[1:] for r in rows], key)
    shards = {f"{i:02x}": {} for i in range(256)}  # every file exists, even if empty
    for key, s in stats.items():
        shards.setdefault(shard_of(key), {})[key] = {"mine": s["mine"], "opponents": s["opponents"]}
    for shard, content in shards.items():
        write_json(os.path.join(data_dir, "stats", f"{shard}.json"), content)
    print(f"Position stats: {len(stats)} positions")

    # Openings
    ops, by_family, names = openings.load()
    write_json(os.path.join(data_dir, "opening-names.json"), names)
    all_stats = insights.my_stats_for_keys(conn, [o["key"] for o in ops])
    write_json(os.path.join(data_dir, "openings.json"), {
        "families": insights.opening_families(conn),
        "lines": {fam: insights.opening_family(conn, fam, all_stats)["lines"] for fam in by_family},
    })
    print(f"Openings: {len(ops)} lines in {len(by_family)} families")
    conn.close()

    build_site(args.out)
    total = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(args.out) for f in fs)
    print(f"Demo site written to {args.out}/ ({total / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
