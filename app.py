import base64
import io
import sqlite3

import chess
import chess.pgn
import chess.svg
import streamlit as st

from blunders import win_pct, classify, CAP

DB_PATH = "data/chess.db"
st.set_page_config(page_title="My Chess Database", layout="wide")


@st.cache_resource
def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def show_board(board, flipped, arrows, lastmove):
    svg = chess.svg.board(board, flipped=flipped, arrows=arrows,
                          lastmove=lastmove, size=480)
    b64 = base64.b64encode(svg.encode()).decode()
    st.markdown(f'<img src="data:image/svg+xml;base64,{b64}"/>',
                unsafe_allow_html=True)


def clamp(cp):
    return max(-CAP, min(CAP, cp))


conn = get_conn()

# ---------- Sidebar: pick a game ----------
st.sidebar.title("My Chess Database")
only_analyzed = st.sidebar.checkbox("Analyzed games only", value=True)

query = "SELECT id, date, white, black, my_color, result, opening, pgn FROM games"
if only_analyzed:
    query += " WHERE analyzed = 1"
query += " ORDER BY date DESC, id DESC LIMIT 500"
games = conn.execute(query).fetchall()

if not games:
    st.warning("No games found. Run analyze.py first.")
    st.stop()


def game_label(g):
    _, date, white, black, color, result, _, _ = g
    opponent = black if color == "white" else white
    return f"{date} vs {opponent} ({color}, {result})"


game = st.sidebar.selectbox("Game", games, format_func=game_label)
game_id, date, white, black, my_color, result, opening, pgn = game

# Reset to the start position when a new game is picked
if st.session_state.get("game_id") != game_id:
    st.session_state.game_id = game_id
    st.session_state.ply = 0

moves = list(chess.pgn.read_game(io.StringIO(pgn)).mainline_moves())
rows = conn.execute(
    "SELECT ply, move_played, best_move, eval_cp FROM positions "
    "WHERE game_id = ? ORDER BY ply", (game_id,)).fetchall()
n = len(moves)

# ---------- Find your mistakes in this game ----------
sign = 1 if my_color == "white" else -1
my_parity = 1 if my_color == "white" else 0
flags = {}  # index of position -> (label, % win chance lost)
for k in range(len(rows) - 1):
    ply, played, best, ev = rows[k]
    ev_after = rows[k + 1][3]
    if ply % 2 != my_parity or played == best or ev is None or ev_after is None:
        continue
    drop = sign * (win_pct(clamp(ev)) - win_pct(clamp(ev_after)))
    label = classify(drop)
    if label:
        flags[k] = (label, drop)

# ---------- Navigation ----------
def go(to=None, delta=0):
    target = to if to is not None else st.session_state.ply + delta
    st.session_state.ply = max(0, min(n, target))


st.title(f"{white} vs {black}")
st.caption(f"{date} | {opening or 'Unknown opening'} | Result: {result} | You played {my_color}")

b1, b2, b3, b4 = st.columns(4)
b1.button("Start", on_click=go, kwargs={"to": 0}, use_container_width=True)
b2.button("Back", on_click=go, kwargs={"delta": -1}, use_container_width=True)
b3.button("Next", on_click=go, kwargs={"delta": 1}, use_container_width=True)
b4.button("End", on_click=go, kwargs={"to": n}, use_container_width=True)
if n > 0:
    st.slider("Move", 0, n, key="ply")

ply = st.session_state.ply

# ---------- Board ----------
board = chess.Board()
for m in moves[:ply]:
    board.push(m)

arrows, played_san, best_san, ev = [], None, None, None
if ply < len(rows):
    _, played_san, best_san, ev = rows[ply]
    played_move = moves[ply]
    if best_san:
        best_move = board.parse_san(best_san)
        arrows.append(chess.svg.Arrow(best_move.from_square, best_move.to_square,
                                      color="#2e9e44"))
        if best_move != played_move:
            arrows.append(chess.svg.Arrow(played_move.from_square,
                                          played_move.to_square, color="#d64545"))

left, right = st.columns([3, 2])
with left:
    show_board(board, flipped=(my_color == "black"), arrows=arrows,
               lastmove=moves[ply - 1] if ply > 0 else None)
    st.caption("Green arrow: Stockfish's best move | Red arrow: the move actually played")

# ---------- Info panel ----------
with right:
    st.subheader("White to move" if board.turn else "Black to move")
    if ev is not None:
        if abs(ev) >= 9000:
            st.metric("Eval", "Mate for White" if ev > 0 else "Mate for Black")
        else:
            st.metric("Eval (White's view)", f"{ev / 100:+.2f}")
    elif ply < n:
        st.info("This game hasn't been analyzed yet.")

    if played_san:
        st.write(f"**Played:** {played_san}")
        st.write(f"**Best:** {best_san or 'n/a'}")
        if ply in flags:
            label, drop = flags[ply]
            st.error(f"{label}: lost {drop:.0f}% win chance")
    elif ply == n:
        st.write("End of game.")

    st.divider()
    st.subheader("Your mistakes in this game")
    if not flags:
        st.write("None flagged.")
    for k, (label, drop) in flags.items():
        st.button(f"Move {(k + 2) // 2}: {label} ({rows[k][1]}, {drop:.0f}%)",
                  on_click=go, kwargs={"to": k}, key=f"jump_{k}")