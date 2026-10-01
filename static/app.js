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
  drawable: {
    enabled: true,
    brushes: {
      engine: { key: 'e', color: '#3b82c4', opacity: 0.8, lineWidth: 12 },
      engineHover: { key: 'eh', color: '#3b82c4', opacity: 0.45, lineWidth: 12 },
      green: { key: 'g', color: '#15781B', opacity: 1, lineWidth: 10 },
      red: { key: 'r', color: '#882020', opacity: 1, lineWidth: 10 },
      blue: { key: 'b', color: '#003088', opacity: 1, lineWidth: 10 },
      yellow: { key: 'y', color: '#e68f00', opacity: 1, lineWidth: 10 },
    },
    onChange: shapes => { if (cur) cur.shapes = shapes; },
  },
});
window.addEventListener('resize', () => cg.redrawAll());

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
$('#flipBtn').onclick = () => { cg.toggleOrientation(); updateEvalBarSide(); };
$('#backToGameBtn').onclick = backToGame;
$('#deleteLineBtn').onclick = deleteLine;
$('#showArrows').onchange = () => drawEngineArrows();

document.addEventListener('keydown', e => {
  if (e.target.tagName === 'SELECT' || e.target.tagName === 'INPUT') return;
  if (e.key === 'ArrowLeft') { back(); e.preventDefault(); }
  if (e.key === 'ArrowRight') { next(); e.preventDefault(); }
  if (e.key === 'Home' || e.key === 'ArrowUp') { toStart(); e.preventDefault(); }
  if (e.key === 'End' || e.key === 'ArrowDown') { toEnd(); e.preventDefault(); }
  if (e.key === 'f') { cg.toggleOrientation(); updateEvalBarSide(); }
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
  cg.setShapes(cur.shapes || []);
  cg.setAutoShapes([]);

  renderMoveList();
  renderStatus(chess);
  renderReview();
  renderVariation();

  clearTimeout(analysisTimer);
  $('#lines').innerHTML = '<span class="muted">Thinking...</span>';
  analysisTimer = setTimeout(requestAnalysis, 200);
}

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
  const current = box.querySelector('.mv.current');
  if (current) current.scrollIntoView({ block: 'nearest' });
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

function arrowFor(uci, brush) {
  return { orig: uci.slice(0, 2), dest: uci.slice(2, 4), brush };
}

function drawEngineArrows(hoverIndex) {
  const shapes = [];
  if ($('#showArrows').checked && engineLines[0]) shapes.push(arrowFor(engineLines[0].first_uci, 'engine'));
  if (hoverIndex !== undefined && hoverIndex > 0 && engineLines[hoverIndex])
    shapes.push(arrowFor(engineLines[hoverIndex].first_uci, 'engineHover'));
  if (hoverIndex === 0 && !$('#showArrows').checked) shapes.push(arrowFor(engineLines[0].first_uci, 'engine'));
  cg.setAutoShapes(shapes);
}

function updateEvalBarSide() {
  $('#evalBar').classList.toggle('flipped', cg.state.orientation === 'black');
}

async function requestAnalysis() {
  const id = ++analysisId;
  const node = cur;
  let data;
  try {
    const res = await fetch('/api/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ fen: node.fen }),
    });
    data = await res.json();
  } catch (e) {
    if (id === analysisId) $('#lines').textContent = 'Could not reach the server. Is server.py running?';
    return;
  }
  if (id !== analysisId || node !== cur) return;  // you've moved on since

  if (data.game_over) {
    engineLines = [];
    cg.setAutoShapes([]);
    $('#lines').textContent = 'Game over: ' + data.game_over;
    return;
  }
  engineLines = data.lines || [];
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
    el.onmouseenter = () => drawEngineArrows(i);
    el.onmouseleave = () => drawEngineArrows();
  });
  drawEngineArrows();

  const top = engineLines[0];
  if (top) {
    const pct = top.mate !== null ? (top.mate > 0 ? 100 : 0) : winPct(top.cp);
    $('#evalWhite').style.height = pct + '%';
    $('#evalText').textContent = formatScore(top).replace('+', '');
  }
}

// ---------- Loading games ----------
async function loadGames() {
  const analyzed = $('#analyzedOnly').checked ? 1 : 0;
  const games = await (await fetch('/api/games?analyzed=' + analyzed)).json();
  const options = ['<option value="">Free analysis (starting position)</option>']
    .concat(games.map(g => `<option value="${g.id}">${g.label}</option>`));
  $('#gameSelect').innerHTML = options.join('');
}

async function loadGame(id) {
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
await loadGames();
await loadGame('');
syncGames();
