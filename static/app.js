import { Chessground } from './vendor/chessground.min.js';
import { Chess } from './vendor/chess.js';

const $ = sel => document.querySelector(sel);
const START_FEN = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1';
const LABELS = ['Best', 'Excellent', 'Good', 'Inaccuracy', 'Mistake', 'Blunder'];
const SYMBOLS = { Best: '★', Excellent: '!', Good: '✓',
                  Inaccuracy: '?!', Mistake: '?', Blunder: '??' };

// ---------- State ----------
// The game is a tree of positions. The game's own moves are the "main" line;
// every move you try yourself becomes a branch (variation) off it.
let gameData = null;   // the loaded game (null = free analysis)
let reviews = [];      // reviews[i] = verdict on game move i
let nodes = {};        // id -> node
let nextId = 0;
let root = null;       // starting position
let cur = null;        // position shown on the board
let engineLines = [];
let analysisId = 0;
let analysisTimer = null;

function makeNode(parent, fen, move, main, gameIndex) {
  const node = {
    id: nextId++, parent, children: [], fen,
    san: move ? move.san : null,
    uci: move ? move.from + move.to + (move.promotion || '') : null,
    from: move ? move.from : null, to: move ? move.to : null,
    ply: parent ? parent.ply + 1 : 0,
    main, gameIndex, shapes: [],
  };
  nodes[node.id] = node;
  if (parent) parent.children.push(node);
  return node;
}

function buildTree(startFen, moves) {
  nodes = {}; nextId = 0;
  root = makeNode(null, startFen, null, true, null);
  let n = root;
  moves.forEach((m, i) => { n = makeNode(n, m.fen, m, true, i); });
  cur = root;
}

// ---------- Board ----------
const cg = Chessground($('#board'), {
  coordinates: true,
  premovable: { enabled: false },
  highlight: { lastMove: true, check: true },
  movable: { free: false, showDests: true, events: { after: onBoardMove } },
  drawable: { enabled: false, visible: false },  // we draw our own arrows below
});
window.addEventListener('resize', () => cg.redrawAll());

// ---------- Arrows and square highlights (chess.com style) ----------
// Drawn in our own SVG layer on top of the board, 100 units per square.
const COLORS = {
  orange: '#ffaa00', green: '#9fcf3f', blue: '#52b0dc', red: '#f05d4e',
  engine: '#81b64c', engineHover: '#81b64c',
};
const OPACITY = { engine: 0.8, engineHover: 0.45 };
const overlay = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
overlay.setAttribute('viewBox', '0 0 800 800');
overlay.id = 'overlay';
$('#boardWrap').appendChild(overlay);
// square highlights go in a second layer underneath the pieces
const underlay = overlay.cloneNode();
underlay.id = 'underlay';
$('#boardWrap').appendChild(underlay);

let engineShapes = [];   // best-move arrows from Stockfish
let preview = null;      // the arrow you're dragging right now
let dragStart = null;

function squareCenter(sq) {
  const file = 'abcdefgh'.indexOf(sq[0]), rank = Number(sq[1]);
  const white = cg.state.orientation === 'white';
  return {
    x: ((white ? file : 7 - file) + 0.5) * 100,
    y: ((white ? 8 - rank : rank - 1) + 0.5) * 100,
  };
}

function squareAt(e) {
  const r = $('#board').getBoundingClientRect();
  const col = Math.floor(((e.clientX - r.left) / r.width) * 8);
  const row = Math.floor(((e.clientY - r.top) / r.height) * 8);
  if (col < 0 || col > 7 || row < 0 || row > 7) return null;
  const white = cg.state.orientation === 'white';
  const file = white ? col : 7 - col, rank = white ? 8 - row : row + 1;
  return 'abcdefgh'[file] + rank;
}

// Straight arrow from p to the tip q, as one polygon
function arrowPolygon(p, q, startAtP) {
  const W = 22, HEAD_W = 52, HEAD_L = 42, PULL = 12;
  const len = Math.hypot(q.x - p.x, q.y - p.y);
  const dx = (q.x - p.x) / len, dy = (q.y - p.y) / len;
  const nx = -dy, ny = dx;
  const tip = { x: q.x - dx * PULL, y: q.y - dy * PULL };
  const start = startAtP ? p : { x: p.x + dx * 12, y: p.y + dy * 12 };
  const base = { x: tip.x - dx * HEAD_L, y: tip.y - dy * HEAD_L };
  const pts = [
    [start.x + nx * W / 2, start.y + ny * W / 2], [base.x + nx * W / 2, base.y + ny * W / 2],
    [base.x + nx * HEAD_W / 2, base.y + ny * HEAD_W / 2], [tip.x, tip.y],
    [base.x - nx * HEAD_W / 2, base.y - ny * HEAD_W / 2], [base.x - nx * W / 2, base.y - ny * W / 2],
    [start.x - nx * W / 2, start.y - ny * W / 2],
  ];
  return `<polygon points="${pts.map(p => p.join(',')).join(' ')}"/>`;
}

function shapeSvg(shape) {
  const color = COLORS[shape.color] || COLORS.orange;
  const opacity = OPACITY[shape.color] || 0.8;
  const a = squareCenter(shape.from);
  if (!shape.to || shape.to === shape.from) {
    return `<rect x="${a.x - 50}" y="${a.y - 50}" width="100" height="100" fill="${color}" opacity="${opacity * 0.85}"/>`;
  }
  const b = squareCenter(shape.to);
  const cols = Math.abs(b.x - a.x) / 100, rows = Math.abs(b.y - a.y) / 100;
  let body;
  if ((cols === 1 && rows === 2) || (cols === 2 && rows === 1)) {
    // knight move: L-shaped arrow, long leg first
    const corner = rows === 2 ? { x: a.x, y: b.y } : { x: b.x, y: a.y };
    const W = 22;
    const x1 = Math.min(a.x, corner.x), x2 = Math.max(a.x, corner.x);
    const y1 = Math.min(a.y, corner.y), y2 = Math.max(a.y, corner.y);
    const leg = rows === 2
      ? `<rect x="${x1 - W / 2}" y="${y1 - W / 2}" width="${W}" height="${y2 - y1 + W}"/>`
      : `<rect x="${x1 - W / 2}" y="${y1 - W / 2}" width="${x2 - x1 + W}" height="${W}"/>`;
    body = leg + arrowPolygon(corner, b, true);
  } else {
    body = arrowPolygon(a, b, false);
  }
  return `<g fill="${color}" opacity="${opacity}">${body}</g>`;
}

function drawOverlay() {
  const shapes = [...engineShapes, ...((cur && cur.shapes) || [])];
  if (preview) shapes.push(preview);
  const isSquare = s => !s.to || s.to === s.from;
  underlay.innerHTML = shapes.filter(isSquare).map(shapeSvg).join('');
  overlay.innerHTML = shapes.filter(s => !isSquare(s)).map(shapeSvg).join('');
}

function brushFor(e) {
  if (e.shiftKey && e.altKey) return 'red';
  if (e.shiftKey) return 'green';
  if (e.altKey) return 'blue';
  return 'orange';
}

// Right-click and drag to draw. Capture phase, so the board never sees right-clicks.
$('#boardWrap').addEventListener('mousedown', e => {
  if (e.button === 2) {
    e.stopPropagation(); e.preventDefault();
    const sq = squareAt(e);
    if (!sq) return;
    dragStart = sq;
    preview = { from: sq, to: null, color: brushFor(e) };
    drawOverlay();
  } else if (e.button === 0 && cur && cur.shapes.length) {
    cur.shapes = [];  // left click clears your arrows, like chess.com
    drawOverlay();
  }
}, true);

window.addEventListener('mousemove', e => {
  if (!dragStart) return;
  const sq = squareAt(e);
  if (sq && sq !== preview.to) {
    preview = { from: dragStart, to: sq === dragStart ? null : sq, color: brushFor(e) };
    drawOverlay();
  }
});

window.addEventListener('mouseup', e => {
  if (e.button !== 2 || !dragStart) return;
  const sq = squareAt(e) || preview.to || dragStart;
  const shape = { from: dragStart, to: sq === dragStart ? null : sq, color: brushFor(e) };
  // drawing the same arrow again removes it; a different color replaces it
  const same = s => s.from === shape.from && (s.to || null) === shape.to;
  const existing = cur.shapes.find(same);
  cur.shapes = cur.shapes.filter(s => !same(s));
  if (!existing || existing.color !== shape.color) cur.shapes.push(shape);
  dragStart = null;
  preview = null;
  drawOverlay();
});
$('#boardWrap').addEventListener('contextmenu', e => e.preventDefault());

function legalDests(chess) {
  const dests = new Map();
  for (const m of chess.moves({ verbose: true })) {
    if (!dests.has(m.from)) dests.set(m.from, []);
    dests.get(m.from).push(m.to);
  }
  return dests;
}

function onBoardMove(from, to) {
  if (!playMove(from, to)) update();  // illegal somehow: snap back
}

// Play a move from the current position. Reuses an existing branch if you
// already played that move here, otherwise starts a new one.
function playMove(from, to, promotion) {
  const chess = new Chess(cur.fen);
  let move;
  try {
    move = chess.move({ from, to, promotion: promotion || 'q' });
  } catch (e) {
    return false;
  }
  const uci = move.from + move.to + (move.promotion || '');
  let child = cur.children.find(c => c.uci === uci);
  if (!child) {
    // In free analysis, the first line you play becomes the main line
    const main = !gameData && cur.main && !cur.children.some(c => c.main);
    child = makeNode(cur, chess.fen(), move, main, null);
  }
  cur = child;
  update();
  return true;
}

function playSan(node, san) {
  const chess = new Chess(node.fen);
  try {
    const m = chess.move(san);
    cur = node;
    return playMove(m.from, m.to, m.promotion);
  } catch (e) {
    return false;
  }
}

// ---------- Navigation ----------
function goTo(node) { if (node) { cur = node; update(); } }
function back() { goTo(cur.parent); }
function next() { goTo(cur.children[0]); }
function toStart() { goTo(root); }
function toEnd() { let n = cur; while (n.children.length) n = n.children[0]; goTo(n); }
function mainAncestor(n) { while (!n.main) n = n.parent; return n; }
function backToGame() { goTo(mainAncestor(cur)); }
function deleteLine() {
  // remove the whole branch you're in, from where it left the game
  let n = cur;
  while (!n.parent.main) n = n.parent;
  const branchPoint = n.parent;
  branchPoint.children = branchPoint.children.filter(c => c !== n);
  cur = branchPoint;
  update();
}

$('#startBtn').onclick = toStart;
$('#backBtn').onclick = back;
$('#nextBtn').onclick = next;
$('#endBtn').onclick = toEnd;
$('#flipBtn').onclick = () => { cg.toggleOrientation(); updateEvalBarSide(); drawOverlay(); };
$('#backToGameBtn').onclick = backToGame;
$('#deleteLineBtn').onclick = deleteLine;
$('#showArrows').onchange = () => drawEngineArrows();

document.addEventListener('keydown', e => {
  if (e.target.tagName === 'SELECT' || e.target.tagName === 'INPUT') return;
  if (e.key === 'ArrowLeft') { back(); e.preventDefault(); }
  if (e.key === 'ArrowRight') { next(); e.preventDefault(); }
  if (e.key === 'Home' || e.key === 'ArrowUp') { toStart(); e.preventDefault(); }
  if (e.key === 'End' || e.key === 'ArrowDown') { toEnd(); e.preventDefault(); }
  if (e.key === 'f') { cg.toggleOrientation(); updateEvalBarSide(); drawOverlay(); }
});

// ---------- Rendering ----------
function update() {
  const chess = new Chess(cur.fen);
  const turn = chess.turn() === 'w' ? 'white' : 'black';
  cg.set({
    fen: cur.fen,
    turnColor: turn,
    lastMove: cur.from ? [cur.from, cur.to] : undefined,
    check: chess.inCheck(),
    movable: { color: chess.isGameOver() ? undefined : turn, dests: legalDests(chess) },
  });
  engineShapes = [];
  drawOverlay();
  historyTab = null;  // pick the history tab automatically for each position

  renderMoveList();
  renderStatus(chess);
  renderReview();
  renderVariation();
  renderOpening();

  // stop the old search right away; start the new one after a tiny pause so
  // holding down the arrow keys doesn't start dozens of searches
  stopAnalysis();
  hoveredLine = undefined;
  $('#lines').innerHTML = '<span class="muted">Thinking...</span>';
  $('#depth').textContent = '';
  clearTimeout(analysisTimer);
  analysisTimer = setTimeout(() => { requestAnalysis(); requestHistory(); }, 40);
}

// ---------- Your games from this position ----------
let history = null;         // last result from the server
let historyTab = null;      // 'mine' | 'opponents' (null = pick automatically)
let historyId = 0;

function myColorHere() {
  return gameData ? gameData.my_color : cg.state.orientation;
}

async function requestHistory() {
  const id = ++historyId;
  const node = cur;
  try {
    const res = await fetch('/api/position-stats?fen=' + encodeURIComponent(node.fen));
    const data = await res.json();
    if (id !== historyId || node !== cur) return;
    history = data;
    renderHistory();
  } catch (e) {
    if (id === historyId) $('#historyBox').textContent = 'Could not load your games for this position.';
  }
}

function renderHistory() {
  if (!history || history.error) return;
  // default tab: your moves when it's your turn, otherwise your opponents'
  const tab = historyTab || (history.to_move === myColorHere() ? 'mine' : 'opponents');
  const rows = history[tab] || [];
  const total = rows.reduce((n, m) => n + m.games, 0);
  $('#tabMine').textContent = `Your moves (${(history.mine || []).reduce((n, m) => n + m.games, 0)})`;
  $('#tabOpp').textContent = `Opponents' moves (${(history.opponents || []).reduce((n, m) => n + m.games, 0)})`;
  $('#tabMine').classList.toggle('active', tab === 'mine');
  $('#tabOpp').classList.toggle('active', tab === 'opponents');

  if (!rows.length) {
    $('#historyBox').innerHTML = tab === 'mine'
      ? "You haven't had this position with this side to move."
      : 'No opponent has played a move against you from this position.';
    return;
  }
  const pct = (n, d) => Math.round((100 * n) / d);
  const seg = (cls, n, games) => {
    const p = pct(n, games);
    return p ? `<span class="${cls}" style="width:${p}%">${p >= 12 ? p + '%' : ''}</span>` : '';
  };
  const body = rows.map(m => `
    <tr data-uci="${m.uci || ''}">
      <td class="mvname">${m.move}</td>
      <td class="num">${m.games}<span class="muted"> (${pct(m.games, total)}%)</span></td>
      <td><div class="wdl" title="${m.win} won, ${m.draw} drawn, ${m.loss} lost">
        ${seg('w', m.win, m.games)}${seg('d', m.draw, m.games)}${seg('l', m.loss, m.games)}</div></td>
      <td>${m.verdict ? `<span class="verdict ${m.verdict}">${m.verdict}</span>` : '<span class="muted">-</span>'}</td>
      <td class="muted">${(m.last || '').replaceAll('.', '-')}</td>
    </tr>`).join('');
  $('#historyBox').innerHTML = `<table class="history">
    <thead><tr><th>Move</th><th>Games</th><th>Your results (W / D / L)</th>
    <th title="Stockfish's average verdict on this move, from your analyzed games">Engine</th>
    <th>Last played</th></tr></thead><tbody>${body}</tbody></table>`;
  $('#historyBox').querySelectorAll('tbody tr').forEach(tr => {
    tr.onclick = () => {
      const u = tr.dataset.uci;
      if (u) playMove(u.slice(0, 2), u.slice(2, 4), u[4]);
    };
  });
}

$('#tabMine').onclick = () => { historyTab = 'mine'; renderHistory(); };
$('#tabOpp').onclick = () => { historyTab = 'opponents'; renderHistory(); };

function moveNumber(node, force) {
  if (node.ply % 2 === 1) return `<span class="num">${(node.ply + 1) / 2}.</span>`;
  return force ? `<span class="num">${node.ply / 2}...</span>` : '';
}

function moveSpan(node, force) {
  let cls = 'mv', sym = '';
  if (node.main && node.gameIndex !== null) {
    const r = reviews[node.gameIndex];
    if (r && r.label) {
      cls += ' ' + r.label;
      if (['Inaccuracy', 'Mistake', 'Blunder', 'Best'].includes(r.label))
        sym = `<span class="sym">${SYMBOLS[r.label]}</span>`;
    }
  }
  if (node === cur) cls += ' current';
  return `${moveNumber(node, force)}<span class="${cls}" data-id="${node.id}">${node.san}${sym}</span>`;
}

// PGN-style move list: 1. e4 e5 2. Nf3 (2. Bc4 Nf6) 2... Nc6 ...
function renderFrom(node, forceNumber) {
  let html = '';
  let n = node;
  while (n.children.length) {
    const [mainChild, ...alts] = n.children;
    html += ' ' + moveSpan(mainChild, forceNumber);
    forceNumber = false;
    for (const alt of alts) {
      html += ` <span class="var">(${moveSpan(alt, true)}${renderFrom(alt, false)})</span>`;
      forceNumber = true;
    }
    n = mainChild;
  }
  return html;
}

function renderMoveList() {
  const box = $('#moves');
  if (!root.children.length) {
    box.innerHTML = '<span class="muted">Drag a piece to start analyzing.</span>';
    return;
  }
  box.innerHTML = renderFrom(root, true);
  box.querySelectorAll('.mv').forEach(el => {
    el.onclick = () => goTo(nodes[el.dataset.id]);
  });
  // keep the current move visible by scrolling only the move list, not the page
  const current = box.querySelector('.mv.current');
  if (current) {
    const top = current.offsetTop;
    if (top < box.scrollTop) box.scrollTop = top - 8;
    else if (top + current.offsetHeight > box.scrollTop + box.clientHeight)
      box.scrollTop = top + current.offsetHeight - box.clientHeight + 8;
  }
}

function renderStatus(chess) {
  let status = chess.turn() === 'w' ? 'White to move' : 'Black to move';
  if (chess.isCheckmate()) status = 'Checkmate';
  else if (chess.isDraw()) status = 'Draw';
  if (gameData) status += ` | ${gameData.date} | ${gameData.opening || ''} | Result ${gameData.result}`;
  $('#status').textContent = status;
}

function renderVariation() {
  if (!gameData || cur.main) { $('#variationBox').style.display = 'none'; return; }
  const line = [];
  let n = cur;
  while (!n.main) { line.unshift(n); n = n.parent; }
  $('#variationText').innerHTML = line.map((m, i) => moveSpan(m, i === 0)).join(' ');
  $('#variationText').querySelectorAll('.mv').forEach(el => {
    el.onclick = () => goTo(nodes[el.dataset.id]);
  });
  $('#variationBox').style.display = 'block';
}

function renderReview() {
  const box = $('#reviewBox');
  if (!gameData || !cur.main || cur === root) { box.innerHTML = ''; return; }
  const i = cur.gameIndex;
  const r = reviews[i];
  const move = gameData.moves[i].san;
  const mover = i % 2 === 0 ? 'white' : 'black';
  const who = mover === gameData.my_color ? 'You' : 'Opponent';
  if (!r) {
    box.innerHTML = `<div class="review"><div>${who} played <b>${move}</b>. `
      + `<span class="muted">This game hasn't been analyzed yet.</span></div></div>`;
    return;
  }
  if (!r.label) {
    box.innerHTML = `<div class="review"><div>${who} played <b>${move}</b>, the final move.</div></div>`;
    return;
  }
  let text;
  if (r.label === 'Best') {
    text = `<b>${move}</b> is the best move.`;
  } else {
    const lost = r.drop >= 10 ? ` It lost ${r.drop}% win chance.` : '';
    const verdict = ['Excellent', 'Good'].includes(r.label)
      ? r.label.toLowerCase()
      : (r.label === 'Inaccuracy' ? 'an inaccuracy' : 'a ' + r.label.toLowerCase());
    text = `<b>${move}</b> is ${verdict}.${lost} The best move was <b>${r.best}</b>.`
         + `<br><button id="showBestBtn">Show best move</button>`;
  }
  box.innerHTML = `<div class="review"><span class="badge ${r.label}">${SYMBOLS[r.label]}</span>`
    + `<div><span class="muted">${who}</span><br>${text}</div></div>`;
  const btn = $('#showBestBtn');
  if (btn) btn.onclick = () => playSan(cur.parent, r.best);
}

function renderSummary() {
  const hasReview = gameData && reviews.some(r => r && r.label);
  if (!hasReview) { $('#summaryBox').style.display = 'none'; return; }
  const w = gameData.summary.white, b = gameData.summary.black;
  const you = c => (gameData.my_color === c ? ' (you)' : '');
  const acc = a => (a === null ? '-' : a.toFixed(1));
  let rows = `<tr><th></th><th>${gameData.white}${you('white')}</th><th>${gameData.black}${you('black')}</th></tr>`;
  rows += `<tr><td>Accuracy</td><td><b>${acc(w.accuracy)}</b></td><td><b>${acc(b.accuracy)}</b></td></tr>`;
  for (const l of LABELS) {
    rows += `<tr><td><span class="badge mini ${l}">${SYMBOLS[l]}</span>${l}</td>`
          + `<td>${w.counts[l] || 0}</td><td>${b.counts[l] || 0}</td></tr>`;
  }
  $('#summary').innerHTML = `<table class="summary">${rows}</table>`;
  $('#summaryBox').style.display = 'block';
}

// ---------- Engine ----------
function winPct(cp) {
  return 50 + 50 * (2 / (1 + Math.exp(-0.00368208 * cp)) - 1);
}

function formatScore(line) {
  if (line.mate !== null) return (line.mate > 0 ? '+M' : '-M') + Math.abs(line.mate);
  return (line.cp >= 0 ? '+' : '') + (line.cp / 100).toFixed(2);
}

function arrowFor(uci, color) {
  return { from: uci.slice(0, 2), to: uci.slice(2, 4), color };
}

function drawEngineArrows(hoverIndex) {
  const shapes = [];
  if ($('#showArrows').checked && engineLines[0]) shapes.push(arrowFor(engineLines[0].first_uci, 'engine'));
  if (hoverIndex !== undefined && hoverIndex > 0 && engineLines[hoverIndex])
    shapes.push(arrowFor(engineLines[hoverIndex].first_uci, 'engineHover'));
  if (hoverIndex === 0 && !$('#showArrows').checked) shapes.push(arrowFor(engineLines[0].first_uci, 'engine'));
  engineShapes = shapes;
  drawOverlay();
}

function updateEvalBarSide() {
  $('#evalBar').classList.toggle('flipped', cg.state.orientation === 'black');
}

// Live analysis: the server streams Stockfish's lines as it searches deeper.
let stream = null;
let hoveredLine;

function stopAnalysis() {
  if (stream) { stream.close(); stream = null; }
}

function requestAnalysis() {
  stopAnalysis();
  const node = cur;
  const es = new EventSource('/api/analyze/stream?fen=' + encodeURIComponent(node.fen));
  stream = es;
  let gotAny = false;
  es.onmessage = ev => {
    if (es !== stream || node !== cur) { es.close(); return; }
    const data = JSON.parse(ev.data);
    gotAny = true;
    if (data.game_over) {
      es.close();
      engineLines = [];
      engineShapes = [];
      drawOverlay();
      $('#depth').textContent = '';
      $('#lines').textContent = 'Game over: ' + data.game_over;
      return;
    }
    if (data.done) es.close();  // finished searching; don't let the browser reconnect
    showLines(data.lines || [], data.depth, data.done);
  };
  es.onerror = () => {
    es.close();
    if (es === stream && !gotAny)
      $('#lines').textContent = 'Could not reach the server. Is server.py running?';
  };
}

function showLines(lines, depth, done) {
  engineLines = lines;
  $('#depth').textContent = depth ? `depth ${depth}${done ? '' : '...'}` : '';
  const box = $('#lines');
  box.innerHTML = engineLines.map((l, i) =>
    `<div class="line" data-i="${i}"><span class="score">${formatScore(l)}</span>${l.san}</div>`
  ).join('');
  box.querySelectorAll('.line').forEach(el => {
    const i = Number(el.dataset.i);
    el.onclick = () => {
      const u = engineLines[i].first_uci;
      playMove(u.slice(0, 2), u.slice(2, 4), u[4]);
    };
    el.onmouseenter = () => { hoveredLine = i; drawEngineArrows(i); };
    el.onmouseleave = () => { hoveredLine = undefined; drawEngineArrows(); };
  });
  drawEngineArrows(hoveredLine);

  const top = engineLines[0];
  if (top) {
    const pct = top.mate !== null ? (top.mate > 0 ? 100 : 0) : winPct(top.cp);
    $('#evalWhite').style.height = pct + '%';
    $('#evalText').textContent = formatScore(top).replace('+', '');
  }
}

// ---------- Openings ----------
let openingNames = {};    // position -> {name, eco, family}
let studyLine = null;     // the opening line you loaded from the explorer
let explorerState = { view: 'families', family: null };
let familiesCache = null;
const posKey = fen => fen.split(' ').slice(0, 4).join(' ');

function currentOpening() {
  // the deepest named position on the way to the current one
  for (let n = cur; n; n = n.parent) {
    const o = openingNames[posKey(n.fen)];
    if (o) return { ...o, key: posKey(n.fen) };
  }
  return null;
}

function renderOpening() {
  const box = $('#openingName');
  const o = currentOpening();
  let html = o
    ? `<a id="openingLink">${o.name}</a><span class="eco">${o.eco}</span>`
    : (cur && cur.ply > 0 ? '<span class="muted">Unnamed position</span>' : '');
  if (studyLine) {
    html += `<div class="studyBar muted">Studying this line. Press Start, then Next, to step through it.
      <a id="backToFamily">See all ${studyLine.family} lines</a></div>`;
  }
  box.innerHTML = html;
  const link = $('#openingLink');
  if (link) link.onclick = () => openExplorer(o.family);
  const back = $('#backToFamily');
  if (back) back.onclick = () => openExplorer(studyLine.family);
}

function wdlBar(m) {
  if (!m.games) return '';
  const pct = n => Math.round((100 * n) / m.games);
  return `<div class="wdl" title="${m.win} won, ${m.draw} drawn, ${m.loss} lost">`
    + (m.win ? `<span class="w" style="width:${pct(m.win)}%"></span>` : '')
    + (m.draw ? `<span class="d" style="width:${pct(m.draw)}%"></span>` : '')
    + (m.loss ? `<span class="l" style="width:${pct(m.loss)}%"></span>` : '') + '</div>';
}

function opRow(r, opts = {}) {
  const tree = opts.depth ? '<span class="opTree">\u2514</span>' : '';
  const indent = Math.min(opts.depth || 0, 7) * 14;
  const moves = opts.depth ? `<b>${r.new}</b>` : r.moves;
  const games = r.mine.games
    ? `You: ${r.mine.games} game${r.mine.games > 1 ? 's' : ''}${wdlBar(r.mine)}`
    : '<span style="opacity:.6">Not in your games</span>';
  return `<div class="opRow${opts.here ? ' here' : ''}" data-i="${opts.index}" style="padding-left:${6 + indent}px">
    <div class="opMain">
      <div class="opName">${tree}${opts.title}</div>
      <div class="opMoves" title="${r.moves}">${r.eco ? r.eco + ' \u00b7 ' : ''}${moves}</div>
    </div>
    <div class="opMeta">${opts.extra || ''}${games}</div>
  </div>`;
}

function setExplorerOpen(open) {
  $('#explorerView').style.display = open ? 'block' : 'none';
  $('#analysisView').style.display = open ? 'none' : 'block';
  $('#openingsBtn').classList.toggle('active', open);
}

async function openExplorer(family) {
  setExplorerOpen(true);
  if (family) await showFamily(family);
  else await showFamilies();
}

async function showFamilies() {
  explorerState = { view: 'families', family: null };
  $('#explorerTitle').textContent = 'Opening explorer';
  $('#explorerNav').innerHTML = '<span class="muted">All openings, the ones you play most first.</span>';
  $('#openingSearch').style.display = 'block';
  const list = $('#explorerList');
  if (!familiesCache) {
    list.innerHTML = '<span class="muted">Loading openings...</span>';
    familiesCache = await (await fetch('/api/openings/families')).json();
  }
  renderFamilyList();
}

function renderFamilyList() {
  const q = $('#openingSearch').value.trim().toLowerCase();
  const fams = familiesCache.filter(f => !q || q.split(/\s+/).every(w => f.family.toLowerCase().includes(w)));
  const list = $('#explorerList');
  list.innerHTML = fams.map((f, i) => opRow(f, {
    index: i, title: f.family,
    extra: `<div>${f.variations} line${f.variations > 1 ? 's' : ''}</div>`,
  })).join('') + (q ? '<div id="variationHits"></div>' : '');
  list.querySelectorAll('.opRow').forEach(el => {
    el.onclick = () => showFamily(fams[Number(el.dataset.i)].family);
  });
  if (q) searchVariations(q, fams.length);
}

let searchId = 0;
async function searchVariations(q, familyCount) {
  const id = ++searchId;
  const hits = await (await fetch('/api/openings/search?q=' + encodeURIComponent(q))).json();
  if (id !== searchId || !$('#variationHits')) return;
  if (!hits.length) {
    if (!familyCount) $('#variationHits').innerHTML = '<div class="muted">No openings match that.</div>';
    return;
  }
  $('#variationHits').innerHTML = '<h2 style="margin-top:14px">Matching lines</h2>'
    + hits.map((h, i) => opRow(h, { index: i, title: h.name })).join('');
  $('#variationHits').querySelectorAll('.opRow').forEach(el => {
    el.onclick = () => loadLine(hits[Number(el.dataset.i)]);
  });
}
$('#openingSearch').oninput = () => { if (explorerState.view === 'families' && familiesCache) renderFamilyList(); };

async function showFamily(name) {
  explorerState = { view: 'family', family: name };
  $('#explorerTitle').textContent = name;
  $('#explorerNav').innerHTML = '<a id="allOpenings">\u2190 All openings</a>';
  $('#allOpenings').onclick = showFamilies;
  $('#openingSearch').style.display = 'none';
  const list = $('#explorerList');
  list.innerHTML = '<span class="muted">Loading lines...</span>';
  const data = await (await fetch('/api/openings/family?name=' + encodeURIComponent(name))).json();
  if (explorerState.family !== name) return;
  const here = currentOpening();
  list.innerHTML = '<div class="muted" style="margin-bottom:6px">Click a line to load it on the board.</div>'
    + data.lines.map((r, i) => opRow(r, {
      index: i, depth: r.depth, here: here && here.key === r.key,
      title: r.variation,
    })).join('');
  list.querySelectorAll('.opRow').forEach(el => {
    el.onclick = () => loadLine(data.lines[Number(el.dataset.i)]);
  });
  // scroll the list (not the page) to the line you're on
  const hereRow = list.querySelector('.opRow.here');
  if (hereRow) list.scrollTop = hereRow.offsetTop - list.clientHeight / 2;
}

// Load an opening line onto the board as something to study
function loadLine(line) {
  const chess = new Chess();
  const moves = [];
  for (const san of line.san) {
    const m = chess.move(san);
    moves.push({ san: m.san, from: m.from, to: m.to, promotion: m.promotion, fen: chess.fen() });
  }
  gameData = null;
  reviews = [];
  studyLine = { name: line.name, family: line.family || line.name.split(':')[0] };
  $('#gameSelect').value = '';
  buildTree(START_FEN, moves);
  Object.values(nodes).forEach(n => { n.gameIndex = null; });
  let end = root;
  while (end.children.length) end = end.children[0];
  cur = end;
  $('#gameTitle').textContent = line.name;
  renderSummary();
  setExplorerOpen(false);
  update();
}

$('#openingsBtn').onclick = () => {
  if ($('#explorerView').style.display === 'block') { setExplorerOpen(false); return; }
  const o = currentOpening();
  openExplorer(o ? o.family : null);
};
$('#closeExplorerBtn').onclick = () => setExplorerOpen(false);

// ---------- Loading games ----------
async function loadGames() {
  const analyzed = $('#analyzedOnly').checked ? 1 : 0;
  const games = await (await fetch('/api/games?analyzed=' + analyzed)).json();
  const options = ['<option value="">Free analysis (starting position)</option>']
    .concat(games.map(g => `<option value="${g.id}">${g.label}</option>`));
  $('#gameSelect').innerHTML = options.join('');
}

async function loadGame(id) {
  historyTab = null;
  studyLine = null;
  if (!id) {
    gameData = null;
    reviews = [];
    buildTree(START_FEN, []);
    cg.set({ orientation: 'white' });
    $('#gameTitle').textContent = 'Free analysis';
  } else {
    gameData = await (await fetch('/api/game/' + id)).json();
    reviews = gameData.reviews || [];
    buildTree(gameData.start_fen, gameData.moves);
    cg.set({ orientation: gameData.my_color });
    $('#gameTitle').textContent = `${gameData.white} vs ${gameData.black}`;
  }
  updateEvalBarSide();
  renderSummary();
  update();
}

$('#gameSelect').onchange = e => loadGame(e.target.value);
$('#analyzedOnly').onchange = loadGames;

// ---------- Syncing new games ----------
async function refreshGameList() {
  const selected = $('#gameSelect').value;
  await loadGames();
  if ([...$('#gameSelect').options].some(o => o.value === selected)) $('#gameSelect').value = selected;
}

async function syncGames() {
  const btn = $('#syncBtn'), status = $('#syncStatus');
  btn.disabled = true;
  status.textContent = 'Checking chess.com...';
  try {
    const data = await (await fetch('/api/sync', { method: 'POST' })).json();
    if (data.error) status.textContent = data.error;
    else if (data.busy) status.textContent = 'A sync is already running';
    else {
      status.textContent = data.new
        ? `${data.new} new game${data.new > 1 ? 's' : ''} found`
        : 'Up to date with chess.com';
      if (data.new) await refreshGameList();
      setTimeout(pollSync, 1500);
    }
  } catch (e) {
    status.textContent = 'Sync failed. Is server.py running?';
  }
  btn.disabled = false;
}

async function pollSync() {
  const s = await (await fetch('/api/sync/status')).json();
  if (s.message) $('#syncStatus').textContent = s.message;
  if (s.running) setTimeout(pollSync, 3000);
  else await refreshGameList();
}
$('#syncBtn').onclick = syncGames;

// ---------- Start ----------
fetch('/api/openings/names').then(r => r.json()).then(n => { openingNames = n; renderOpening(); });
await loadGames();
await loadGame('');
syncGames();
