// Demo version of api.js, for the public static site (GitHub Pages).
// There's no server: game data comes from JSON files made by export_demo.py,
// and Stockfish runs in the visitor's browser as WebAssembly.
import { Chess } from './vendor/chess.js';

const getJson = async path => {
  const res = await fetch('./data/' + path);
  if (!res.ok) throw new Error(res.status + ' ' + path);
  return res.json();
};
const posKey = fen => fen.split(' ').slice(0, 4).join(' ');

// Position stats are split into 256 files by a hash of the position,
// so the browser only downloads the small file it needs.
function shardOf(key) {
  let h = 0x811c9dc5;  // FNV-1a, same as export_demo.py
  for (const byte of new TextEncoder().encode(key)) {
    h ^= byte;
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return (h & 0xff).toString(16).padStart(2, '0');
}
const shardCache = {};
const loadShard = id => (shardCache[id] ||= getJson(`stats/${id}.json`).catch(() => ({})));

let openingsData = null;
const loadOpenings = () => (openingsData ||= getJson('openings.json'));

// ---------- Stockfish in a Web Worker ----------
const ENGINE = './vendor/stockfish/stockfish-19-lite-single.js';
const MAX_DEPTH = 22, MAX_MS = 10000, NUM_LINES = 3;
let worker = null, engineFailed = false;
let running = null;   // the search the engine is working on
let queued = null;    // the search to start once the engine has stopped

function startWorker() {
  if (worker || engineFailed) return;
  try {
    worker = new Worker(ENGINE);
  } catch (e) {
    engineFailed = true;
    return;
  }
  worker.onmessage = e => onEngineLine(String(e.data));
  worker.onerror = () => {
    engineFailed = true;
    for (const job of [running, queued]) if (job && !job.cancelled) job.onError('The engine could not start in this browser.');
  };
  worker.postMessage('uci');
  worker.postMessage(`setoption name MultiPV value ${NUM_LINES}`);
  worker.postMessage('setoption name Hash value 32');
  worker.postMessage('isready');
}

function begin(job) {
  running = job;
  worker.postMessage('position fen ' + job.fen);
  worker.postMessage(`go depth ${MAX_DEPTH} movetime ${MAX_MS}`);
}

function pvToSan(fen, uciMoves) {
  const chess = new Chess(fen);
  const parts = [];
  for (const [i, u] of uciMoves.slice(0, 10).entries()) {
    let m;
    try { m = chess.move({ from: u.slice(0, 2), to: u.slice(2, 4), promotion: u[4] }); } catch (e) { break; }
    const n = chess.moveNumber();
    if (m.color === 'w') parts.push(`${n}. ${m.san}`);
    else parts.push(i === 0 ? `${n - 1}... ${m.san}` : m.san);
  }
  return parts.join(' ');
}

function onEngineLine(line) {
  const job = running;
  if (line.startsWith('bestmove')) {
    if (job && !job.cancelled) {
      job.flush(true);
    }
    running = null;
    if (queued) { const next = queued; queued = null; begin(next); }
    return;
  }
  if (!job || job.cancelled || !line.startsWith('info ') || !line.includes(' pv ')) return;
  if (line.includes(' lowerbound') || line.includes(' upperbound')) return;
  const t = line.split(' ');
  const get = name => t[t.indexOf(name) + 1];
  const depth = Number(get('depth'));
  const multipv = Number(t.includes('multipv') ? get('multipv') : 1);
  const scoreType = get('score'), raw = Number(t[t.indexOf('score') + 2]);
  const pv = t.slice(t.indexOf('pv') + 1);
  // Stockfish scores from the side to move; the board wants White's point of view
  const sign = job.whiteToMove ? 1 : -1;
  job.lines[multipv] = {
    cp: scoreType === 'cp' ? sign * raw : null,
    mate: scoreType === 'mate' ? sign * raw : null,
    depth, first_uci: pv[0], san: pvToSan(job.fen, pv),
  };
  job.flush(false);
}

export const api = {
  mode: 'demo',

  games: async () => getJson('games.json'),
  game: id => getJson(`games/${id}.json`),
  async positionStats(fen) {
    const key = posKey(fen);
    const shard = await loadShard(shardOf(key));
    const toMove = fen.split(' ')[1] === 'w' ? 'white' : 'black';
    const s = shard[key];
    return { to_move: toMove, mine: s ? s.mine : [], opponents: s ? s.opponents : [] };
  },

  openingNames: () => getJson('opening-names.json'),
  async openingFamilies() { return (await loadOpenings()).families; },
  async openingFamily(name) {
    const lines = (await loadOpenings()).lines[name];
    return lines ? { family: name, lines } : null;
  },
  async openingSearch(text) {
    const words = text.toLowerCase().split(/\s+/).filter(Boolean);
    if (!words.length) return [];
    const all = Object.values((await loadOpenings()).lines).flat();
    return all.filter(l => words.every(w => l.name.toLowerCase().includes(w)))
      .sort((a, b) => a.san.length - b.san.length || a.name.localeCompare(b.name))
      .slice(0, 60);
  },

  sync: async () => ({ new: 0 }),
  syncStatus: async () => ({ running: false, message: '' }),

  analyze(fen, onUpdate, onError) {
    const chess = new Chess(fen);
    if (chess.isGameOver()) {
      const result = chess.isCheckmate() ? (chess.turn() === 'w' ? '0-1' : '1-0') : '1/2-1/2';
      setTimeout(() => onUpdate({ game_over: result }), 0);
      return () => {};
    }
    startWorker();
    if (engineFailed) {
      setTimeout(() => onError('The engine could not start in this browser.'), 0);
      return () => {};
    }
    let lastSent = 0, lastDepth = 0;
    const job = {
      fen, whiteToMove: chess.turn() === 'w', lines: {}, cancelled: false, onError,
      flush(done) {
        if (!job.lines[1]) return;
        const depth = job.lines[1].depth, now = performance.now();
        if (!done && depth <= lastDepth && now - lastSent < 120) return;
        lastSent = now; lastDepth = depth;
        const lines = Object.keys(job.lines).sort((a, b) => a - b).map(k => job.lines[k]);
        onUpdate({ lines, depth, done });
      },
    };
    // Only one search at a time: stop the current one, then start this one
    if (running) {
      if (queued) queued.cancelled = true;
      queued = job;
      running.cancelled = true;
      worker.postMessage('stop');
    } else {
      begin(job);
    }
    return () => {
      job.cancelled = true;
      if (running === job) worker.postMessage('stop');
      if (queued === job) queued = null;
    };
  },
};
