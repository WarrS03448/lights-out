'use strict';

/**
 * THE GAMEMODE BUILD NUMBER — a version component that counts rules changes.
 *
 * Sam, 2026-09-17: *"lets just make it create a new gamemode version for every update so its not
 * confusing"*.
 *
 * WHY A GAMEMODE CAN CHANGE WITHOUT ITS VERSION MOVING. A pack's zip carries cooked assets and
 * nothing else; the numbers that decide what the mode actually PLAYS - the score limit, the round
 * count, the team sizes - are merged in when the hub builds the pak on the player's machine, from
 * `rules` in the manifest and `rules_override` off the catalogue. The override comes from
 * COMP_GAME_RULES_OVERRIDE and is attached per request, so changing it changes every pak built
 * from then on while the version number sits still.
 *
 * That is not a theoretical confusion. On 2026-09-16 it cost four live test matches: the catalogue
 * said `max_players` 10, the pak on disk had been built with 2 - the value that leaves
 * `ABodycamGameState::Teams` empty, so no match can ever reach its score limit - and nothing
 * anywhere said so, because 1.0.15 was installed and 1.0.15 was on offer.
 *
 * So a mode with an override is served with a fourth component:
 *
 *     1.0.15        no override   - production, byte for byte what is on disk
 *     1.0.15.1      first ruleset
 *     1.0.15.2      ...and the next
 *
 * Both comparators already cope, unchanged: `hub/catalogue.py` `_parts` splits on every dot and
 * `version_newer` pads the short side with zeroes, and `live.cjs` `versionOlder` does the same. So
 * `1.0.15.1` is newer than `1.0.15`, and `1.0.15.2` is newer again.
 *
 * IT ONLY EVER COUNTS UP. A number derived from the rules themselves - a hash, or the values
 * packed into digits - would be stable and would need no storage, but it would go DOWN as often as
 * up, and a hub offering an "update" from 1.0.15.812 to 1.0.15.44 is worse than no number at all.
 * Going back to a ruleset that was used before therefore takes the NEXT number, not the old one.
 *
 * THE COUNTER IS OPTIONAL, like every other use of the store here. Until `restore` has been called
 * the answer is 0 and no suffix is added: a plain version is always safe, whereas a counter guessed
 * before the stored one is read could go backwards on the next boot, which is the single thing this
 * must never do. With no store at all the numbers still advance in memory and start again on
 * restart - the behaviour before this existed, plus a number.
 */

/** A ruleset's identity, stable across key order so a re-serialised copy is not a new ruleset. */
function fingerprint(rules) {
  const keys = Object.keys(rules || {}).sort();
  return keys.map((key) => `${key}=${rules[key]}`).join(';');
}

/**
 * `persist(id, value)` is called, fire and forget, whenever a number is issued. A failed write
 * costs one repeated number after a restart, never a wrong answer now - so it is never awaited and
 * never throws into the caller.
 */
function createRulesBuilds(persist) {
  let builds = null;                 // Map: mode id -> { fp, n }. null until restore().

  /** Adopt the counters read back from the store. `rows` is Upstash's flat HGETALL array. */
  function restore(rows) {
    const map = new Map();
    if (Array.isArray(rows)) {
      for (let i = 0; i + 1 < rows.length; i += 2) {
        const [count, ...rest] = String(rows[i + 1] || '').split(' ');
        const n = Number(count);
        if (Number.isFinite(n) && n > 0) map.set(String(rows[i]), { n, fp: rest.join(' ') });
      }
    }
    builds = map;
    return map;
  }

  /** The number this mode's CURRENT ruleset has, or the next one. 0 = not restored yet. */
  function numberFor(id, rules) {
    if (builds === null) return 0;
    const key = String(id);
    const fp = fingerprint(rules);
    const had = builds.get(key);
    if (had && had.fp === fp) return had.n;
    const n = (had ? had.n : 0) + 1;
    builds.set(key, { fp, n });
    try {
      const out = persist && persist(key, `${n} ${fp}`);
      if (out && typeof out.catch === 'function') out.catch(() => {});
    } catch {
      /* the store is optional */
    }
    return n;
  }

  /**
   * The version to SERVE for this entry: its own, plus the build number when it has an override.
   *
   * `rules` is null for a mode nobody is overriding, which is every mode in production - and that
   * path returns the version untouched, so a production catalogue is byte for byte the file on disk.
   */
  function versionFor(entry, rules) {
    const base = String((entry && entry.version) || '');
    if (!rules || !base) return base;
    const n = numberFor((entry && entry.id) || '', rules);
    return n > 0 ? `${base}.${n}` : base;
  }

  return { restore, numberFor, versionFor, fingerprint,
           get restored() { return builds !== null; } };
}

module.exports = { createRulesBuilds, fingerprint };
