'use strict';
// Persistent player ownership and the game's native Steam identity are separate.
const STEAM = /^\d{17}$/;
const PLAYER = /^(?:\d{17}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/;
const validSteam = value => typeof value === 'string' && STEAM.test(value);
const validPlayer = value => typeof value === 'string' && PLAYER.test(value);
const playerOf = row => row?.player_id || row?.steam_id || '';
const gameOf = row => row?.game_steam_id || (validSteam(row?.steam_id) ? row.steam_id : '') ||
  (!Object.hasOwn(row || {}, 'player_id') && validSteam(row?.steamId) ? row.steamId : '');

// Old stored records carry only steam_id. Never infer a new profile's game
// identity from its player_id, even when that profile originated on Steam.
function hydrate(value, seen = new WeakSet()) {
  if (!value || typeof value !== 'object') return value;
  if(seen.has(value))return value;
  seen.add(value);
  if (Array.isArray(value)) { value.forEach(item=>hydrate(item,seen)); return value; }
  if (Object.getPrototypeOf(value) !== Object.prototype && Object.getPrototypeOf(value) !== null) return value;
  if (!value.player_id && validSteam(value.steam_id)) value.player_id = value.steam_id;
  for (const child of Object.values(value)) hydrate(child,seen);
  return value;
}
function freezeMatch(match) {
  hydrate(match);
  if (match.game_bindings) {
    const rows = [...(match.players || []), ...(match.left || [])];
    if (!validBindings(match) || rows.length !== Object.keys(match.game_bindings).length ||
        new Set(rows.map(playerOf)).size !== rows.length ||
        rows.some(row => gameFor(match, playerOf(row)) !== gameOf(row)))
      throw new Error('Invalid saved game identity bindings');
    match.game_bindings = Object.freeze({...match.game_bindings});
    return match;
  }
  const bindings = {}, seen = new Set();
  for (const row of [...(match.players || []), ...(match.left || [])]) {
    const player = playerOf(row), game = gameOf(row);
    if (!validPlayer(player) || !validSteam(game) || seen.has(game) || Object.hasOwn(bindings, player))
      return match; // Non-native legacy fixtures may still be used by pure calculations.
    bindings[player] = game; seen.add(game);
    row.game_steam_id = game;
    row.steam_id = game;
  }
  if (seen.size) match.game_bindings = Object.freeze(bindings);
  return match;
}
function validBindings(match) {
  const bindings = match?.game_bindings;
  if (!bindings || typeof bindings !== 'object') return false;
  const entries = Object.entries(bindings);
  return entries.length > 0 && entries.every(([p,g]) => validPlayer(p) && validSteam(g)) &&
    new Set(entries.map(([,g]) => g)).size === entries.length;
}
function gameFor(match, player) {
  return validBindings(match) && Object.hasOwn(match.game_bindings, player) ? match.game_bindings[player] : '';
}
function playerFor(match, game) {
  if (!validSteam(game) || !validBindings(match)) return '';
  return Object.keys(match.game_bindings).find(p => match.game_bindings[p] === game) || '';
}
function combatSummary(match, summary) {
  const bound = match.game_bindings ? match : freezeMatch({...match,
    players:(match.players || []).map(row=>({...row})), left:(match.left || []).map(row=>({...row}))});
  return {...summary, playerStats:(summary.playerStats || []).flatMap(row => {
    const game = row.game_steam_id || row.steam_id, player = playerFor(bound, game);
    return player ? [{...row, player_id:player, game_steam_id:game, steam_id:game}] : [];
  })};
}
// Steam-only older clients keep working. UUIDs are never labelled Steam IDs.
function wire(value) {
  if (!value || typeof value !== 'object') return value;
  if (Array.isArray(value)) return value.map(wire);
  const out = Object.fromEntries(Object.entries(value).map(([k,v]) => [k,wire(v)]));
  if (out.player_id && !out.steam_id) {
    if (validSteam(out.game_steam_id)) out.steam_id = out.game_steam_id;
    else if (validSteam(out.player_id)) out.steam_id = out.player_id;
  }
  return out;
}
class MatchMap extends Map {
  set(key, match) { return super.set(key, freezeMatch(match)); }
}
module.exports = { validPlayer, validSteam, playerOf, gameOf, hydrate, freezeMatch,
  validBindings, gameFor, playerFor, combatSummary, wire, MatchMap, parse: text => hydrate(JSON.parse(text)) };
