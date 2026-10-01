// Everything the board needs from the outside world, in one place.
// This version talks to server.py. The public demo swaps in api-demo.js,
// which reads exported JSON files and runs Stockfish in the browser.

const getJson = async url => (await fetch(url)).json();
const q = encodeURIComponent;

export const api = {
  mode: 'local',

  games: analyzedOnly => getJson('/api/games?analyzed=' + (analyzedOnly ? 1 : 0)),
  game: id => getJson('/api/game/' + id),
  positionStats: fen => getJson('/api/position-stats?fen=' + q(fen)),

  openingNames: () => getJson('/api/openings/names'),
  openingFamilies: () => getJson('/api/openings/families'),
  openingFamily: name => getJson('/api/openings/family?name=' + q(name)),
  openingSearch: text => getJson('/api/openings/search?q=' + q(text)),

  sync: async () => (await fetch('/api/sync', { method: 'POST' })).json(),
  syncStatus: () => getJson('/api/sync/status'),

  // Live engine. Calls onUpdate({lines, depth, done}) as the search deepens,
  // onUpdate({game_over}) at the end of a game, or onError() if the server is down.
  // Returns a function that stops the search.
  analyze(fen, onUpdate, onError) {
    const es = new EventSource('/api/analyze/stream?fen=' + q(fen));
    let gotAny = false, stopped = false;
    es.onmessage = ev => {
      if (stopped) return;
      const data = JSON.parse(ev.data);
      gotAny = true;
      if (data.done || data.game_over) es.close();  // don't let the browser reconnect
      onUpdate(data);
    };
    es.onerror = () => {
      es.close();
      if (!stopped && !gotAny) onError('Could not reach the server. Is server.py running?');
    };
    return () => { stopped = true; es.close(); };
  },
};
