#!/usr/bin/env node
// Plain Node test runner for the Community Hub server.
// No framework: node:assert/strict + node:child_process only.

import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
// The offline-persona guard reads live.cjs as TEXT: a duplicate function declaration is legal
// JavaScript, so counting them in the source is the only cheap way to catch the next one.
import { readFile as readFile_ } from 'node:fs/promises';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SERVER_PATH = path.join(__dirname, '..', 'server.cjs');
// live.cjs is CommonJS and this runner is an ES module: the routing test below asks it which
// paths it owns, and asking is the whole point - a copy of the list would not catch the bug.
const require_ = createRequire(import.meta.url);
// Existing lifecycle fixtures have no Steam probe. Dedicated network tests keep enforcement on.
process.env.NODE_ENV = 'test';
process.env.COMP_NETWORK_TEST_BYPASS = '1';
const liveModule = require_('../live.cjs');
const rulesBuildModule = require_('../rulesbuild.cjs');

const results = [];

// Model the atomic persistence commands used by these integration fixtures. The production
// Lua itself is executed separately by test-settlement-lua.py; unsupported scripts fail loudly.
function rankScript(script, args, { strings, sets, boards }) {
  const count = Number(args[0]);
  const keys = args.slice(1, count + 1);
  const argv = args.slice(count + 1);
  const index = (key, row) => {
    if (!boards.has(key)) boards.set(key, new Map());
    if (row.placing) boards.get(key).delete(row.id);
    else boards.get(key).set(row.id, Number(row.progress));
  };
  if(script.includes('host-forget-v1')) {
    const authority=JSON.parse(strings.get(keys[2])||'null');
    if(authority&&!authority.closed&&!strings.has(keys[3]))return 0;
    strings.delete(keys[0]);sets.get(keys[1])?.delete(argv[0]);return 1;
  }
  if (script.includes('rank-snapshot-v1')) {
    const receipt = strings.get(keys[0]);
    if (receipt) {
      strings.delete(keys[1]);
      sets.get(keys[2])?.delete(argv[0]);
      return ['settled', receipt];
    }
    strings.set(keys[1], argv[1]);
    if (!sets.has(keys[2])) sets.set(keys[2], new Set());
    sets.get(keys[2]).add(argv[0]);
    return ['saved'];
  }
  if (script.includes('rank-write-v1')) {
    const [row] = JSON.parse(argv[0]);
    const current = strings.get(keys[0]);
    if (keys[2] && strings.has(keys[2])) return ['written', current];
    if (current === row.json) {
      if (keys[2]) strings.set(keys[2], row.json);
      return ['written', current];
    }
    if ((JSON.parse(current || '{}').revision || 0) !== row.expected) return ['conflict'];
    strings.set(keys[0], row.json);
    index(keys[1], row);
    if (keys[2]) strings.set(keys[2], row.json);
    return ['written', row.json];
  }
  if (script.includes('rank-settlement-v1')) {
    const receipt = strings.get(keys[0]);
    if (receipt) return ['replayed', receipt];
    const rows = JSON.parse(argv[2]);
    for (let i = 0; i < rows.length; i += 1) {
      if ((JSON.parse(strings.get(keys[4 + i]) || '{}').revision || 0) !== rows[i].expected) {
        return ['conflict', rows[i].id];
      }
    }
    for (let i = 0; i < rows.length; i += 1) {
      strings.set(keys[4 + i], rows[i].json);
      index(keys[1], rows[i]);
    }
    strings.set(keys[0], argv[1]);
    strings.delete(keys[2]);
    sets.get(keys[3])?.delete(argv[0]);
    return ['committed', argv[1]];
  }
  throw new Error('Unsupported Redis script in integration fixture');
}

/**
 * What a hub claims to be running, on every request, exactly as hub/live.py `_stamp` sends it.
 *
 * The server's queue gate (live.cjs) refuses /api/queue/join from anything behind the catalogue
 * it is serving, and a test that sent no headers would be refused as an ancient hub. main() fills
 * this in from the catalogue the server actually answers with, so the whole suite queues as a
 * current hub does - and the gate's own tests pass an override to be something else.
 */
const CLIENT_VERSIONS = { hub: '', mode: '' };

const versionHeaders = (over) => {
  const v = { ...CLIENT_VERSIONS, ...(over || {}) };
  const out = {};
  if (v.hub) out['x-hub-version'] = v.hub;
  if (v.mode) out['x-mode-version'] = v.mode;
  return out;
};

/** Open an SSE stream and collect events. `wait(type)` resolves with the first matching one. */
async function openStream(base, token, versions) {
  const controller = new AbortController();
  const response = await fetch(`${base}/api/live`, {
    headers: { authorization: `Bearer ${token}`, ...versionHeaders(versions) },
    signal: controller.signal,
  });
  if (response.status !== 200) {
    controller.abort();
    throw new Error(`stream failed: HTTP ${response.status}`);
  }
  const events = [];
  const waiters = [];
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  (async () => {
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let cut;
        while ((cut = buffer.indexOf('\n\n')) >= 0) {
          const frame = buffer.slice(0, cut);
          buffer = buffer.slice(cut + 2);
          for (const line of frame.split('\n')) {
            if (!line.startsWith('data: ')) continue;       // ": ping" and "retry:" are not events
            const event = JSON.parse(line.slice(6));
            events.push(event);
            for (let i = waiters.length - 1; i >= 0; i -= 1) {
              if (waiters[i].type === event.type && waiters[i].predicate(event)) { waiters[i].resolve(event); waiters.splice(i, 1); }
            }
          }
        }
      }
    } catch { /* aborted */ }
  })();

  return {
    events,
    close: () => controller.abort(),
    wait(type, ms = 8000, predicate = () => true) {
      const already = events.find((e) => e.type === type && predicate(e));
      if (already) return Promise.resolve(already);
      return new Promise((resolve, reject) => {
        const waiter = { type, resolve, predicate };
        waiters.push(waiter);
        setTimeout(() => {
          const i = waiters.indexOf(waiter);
          if (i >= 0) waiters.splice(i, 1);
          reject(new Error(`timed out waiting for "${type}"; saw: ${events.map((e) => e.type).join(', ')}`));
        }, ms);
      });
    },
  };
}

const post = (base, path, token, body, versions) => fetch(`${base}${path}`, {
  method: 'POST',
  headers: {
    ...(token ? { authorization: `Bearer ${token}` } : {}),
    ...(body === undefined ? {} : { 'content-type': 'application/json' }),
    ...versionHeaders(versions),
  },
  ...(body === undefined ? {} : { body: JSON.stringify(body) }),
});

const sleep = (ms) => new Promise((resolve) => { setTimeout(resolve, ms).unref(); });

async function test(name, fn) {
  try {
    await fn();
    results.push({ name, ok: true });
    console.log(`ok - ${name}`);
  } catch (err) {
    results.push({ name, ok: false, err });
    console.error(`FAIL - ${name}`);
    console.error(err && err.stack ? err.stack : err);
  }
}

function startServer(env = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [SERVER_PATH], {
      env: {
        ...process.env,
        PORT: '0',
        // These HTTP flow tests have no game/pak reporter. Team gating is exercised below
        // and in test-team-sort.cjs with the default enabled setting.
        COMP_TEAMS_GATE_SECONDS: '0',
        ...env,
      },
      stdio: ['ignore', 'pipe', 'pipe'],
    });

    let settled = false;
    let stdoutBuf = '';
    let stderrBuf = '';

    const timeout = setTimeout(() => {
      if (settled) return;
      settled = true;
      child.kill();
      reject(new Error(`Server did not report a listening port in time. stderr:\n${stderrBuf}`));
    }, 10_000);

    child.stdout.on('data', (chunk) => {
      stdoutBuf += chunk.toString('utf8');
      const match = stdoutBuf.match(/listening on port (\d+)/);
      if (match && !settled) {
        settled = true;
        clearTimeout(timeout);
        resolve({ child, port: Number(match[1]) });
      }
    });

    child.stderr.on('data', (chunk) => {
      stderrBuf += chunk.toString('utf8');
    });

    child.on('error', (err) => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      reject(err);
    });

    child.on('exit', (code) => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      reject(new Error(`Server exited early (code ${code}). stderr:\n${stderrBuf}`));
    });
  });
}

function stopServer(child) {
  return new Promise((resolve) => {
    child.once('exit', () => resolve());
    child.kill();
    setTimeout(resolve, 2000).unref();
  });
}

async function main() {
  const { child, port } = await startServer();
  const base = `http://127.0.0.1:${port}`;

  try {
    // The hub version and the gamemode count move every publish, so read them from the
    // catalogue the server is actually serving instead of hardcoding them (they were
    // pinned to 1.0.0 / 1 gamemode and had been failing since the first BB5 publish).
    const catalogue = await (await fetch(`${base}/catalogue.json`)).json();
    const liveHubVersion = catalogue.hub.version;
    const liveGamemodeCount = catalogue.gamemodes.length;
    // ...and from here on every request in this file reports those versions, because the queue
    // gate refuses anything older. Read from the served catalogue for the same reason the two
    // lines above are: hardcoding it is how they came to be pinned at 1.0.0.
    const rankedEntry = catalogue.gamemodes.find((m) => m.id === liveModule.GATED_MODE_ID) || {};
    CLIENT_VERSIONS.hub = liveHubVersion;
    CLIENT_VERSIONS.mode = rankedEntry.version || '';

    await test('GET / contains the title and the current version', async () => {
      const res = await fetch(`${base}/`);
      assert.equal(res.status, 200);
      const body = await res.text();
      assert.match(body, /Lights Out/);
      assert.match(body, /Bodycam/);
      assert.ok(body.includes(liveHubVersion), `index.html should show hub ${liveHubVersion}`);
      // Ranked, How it works and FAQ moved off the front page on 2026-09-16. What has to survive
      // is the way OUT to them: a front page that drops all three and links to none of them is
      // the regression this catches.
      for (const href of ['/ranked', '/how', '/faq', '/about']) {
        assert.match(body, new RegExp(`href="${href}"`), `index.html should link ${href}`);
      }
    });

    await test('the front page no longer carries the four build facts', async () => {
      // Version, installer size, "Windows 10 & 11" and "no account" were cut from the hero on
      // 2026-09-16; the version stays in the footer, which is the one place it has to be right.
      const body = await (await fetch(`${base}/`)).text();
      assert.doesNotMatch(body, /cta-meta/);
      assert.doesNotMatch(body, /MB installer|KB installer/);
      assert.doesNotMatch(body, /Windows 10/);
      assert.doesNotMatch(body, /no account/);
      // The download guidance explains the warning and gives the installation steps.
      assert.match(body, /Windows protected your PC/);
      assert.match(body, /More info/);
      assert.match(body, /Run anyway/);
      assert.match(body, /faq-smartscreen/);
      assert.match(body, /By downloading and using Lights Out, you accept responsibility/);
      assert.match(body, /the risk is not zero/);
    });

    await test('the three moved sections each render at their own path', async () => {
      const pages = [
        ['/ranked', /id="ranked"/, /Inside a ranked match/],
        ['/how', /id="how"/, /How it works/],
        ['/faq', /id="faq"/, /Windows protected your PC/],
      ];
      for (const [pathname, id, heading] of pages) {
        const res = await fetch(`${base}${pathname}`);
        assert.equal(res.status, 200, `${pathname} should be a page`);
        assert.match(res.headers.get('content-type') || '', /text\/html/);
        const body = await res.text();
        assert.match(body, id, `${pathname} should carry its section id`);
        assert.match(body, heading, `${pathname} should carry its content`);
        assert.ok(body.includes(liveHubVersion), `${pathname} should show hub ${liveHubVersion}`);
        assert.doesNotMatch(body, /\{\{[A-Z_]+\}\}/, `${pathname} should leave no placeholder`);
      }
      // The ladder travelled with the section, and it is still RENDERED rather than typed: the
      // capstone chip only exists because renderRankLadder() marked it.
      const ranked = await (await fetch(`${base}/ranked`)).text();
      assert.match(ranked, /class="ladder"/);
      assert.match(ranked, /<li class="top">/);
      // The section that replaced "This is not a cheat": the page explains the mechanism now.
      const how = await (await fetch(`${base}/how`)).text();
      assert.doesNotMatch(how, /<h[1-3][^>]*>\s*This is not a cheat/i);
      assert.match(how, /never reads or writes the game/);
    });

    await test('GET / lists every catalogue gamemode, and no placeholder survives', async () => {
      const res = await fetch(`${base}/`);
      const body = await res.text();
      // The mode list is rendered from the catalogue, so it cannot go stale the way the typed-in
      // "currently Capture the Flag 10v10" line did. The card splits the title: "Capture the Flag
      // 10v10" renders as a "10v10" badge beside the name "Capture the Flag", so assert on the two
      // halves rather than on the joined string.
      for (const mode of catalogue.gamemodes) {
        const format = (mode.title.match(/\s*(\d+\s*v\s*\d+)\s*$/i) || [])[1];
        const name = format ? mode.title.slice(0, mode.title.lastIndexOf(format)).trim() : mode.title;
        assert.ok(body.includes(`>${name}</h3>`), `index.html should name "${name}"`);
        if (format) {
          assert.ok(
            body.includes(`class="mode-format">${format.replace(/\s+/g, '').toLowerCase()}<`),
            `index.html should badge "${mode.title}" with its format`,
          );
        }
        assert.ok(body.includes(`v${mode.version}<`), `index.html should show v${mode.version}`);
      }
      // The "N modes" chip and the lede beside the heading were cut on 2026-09-16: the front page
      // is one screen now, and a count of a list that is right there was the cheapest line on it.
      assert.doesNotMatch(body, />\d+ modes</);
      assert.doesNotMatch(body, /section-lede/);
      assert.doesNotMatch(body, /movement and gunplay/);
      assert.doesNotMatch(body, /\{\{[A-Z_]+\}\}/);
    });

    await test('the front page is the one-screen layout, and no page carries the free/windows tell', async () => {
      // `home` is what site.css hangs the viewport-height layout on. It has to be on the download
      // page and on NO other page: every other page is a document and has to scroll.
      const home = await (await fetch(`${base}/`)).text();
      assert.match(home, /<body class="home">/, 'the front page wears the one-screen layout');
      for (const pathname of ['/ranked', '/how', '/faq', '/about']) {
        const body = await (await fetch(`${base}${pathname}`)).text();
        assert.doesNotMatch(body, /<body class="home">/, `${pathname} must stay a scrolling document`);
      }
      // ...and the stylesheet still gates it on a window with the room, rather than clipping.
      const css = await (await fetch(`${base}/assets/site.css`)).text();
      assert.match(css, /@media \(min-width: 1060px\) and \(min-height: 700px\)/);
      assert.doesNotMatch(css, /body\.home \{[^}]*overflow: hidden/);
      // The "Free · Windows" badge came out of the topbar on 2026-09-16, everywhere.
      for (const pathname of ['/', '/ranked', '/how', '/faq', '/about']) {
        const body = await (await fetch(`${base}${pathname}`)).text();
        assert.doesNotMatch(body, /status-dot/, `${pathname} should have no status badge`);
      }
      assert.doesNotMatch(css, /\.status-dot/);
    });

    await test('only the ranked mode carries the ranked tell, and it is read from live.cjs', async () => {
      // The card wears what the app's own gamemode screen wears: a solid RANKED tag beside the
      // name and an accent top edge, with every other card left grey. It is derived from
      // live.cjs's GATED_MODE_ID rather than typed, so the mode the site calls ranked is the one
      // the queue actually gates on.
      const body = await (await fetch(`${base}/`)).text();
      const cards = body.match(/<li class="mode[^"]*">[\s\S]*?<\/li>\s*(?=<li class="mode|<\/ul>)/g) || [];
      assert.equal(cards.length, catalogue.gamemodes.length, 'one card per catalogue mode');
      const tagged = cards.filter((card) => card.includes('class="mode-tag"'));
      assert.equal(tagged.length, 1, 'exactly one card should be tagged ranked');
      assert.ok(tagged[0].includes('<li class="mode ranked">'), 'the tagged card gets the accent edge');
      // Same split the card does: "Bodybomb 5v5" is a "5v5" badge beside the name "Bodybomb".
      const rankedTitle = String(rankedEntry.title || '');
      const rankedFormat = (rankedTitle.match(/\s*(\d+\s*v\s*\d+)\s*$/i) || [])[1];
      const rankedName = rankedFormat
        ? rankedTitle.slice(0, rankedTitle.lastIndexOf(rankedFormat)).trim()
        : rankedTitle;
      assert.ok(
        tagged[0].includes(`>${rankedName}</h3>`),
        `the ranked tag belongs to ${liveModule.GATED_MODE_ID}, not to another mode`,
      );
      for (const card of cards) {
        if (card === tagged[0]) continue;
        assert.ok(!card.includes('mode ranked'), 'an unranked mode keeps the grey edge');
      }
    });

    await test('a mode card carries rule chips read from its own ruleset', async () => {
      // The chips are DERIVED, not typed: a mode whose ruleset is worded differently must lose a
      // chip rather than gain a wrong one, so this checks the derivation against the live text.
      const body = await (await fetch(`${base}/`)).text();
      const withRules = catalogue.gamemodes.filter(
        (m) => m.rulesets && /First team to (\d+)/i.test(m.rulesets.en || ''),
      );
      assert.ok(withRules.length, 'at least one catalogue mode should state a round target');
      for (const mode of withRules) {
        const target = mode.rulesets.en.match(/First team to (\d+)/i)[1];
        assert.ok(
          body.includes(`<li>First to ${target}</li>`),
          `${mode.id} should chip its "first to ${target}"`,
        );
      }
      // The shouty bracketed asides move out of the sentence and into chips.
      assert.doesNotMatch(body, /class="mode-desc">[^<]*\[/);
    });

    await test('GET /about renders the about page with the current version', async () => {
      const res = await fetch(`${base}/about`);
      assert.equal(res.status, 200);
      assert.match(res.headers.get('content-type') || '', /text\/html/);
      const body = await res.text();
      assert.match(body, /<title>About Lights Out<\/title>/);
      assert.match(body, /not in any way affiliated/);
      assert.match(body, /contact@theneeb\.com/);
      assert.ok(body.includes(liveHubVersion), `about.html should show hub ${liveHubVersion}`);
      assert.doesNotMatch(body, /\{\{[A-Z_]+\}\}/);
    });

    await test('both pages load the site stylesheet, and /assets/ serves it', async () => {
      // A page that renders unstyled is the failure this catches: the pages reference /assets/
      // by absolute path, and nothing else in the server serves that prefix.
      for (const page of ['/', '/ranked', '/how', '/faq', '/about']) {
        const body = await (await fetch(`${base}${page}`)).text();
        assert.match(body, /\/assets\/site\.css/, `${page} should link the site stylesheet`);
      }

      const css = await fetch(`${base}/assets/site.css`);
      assert.equal(css.status, 200);
      assert.match(css.headers.get('content-type') || '', /text\/css/);
      // Edited in place, so it must not be cached for a year the way the fonts are.
      assert.equal(css.headers.get('cache-control'), 'no-store');
      const cssBody = await css.text();
      // The site wears the app's tokens; the accent is the one value that proves it.
      assert.match(cssBody, /#c8102e/);

      const font = await fetch(`${base}/assets/oswald-latin.woff2`);
      assert.equal(font.status, 200);
      assert.match(font.headers.get('content-type') || '', /font\/woff2/);
      assert.match(font.headers.get('cache-control') || '', /immutable/);
    });

    await test('/assets/ refuses traversal and unlisted file types', async () => {
      // Encoded separators only: fetch() collapses a literal `..` segment before the request is
      // ever sent, so `/assets/../catalogue.json` would test the client, not the server.
      for (const target of [
        '/assets/..%2Fcatalogue.json',
        '/assets/..%5Ccatalogue.json',
        '/assets/server.cjs',
        '/assets/nope.woff2',
      ]) {
        const res = await fetch(`${base}${target}`, { redirect: 'manual' });
        assert.ok(res.status === 400 || res.status === 404,
          `${target} should be refused, got ${res.status}`);
      }
    });

    await test('GET /catalogue.json parses and has gamemodes[0].id === "CTF"', async () => {
      const res = await fetch(`${base}/catalogue.json`);
      assert.equal(res.status, 200);
      assert.equal(res.headers.get('cache-control'), 'no-store');
      assert.match(res.headers.get('content-type') || '', /application\/json/);
      const json = await res.json();
      assert.equal(json.gamemodes[0].id, 'CTF');
    });

    await test('the catalogue carries no rules_override unless COMP_GAME_RULES_OVERRIDE is set', async () => {
      const json = await (await fetch(`${base}/catalogue.json`)).json();
      for (const m of json.gamemodes) {
        assert.equal(m.rules_override, undefined, `${m.id} must ship its own rules in production`);
      }
      const health = await (await fetch(`${base}/api/health`)).json();
      assert.equal(health.rules_override, undefined);
    });

    await test('COMP_GAME_RULES_OVERRIDE puts the test rules on the catalogue, and derives the rest', async () => {
      // One number is enough: max_rounds and team_switch_interval are derived so a first-to-2
      // match cannot end in a draw the server is never told about.
      const { child: c2, port: p2 } = await startServer({
        COMP_GAME_RULES_OVERRIDE: '{"BB5":{"score_limit":2},"NOPE":{"score_limit":3}}',
      });
      try {
        const b2 = `http://127.0.0.1:${p2}`;
        const json = await (await fetch(`${b2}/catalogue.json`)).json();
        const bb5 = json.gamemodes.find((m) => m.id === 'BB5');
        assert.deepEqual(bb5.rules_override,
                         { score_limit: 2, max_rounds: 3, team_switch_interval: 1 });
        const ctf = json.gamemodes.find((m) => m.id === 'CTF');
        assert.equal(ctf.rules_override, undefined, 'only the named modes are touched');
        // and it is readable without a Railway dashboard
        const health = await (await fetch(`${b2}/api/health`)).json();
        assert.equal(health.rules_override.BB5.score_limit, 2);
      } finally {
        await stopServer(c2);
      }
    });

    await test('the derived swap is the one BB5 ships, not a half of the derived cap', async () => {
      // score_limit 7 has a known right answer - the shipped mode is 7 / 12 / 6 - so the
      // derivation has to reproduce it rather than contradict it.
      const { child: c2, port: p2 } = await startServer({
        COMP_GAME_RULES_OVERRIDE: '{"BB5":{"score_limit":7}}',
      });
      try {
        const json = await (await fetch(`http://127.0.0.1:${p2}/catalogue.json`)).json();
        const bb5 = json.gamemodes.find((m) => m.id === 'BB5');
        assert.equal(bb5.rules_override.team_switch_interval, 6,
                     'sides swap after 6, as they do in the pack and in every other first-to-N');
        assert.equal(bb5.rules_override.max_rounds, 13,
                     'one round past the shipped 12, where 6-6 is a draw the server never hears about');
      } finally {
        await stopServer(c2);
      }
    });

    // ------------------------------------------------------- the rules build number
    //
    // Sam, 2026-09-17: "lets just make it create a new gamemode version for every update so its
    // not confusing". A gamemode's rules are served per request and never touch its zip, so one
    // version number covered paks that play differently - which cost four live test matches in one
    // evening, because 1.0.15 was installed, 1.0.15 was on offer, and they were not the same pak.
    const builds = (persisted) => rulesBuildModule.createRulesBuilds(
      (id, value) => { if (persisted) persisted.push([id, value]); });
    const BB5 = { id: 'BB5', version: '1.0.15' };

    await test('a rules change numbers the version, and the same rules keep their number', () => {
      const saved = [];
      const B = builds(saved);
      B.restore([]);                                   // the store answered: nothing stored yet
      assert.equal(B.versionFor(BB5, { score_limit: 2 }), '1.0.15.1', 'the first ruleset takes .1');
      assert.equal(B.versionFor(BB5, { score_limit: 2 }), '1.0.15.1',
                   'asking twice about the same rules is not a change');
      assert.equal(B.versionFor(BB5, { max_rounds: 12, score_limit: 2 }), '1.0.15.2');
      assert.equal(B.versionFor(BB5, { score_limit: 2, max_rounds: 12 }), '1.0.15.2',
                   'the same ruleset written in the other order is the same ruleset');
      assert.equal(B.versionFor(BB5, { score_limit: 2 }), '1.0.15.3',
                   'going BACK to an earlier ruleset still counts UP - a version that goes down is '
                   + 'worse than no number at all');
      assert.deepEqual(saved.map(([, v]) => v.split(' ')[0]), ['1', '2', '3'],
                       'and every number issued was written to the store');
    });

    await test('production is served byte for byte, with no number at all', () => {
      const B = builds();
      B.restore([]);
      assert.equal(B.versionFor(BB5, null), '1.0.15');
    });

    await test('no number is added until the store has answered', () => {
      // A counter guessed before the stored one is read could go BACKWARDS on the next boot, and a
      // version that goes backwards is the one thing this must never do. A plain version is always
      // safe, so that is what the opening moments serve.
      const B = builds();
      assert.equal(B.restored, false);
      assert.equal(B.versionFor(BB5, { score_limit: 2 }), '1.0.15');
    });

    await test('a restored counter carries on rather than re-issuing a number', () => {
      // The store is read at boot precisely so a redeploy does not hand out .1 twice for two
      // different rulesets. Upstash returns HGETALL as a flat array.
      const B = builds();
      B.restore(['BB5', '7 score_limit=7']);
      assert.equal(B.versionFor(BB5, { score_limit: 7 }), '1.0.15.7',
                   'the ruleset it was already serving keeps its number');
      assert.equal(B.versionFor(BB5, { score_limit: 2 }), '1.0.15.8',
                   'and a new one takes the next, not the first');
    });

    await test('a store that answers with rubbish is ignored, not fatal', () => {
      const B = builds();
      B.restore(['BB5', 'not-a-number fp', 'CTF', '', 'ODD']);
      assert.equal(B.restored, true, 'it still counts as answered');
      assert.equal(B.versionFor(BB5, { score_limit: 2 }), '1.0.15.1');
    });

    await test('an explicit max_rounds is left alone, and garbage is ignored rather than fatal', async () => {
      const { child: c2, port: p2 } = await startServer({
        COMP_GAME_RULES_OVERRIDE: '{"BB5":{"score_limit":2,"max_rounds":12,"nonsense":"x"}}',
      });
      try {
        const json = await (await fetch(`http://127.0.0.1:${p2}/catalogue.json`)).json();
        const bb5 = json.gamemodes.find((m) => m.id === 'BB5');
        assert.deepEqual(bb5.rules_override,
                         { score_limit: 2, max_rounds: 12, team_switch_interval: 1 });
      } finally {
        await stopServer(c2);
      }
    });

    await test('a broken COMP_GAME_RULES_OVERRIDE serves the catalogue untouched instead of 500ing', async () => {
      // A typo in an environment variable must not take the catalogue - and with it the queue
      // gate, the download page and every install - off the air.
      const { child: c2, port: p2 } = await startServer({ COMP_GAME_RULES_OVERRIDE: '{not json' });
      try {
        const res = await fetch(`http://127.0.0.1:${p2}/catalogue.json`);
        assert.equal(res.status, 200);
        const json = await res.json();
        assert.ok(json.gamemodes.length > 0);
        assert.equal(json.gamemodes[0].rules_override, undefined);
      } finally {
        await stopServer(c2);
      }
    });

    await test('GET /hub/download 302s to the zip when published, else the exe', async () => {
      const catRes = await fetch(`${base}/catalogue.json`);
      const cat = await catRes.json();

      const res = await fetch(`${base}/hub/download`, { redirect: 'manual' });
      assert.equal(res.status, 302);
      assert.equal(res.headers.get('location'), cat.hub.zip_url || cat.hub.download_url);
      // hub/update.py self-updates from download_url and must get a runnable exe, never the zip
      assert.ok(cat.hub.download_url.endsWith('.exe'), 'download_url must stay the .exe');
    });

    await test('the current CTF pack streams with the right length and immutable cache header', async () => {
      const catalogue = await (await fetch(`${base}/catalogue.json`)).json();
      const pack = catalogue.gamemodes.find((mode) => mode.id === 'CTF');
      assert.ok(pack, 'the catalogue must include CTF');
      const res = await fetch(`${base}${new URL(pack.pack_url).pathname}`);
      assert.equal(res.status, 200);
      assert.equal(res.headers.get('cache-control'), 'public, max-age=31536000, immutable');
      assert.equal(res.headers.get('content-type'), 'application/zip');
      const buf = Buffer.from(await res.arrayBuffer());
      assert.equal(String(buf.length), res.headers.get('content-length'));
      assert.equal(buf.length, pack.size, 'the served pack must match the catalogue');
      assert.ok(buf.length > 0, 'zip body should not be empty');
    });

    await test('GET /packs/../server.cjs is rejected (404/400)', async () => {
      const res = await fetch(`${base}/packs/../server.cjs`);
      assert.ok(
        res.status === 404 || res.status === 400,
        `expected 404 or 400, got ${res.status}`
      );
    });

    await test('GET /packs/%2e%2e/server.cjs is rejected (404/400)', async () => {
      const res = await fetch(`${base}/packs/%2e%2e/server.cjs`);
      assert.ok(
        res.status === 404 || res.status === 400,
        `expected 404 or 400, got ${res.status}`
      );
    });

    await test('GET /api/health is ok', async () => {
      const res = await fetch(`${base}/api/health`);
      assert.equal(res.status, 200);
      assert.equal(res.headers.get('cache-control'), 'no-store');
      const json = await res.json();
      assert.equal(json.ok, true);
      assert.equal(json.build, '1.0.0');            // server/package.json, not the hub
      assert.equal(json.gamemodes, liveGamemodeCount);
      assert.equal(json.hub, liveHubVersion);
    });

    await test('POST /api/event returns 204 without Upstash env configured', async () => {
      const res = await fetch(`${base}/api/event`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ gamemode: 'CTF', action: 'install' }),
      });
      assert.equal(res.status, 204);
    });

    await test('POST /api/event with bad JSON returns 400', async () => {
      const res = await fetch(`${base}/api/event`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: '{not valid json',
      });
      assert.equal(res.status, 400);
    });

    await test('POST /api/event with an invalid action returns 400', async () => {
      const res = await fetch(`${base}/api/event`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ gamemode: 'CTF', action: 'nonsense' }),
      });
      assert.equal(res.status, 400);
    });

    await test('native telemetry remains reachable without exposing diagnostic data', async () => {
      assert.equal((await fetch(`${base}/api/probe`, {method: 'POST', body: '{}'})).status, 200);
      for (const route of ['/probe', '/api/probe/log', '/api/probe/clear', '/api/probe/join', '/api/probe/team']) {
        assert.equal((await fetch(base + route, {method: route.endsWith('/clear') ? 'POST' : 'GET'})).status, 404);
      }
    });

    // --- Steam sign-in (C1) ---
    await test('GET /api/auth/start hands out a link code and a URL to open', async () => {
      const res = await fetch(`${base}/api/auth/start`);
      assert.equal(res.status, 200);
      assert.equal(res.headers.get('cache-control'), 'no-store');
      const json = await res.json();
      assert.equal(json.ok, true);
      assert.match(json.code, /^[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{12}$/);  // no I/O/0/1
      assert.ok(json.url.endsWith(`/auth/steam/start?code=${json.code}`), json.url);
      assert.ok(json.expires_in >= 300);
    });

    await test('GET /auth/steam/start bounces to Steam with a correct OpenID request', async () => {
      const { code } = await (await fetch(`${base}/api/auth/start`)).json();
      const res = await fetch(`${base}/auth/steam/start?code=${code}`, { redirect: 'manual' });
      assert.equal(res.status, 302);
      const location = new URL(res.headers.get('location'));
      assert.equal(location.origin + location.pathname, 'https://steamcommunity.com/openid/login');
      const q = location.searchParams;
      assert.equal(q.get('openid.mode'), 'checkid_setup');
      assert.equal(q.get('openid.ns'), 'http://specs.openid.net/auth/2.0');
      assert.equal(q.get('openid.identity'), 'http://specs.openid.net/auth/2.0/identifier_select');
      assert.equal(q.get('openid.claimed_id'), 'http://specs.openid.net/auth/2.0/identifier_select');
      assert.ok(q.get('openid.return_to').includes(`code=${code}`), q.get('openid.return_to'));
      // return_to must live under realm, or Steam itself rejects the request
      assert.ok(q.get('openid.return_to').startsWith(q.get('openid.realm')));
    });

    await test('an unknown or used link code is refused, not silently accepted', async () => {
      const res = await fetch(`${base}/auth/steam/start?code=NOPENOPENOPE`, { redirect: 'manual' });
      assert.equal(res.status, 400);
      assert.match(await res.text(), /expired/i);
    });

    await test('GET /auth/steam/return rejects a claimed_id that is not a Steam identity', async () => {
      const { code } = await (await fetch(`${base}/api/auth/start`)).json();
      const bad = `${base}/auth/steam/return?code=${code}`
        + '&openid.claimed_id=' + encodeURIComponent('https://evil.example/openid/id/76561198000000001');
      const res = await fetch(bad);
      assert.equal(res.status, 400);
      assert.match(await res.text(), /valid account id/i);
      // and the code must still be unusable afterwards
      const poll = await (await fetch(`${base}/api/auth/poll?code=${code}`)).json();
      assert.equal(poll.status, 'pending');
    });

    await test('GET /api/auth/poll reports pending, then expired for an unknown code', async () => {
      const { code } = await (await fetch(`${base}/api/auth/start`)).json();
      let json = await (await fetch(`${base}/api/auth/poll?code=${code}`)).json();
      assert.equal(json.status, 'pending');
      json = await (await fetch(`${base}/api/auth/poll?code=ZZZZZZZZZZZZ`)).json();
      assert.equal(json.status, 'expired');
      const missing = await fetch(`${base}/api/auth/poll`);
      assert.equal(missing.status, 400);
    });

    await test('GET /api/auth/me is 401 without a token and with a made-up one', async () => {
      let res = await fetch(`${base}/api/auth/me`);
      assert.equal(res.status, 401);
      res = await fetch(`${base}/api/auth/me`, { headers: { authorization: 'Bearer not-a-real-token' } });
      assert.equal(res.status, 401);
      assert.equal((await res.json()).ok, false);
    });

    await test('POST /api/auth/signout is harmless without a token', async () => {
      const res = await fetch(`${base}/api/auth/signout`, { method: 'POST' });
      assert.equal(res.status, 200);
      assert.equal((await res.json()).ok, true);
    });

    await test('unknown route returns 404 JSON', async () => {
      const res = await fetch(`${base}/does/not/exist`);
      assert.equal(res.status, 404);
      const json = await res.json();
      assert.equal(typeof json.error, 'string');
    });
    await test('the live endpoints refuse an unauthenticated caller', async () => {
      for (const path of ['/api/live', '/api/queue/join', '/api/queue/leave', '/api/match/accept']) {
        const method = path === '/api/live' ? 'GET' : 'POST';
        const res = await fetch(`${base}${path}`, { method });
        assert.equal(res.status, 401, path);
      }
      // stats is the one open endpoint: the idle screen shows it before you sign in
      const stats = await fetch(`${base}/api/live/stats`);
      assert.equal(stats.status, 200);
      const json = await stats.json();
      assert.equal(json.ok, true);
      assert.equal(typeof json.online, 'number');
      assert.equal(typeof json.queued, 'number');
      assert.equal(typeof json.live_matches, 'number');
    });

  } finally {
    await stopServer(child);
  }

  // --- the live competitive service, on its own server: a 2-player match and a 5 s accept ---
  const live = await startServer({
    COMP_MATCH_SIZE: '2',
    COMP_ACCEPT_SECONDS: '5',
    HUB_TEST_TOKENS: 'tok-a=76561198000000001,tok-b=76561198000000002',
  });
  const lbase = `http://127.0.0.1:${live.port}`;
  const A = 'tok-a';
  const B = 'tok-b';

  try {
    await test('the stream says hello and reports presence', async () => {
      const a = await openStream(lbase, A);
      const hello = await a.wait('hello');
      assert.equal(hello.steam_id, '76561198000000001');
      assert.equal(hello.match_size, 2);
      assert.equal(hello.accept_seconds, 5);
      // THE PENALTY RULES ride along with the rank ladder, and for the same reason: every number
      // in here is an env dial, so a hub that shipped its own copy of the ban ladder would keep
      // explaining the old one the day a dial was turned - silently. The hub's Penalties screen
      // draws itself from exactly this and draws nothing when it is absent.
      assert.ok(hello.penalties, 'hello carries the penalty rules');
      // Rung by rung, in SECONDS, already multiplied out - a client that had to multiply would
      // need the base as well and could get the arithmetic wrong on its own.
      //
      // ASSERTED AS A SHAPE, NOT AS LITERALS, and that is the point of the whole payload. This
      // test named the six rungs for about an hour, and in that hour NO_SHOW_LADDER was retuned
      // from [1,3,6,12,24,48] to [1,3,6,12] on main (186aff2, the ban stops climbing at an hour)
      // - so a list of literals here would have failed for a change that is completely correct,
      // which is the same trap a ban ladder hard-coded into the hub's JS would fall into. What
      // has to hold is that the ladder ARRIVES and CLIMBS; what the rungs are is Sam's to retune.
      const rungs = hello.penalties.rungs;
      assert.ok(Array.isArray(rungs) && rungs.length >= 2, 'a ladder needs rungs to climb');
      assert.ok(rungs.every((s) => Number.isInteger(s) && s > 0), 'whole seconds, all positive');
      assert.deepEqual(rungs, [...rungs].sort((x, y) => x - y), 'it never gets SHORTER on repeat');
      assert.ok(rungs[rungs.length - 1] > rungs[0], 'and a repeat costs more than a first offence');
      // A shape as well, for the same reason: this was a literal 25 until Sam retuned it to 15 on
      // 2026-09-16, and the number that arrives is the service's to say.
      assert.ok(Number.isInteger(hello.penalties.rr) && hello.penalties.rr > 0,
                'and what an offence costs on the VISIBLE ladder');
      assert.ok(hello.penalties.connect_seconds > 0, 'the window a no-show is measured against');
      assert.ok(hello.penalties.decay_seconds > 0, 'and how fast a clean run forgives one');
      assert.equal(hello.penalties.team_kill.enforced, false,
                   'the screen must not promise a ban COMP_TK_ENFORCE is not handing out');
      assert.equal(hello.penalties.team_kill.limit, 3);
      const stats = await a.wait('stats');
      assert.equal(stats.online, 1);
      assert.equal(stats.queued, 0);
      assert.equal(stats.live_matches, 0);
      a.close();
    });

    await test('joining the queue is reported, and leaving it again', async () => {
      const a = await openStream(lbase, A);
      await a.wait('hello');
      const res = await post(lbase, '/api/queue/join', A);
      assert.equal(res.status, 200);
      const queued = await a.wait('queued');
      assert.equal(queued.position, 1);
      assert.equal((await (await post(lbase, '/api/queue/leave', A)).json()).was_queued, true);
      await a.wait('unqueued');
      a.close();
    });

    await test('two players in the queue are matched, and both are told', async () => {
      const a = await openStream(lbase, A);
      const b = await openStream(lbase, B);
      await a.wait('hello'); await b.wait('hello');
      await post(lbase, '/api/queue/join', A);
      await a.wait('queued');
      await post(lbase, '/api/queue/join', B);

      const foundA = await a.wait('match_found');
      const foundB = await b.wait('match_found');
      assert.equal(foundA.match_id, foundB.match_id);
      assert.equal(foundA.players.length, 2);
      assert.deepEqual(foundA.players.map((p) => p.steam_id).sort(),
                       ['76561198000000001', '76561198000000002']);
      // and the queue is empty again - and the match that emptied it is counted, because the
      // hub's top bar shows `live_matches` beside `online` and `queued` off this one payload.
      const stats = await (await fetch(`${lbase}/api/live/stats`)).json();
      assert.equal(stats.queued, 0);
      assert.equal(stats.live_matches, 1, 'a match in flight is not counted');

      // accepting is reported to BOTH players as it happens, then the match is ready
      await post(lbase, '/api/match/accept', A);
      const progress = await a.wait('match_accept');
      assert.equal(progress.accepted, 1);
      assert.equal(progress.total, 2);
      await post(lbase, '/api/match/accept', B);
      const readyA = await a.wait('match_ready');
      const readyB = await b.wait('match_ready');
      assert.equal(readyA.match_id, readyB.match_id);

      // a ready match holds both players until they leave it; without that they could
      // never queue again (which is exactly how this was found)
      const stuck = await post(lbase, '/api/queue/join', A);
      assert.equal(stuck.status, 409);
      assert.equal((await (await post(lbase, '/api/match/leave', A)).json()).was_in_match, true);
      await post(lbase, '/api/match/leave', B);
      const free = await post(lbase, '/api/queue/join', A);
      assert.equal(free.status, 200, 'leaving the match must free the player to queue');
      await post(lbase, '/api/queue/leave', A);
      // ...and the count comes back down. A figure that only ever goes up is worse than none.
      const after = await (await fetch(`${lbase}/api/live/stats`)).json();
      assert.equal(after.live_matches, 0, 'a match nobody is left in is still counted as live');
      a.close(); b.close();
    });

    await test('a chat line reaches the other player, filtered, under the name they may be told', async () => {
      // The bug this exists for: chat used to be a LOCAL ECHO. Every hub appended your line to
      // its own log and nothing was ever sent, so two people in the same lobby each saw a
      // conversation with themselves and nothing looked broken (Sam's friend, 2026-09-16).
      const a = await openStream(lbase, A);
      const b = await openStream(lbase, B);
      await a.wait('hello'); await b.wait('hello');
      await post(lbase, '/api/queue/join', A);
      await a.wait('queued');
      await post(lbase, '/api/queue/join', B);
      await a.wait('match_found'); await b.wait('match_found');
      await post(lbase, '/api/match/accept', A);
      await post(lbase, '/api/match/accept', B);
      const ready = await a.wait('match_ready'); await b.wait('match_ready');

      // The rate limiter is per account, so every line A sends has to clear COMP_CHAT_MIN_GAP_MS.
      // A human typing cannot reach it; a test firing POSTs back to back trips it every time,
      // which is how it was found.
      const gap = () => new Promise((r) => setTimeout(r, 650));

      // wait(type) returns the FIRST event of that type ever seen and does not consume it. That
      // is right for the one-shot events the rest of this file waits on and wrong for chat: a
      // player is in the audience of their own `all` line, so B's first `chat` event is B's own.
      // This waits for the line actually being asserted about.
      const heard = async (stream, match, label, ms = 6000) => {
        const deadline = Date.now() + ms;
        for (;;) {
          const hit = stream.events.find((e) => e.type === 'chat' && match(e));
          if (hit) return hit;
          if (Date.now() > deadline) {
            const saw = stream.events.filter((e) => e.type === 'chat').map((e) => e.text);
            throw new Error(`never heard ${label}; chat seen: ${JSON.stringify(saw)}`);
          }
          await new Promise((r) => setTimeout(r, 25));
        }
      };

      try {
        // TEAM chat must not leave the team. At this size A and B are one each, so A's team line
        // has an audience of one - A.
        assert.equal((await post(lbase, '/api/match/chat', A, { channel: 'team', text: 'team only' })).status, 200);
        const mine = await heard(a, (e) => e.text === 'team only', 'A\'s own team line');
        assert.equal(mine.channel, 'team');

        // ...and a second line straight after it is refused: one message every half second, per
        // account. B is a different account and is NOT held back by A's clock, which is the half
        // of the rule that stops a busy lobby throttling itself.
        const tooFast = await post(lbase, '/api/match/chat', A, { channel: 'all', text: 'again' });
        assert.equal(tooFast.status, 409, 'the rate limit let a burst through');
        assert.equal((await post(lbase, '/api/match/chat', B, { channel: 'all', text: 'hi' })).status, 200,
                     'the rate limit is per account, not per match');

        await gap();
        assert.equal((await post(lbase, '/api/match/chat', A, { channel: 'all', text: 'gl hf' })).status, 200);
        const got = await heard(b, (e) => e.text === 'gl hf', "A's all-chat line");
        assert.equal(got.channel, 'all');
        assert.equal(got.match_id, ready.match_id);
        // The negative, asserted once the `all` line has arrived: whatever B was going to be sent
        // has been sent by now, and the team line is not in it.
        assert.ok(!b.events.some((e) => e.type === 'chat' && e.text === 'team only'),
                  'the team line reached the other team');

        // A is on the other team, so during the LOBBY B is told a call sign, never the persona.
        // The persona must not be on the wire at all - a hub cannot hide what it was sent.
        assert.ok(/^(Alpha|Bravo|Charlie|Delta|Echo|Foxtrot|Golf|Hotel|India|Juliett)$/.test(got.name),
                  `expected a call sign, got ${JSON.stringify(got.name)}`);
        assert.ok(!JSON.stringify(got).includes('Test 01'), 'the persona reached the wire');

        // FILTERED BY THE SERVICE, not by the sender's hub. The word is taken from the service's
        // own blocklist so this file names none of them.
        const censor = require_('../censor.cjs');
        const slur = censor.words()[0];
        await gap();
        assert.equal((await post(lbase, '/api/match/chat', A, { channel: 'all', text: `hey ${slur}` })).status, 200);
        const masked = await heard(b, (e) => e.text.startsWith('hey '), 'the filtered line');
        assert.ok(!masked.text.includes(slur), 'the service broadcast a slur');
        assert.ok(masked.text.includes('*'), masked.text);

        // An unknown channel is the TEAM log, never `all`: the cost of that fallback being
        // backwards is a message meant for four people reaching ten.
        await gap();
        assert.equal((await post(lbase, '/api/match/chat', A, { channel: 'enemy', text: 'oops' })).status, 200);
        const fellback = await heard(a, (e) => e.text === 'oops', 'the fallback line');
        assert.equal(fellback.channel, 'team');
        assert.ok(!b.events.some((e) => e.type === 'chat' && e.text === 'oops'),
                  'an unknown channel was treated as all chat');

        // and an empty line is refused rather than broadcast as nothing
        await gap();
        assert.equal((await post(lbase, '/api/match/chat', A, { channel: 'all', text: '   ' })).status, 409);
      } finally {
        // WHATEVER HAPPENED ABOVE, get out of the match. A failed assertion used to leave A and B
        // sitting in a ready match, and the next two tests in this file - which queue the same two
        // accounts - failed for that reason rather than their own.
        await post(lbase, '/api/match/leave', A);
        await post(lbase, '/api/match/leave', B);
        a.close(); b.close();
      }
    });

    await test('a player who does not accept in time cancels the match, and the one who did is requeued', async () => {
      const a = await openStream(lbase, A);
      const b = await openStream(lbase, B);
      await a.wait('hello'); await b.wait('hello');
      await post(lbase, '/api/queue/join', A);
      await post(lbase, '/api/queue/join', B);
      await a.wait('match_found');
      await post(lbase, '/api/match/accept', A);      // A accepts, B says nothing

      const cancelled = await a.wait('match_cancelled', 12000);
      assert.equal(cancelled.reason, 'declined');
      assert.equal(cancelled.requeued, true, 'the player who accepted goes back in the queue');
      const forB = await b.wait('match_cancelled', 2000);
      assert.equal(forB.requeued, false, 'the player who did not accept does not');
      await post(lbase, '/api/queue/leave', A);
      await post(lbase, '/api/queue/leave', B);
      a.close(); b.close();
    });

    await test('leaving a match nobody has accepted yet is a decline', async () => {
      const a = await openStream(lbase, A);
      const b = await openStream(lbase, B);
      await a.wait('hello'); await b.wait('hello');
      await post(lbase, '/api/queue/join', A);
      await post(lbase, '/api/queue/join', B);
      await a.wait('match_found');
      const left = await (await post(lbase, '/api/match/leave', B)).json();
      assert.equal(left.declined, true);
      const cancelled = await a.wait('match_cancelled', 4000);
      assert.equal(cancelled.reason, 'declined');
      await post(lbase, '/api/queue/leave', A);
      await post(lbase, '/api/queue/leave', B);
      a.close(); b.close();
    });

    await test('accepting when you are in no match is refused', async () => {
      const a = await openStream(lbase, A);
      await a.wait('hello');
      const res = await post(lbase, '/api/match/accept', A);
      assert.equal(res.status, 409);
      assert.match((await res.json()).error, /no match/i);
      a.close();
    });

    // Leave any leftover party so each party test starts from solo (state is per-process).
    const clearParties = async () => {
      await post(lbase, '/api/party/leave', A);
      await post(lbase, '/api/party/leave', B);
    };

    // Wait for a party_update on `stream` that satisfies `pred` (streams accumulate events, so
    // an earlier one-member update may already be in the buffer - poll for the one we mean).
    const waitParty = (stream, pred, ms = 4000) => new Promise((resolve, reject) => {
      const t = setTimeout(() => reject(new Error('no matching party_update in time')), ms);
      const check = () => {
        const found = stream.events.filter((e) => e.type === 'party_update' && pred(e));
        if (found.length) { clearTimeout(t); resolve(found[found.length - 1]); }
        else setTimeout(check, 25);
      };
      check();
    });

    // The same poll for the invite inbox: it is pushed whole on every change, so the one we mean
    // is the one that satisfies `pred`, not simply the first to arrive.
    const waitInvites = (stream, pred, ms = 4000) => new Promise((resolve, reject) => {
      const t = setTimeout(() => reject(new Error('no matching party_invites in time')), ms);
      const check = () => {
        const found = stream.events.filter((e) => e.type === 'party_invites' && pred(e));
        if (found.length) { clearTimeout(t); resolve(found[found.length - 1]); }
        else setTimeout(check, 25);
      };
      check();
    });

    await test('creating a party gives a code and a one-member party_update', async () => {
      await clearParties();
      const a = await openStream(lbase, A);
      await a.wait('hello');
      const res = await post(lbase, '/api/party/create', A);
      assert.equal(res.status, 200);
      const body = await res.json();
      assert.match(body.code, /^[A-Z2-9]{4}-[A-Z2-9]{2}$/, 'a formatted, unambiguous code');
      const update = await a.wait('party_update');
      assert.equal(update.code, body.code);
      assert.equal(update.leader_id, '76561198000000001');
      assert.equal(update.members.length, 1);
      assert.equal(update.members[0].steam_id, '76561198000000001');
      assert.equal(update.members[0].persona, 'Test 01');
      // level/ping are explicit nulls, never invented
      assert.equal(update.members[0].level, null);
      assert.equal(update.members[0].ping, null);
      a.close();
      await clearParties();
    });

    await test('creating a party twice is idempotent (same code, no second party)', async () => {
      await clearParties();
      const a = await openStream(lbase, A);
      await a.wait('hello');
      const first = await (await post(lbase, '/api/party/create', A)).json();
      const second = await (await post(lbase, '/api/party/create', A)).json();
      assert.equal(first.code, second.code, 'a second create must not mint a new code');
      a.close();
      await clearParties();
    });

    await test('joining by code broadcasts a party_update to both members with real personas', async () => {
      await clearParties();
      const a = await openStream(lbase, A);
      const b = await openStream(lbase, B);
      await a.wait('hello'); await b.wait('hello');
      const code = (await (await post(lbase, '/api/party/create', A)).json()).code;
      await a.wait('party_update');
      const joined = await post(lbase, '/api/party/join', B, { code });
      assert.equal(joined.status, 200);

      for (const s of [a, b]) {
        const two = await waitParty(s, (e) => e.members && e.members.length === 2);
        assert.deepEqual(two.members.map((m) => m.steam_id).sort(),
                         ['76561198000000001', '76561198000000002']);
        assert.deepEqual(two.members.map((m) => m.persona).sort(), ['Test 01', 'Test 02']);
      }
      a.close(); b.close();
      await clearParties();
    });

    await test('leaving a party updates the survivor and tells the leaver they are solo', async () => {
      await clearParties();
      const a = await openStream(lbase, A);
      const b = await openStream(lbase, B);
      await a.wait('hello'); await b.wait('hello');
      const code = (await (await post(lbase, '/api/party/create', A)).json()).code;
      await post(lbase, '/api/party/join', B, { code });
      await a.wait('party_update'); await b.wait('party_update');

      await post(lbase, '/api/party/leave', B);
      // B is told they are solo (code:null); A sees a one-member roster again
      const solo = await waitParty(b, (e) => e.code === null);
      assert.equal(solo.code, null);
      const back = await waitParty(a, (e) => e.code === code && e.members && e.members.length === 1);
      assert.equal(back.members.length, 1);
      a.close(); b.close();
      await clearParties();
    });

    await test('when the leader leaves, the next member is promoted', async () => {
      await clearParties();
      const a = await openStream(lbase, A);
      const b = await openStream(lbase, B);
      await a.wait('hello'); await b.wait('hello');
      const code = (await (await post(lbase, '/api/party/create', A)).json()).code;
      await post(lbase, '/api/party/join', B, { code });
      await a.wait('party_update'); await b.wait('party_update');

      await post(lbase, '/api/party/leave', A);   // the leader walks out
      const promoted = await waitParty(b, (e) => e.leader_id === '76561198000000002');
      assert.equal(promoted.leader_id, '76561198000000002');
      assert.equal(promoted.members.length, 1);
      a.close(); b.close();
      await clearParties();
    });

    await test('refresh-code is leader-only and invalidates the old code at once', async () => {
      await clearParties();
      const a = await openStream(lbase, A);
      const b = await openStream(lbase, B);
      await a.wait('hello'); await b.wait('hello');
      const code = (await (await post(lbase, '/api/party/create', A)).json()).code;
      await post(lbase, '/api/party/join', B, { code });
      await a.wait('party_update'); await b.wait('party_update');

      // a non-leader cannot refresh
      const refusedB = await post(lbase, '/api/party/refresh-code', B);
      assert.equal(refusedB.status, 403);

      const refreshed = await post(lbase, '/api/party/refresh-code', A);
      assert.equal(refreshed.status, 200);
      const next = (await refreshed.json()).code;
      assert.notEqual(next, code, 'a new code must be minted');

      // the old code is unknown immediately: a stale join gets a 404
      await post(lbase, '/api/party/leave', B);
      const stale = await post(lbase, '/api/party/join', B, { code });
      assert.equal(stale.status, 404);
      // ...but the new code works
      const fresh = await post(lbase, '/api/party/join', B, { code: next });
      assert.equal(fresh.status, 200);
      a.close(); b.close();
      await clearParties();
    });

    await test('joining a bad or unknown code is a 404', async () => {
      await clearParties();
      const unknown = await post(lbase, '/api/party/join', A, { code: 'ZZZZ-ZZ' });
      assert.equal(unknown.status, 404);
      const malformed = await post(lbase, '/api/party/join', A, { code: 'nope' });
      assert.equal(malformed.status, 404);
      await clearParties();
    });

    // ---------------------------------------------------------------- routing
    //
    // 2026-09-15: the friends tab showed "Not found." and no friend code. Every friends route was
    // implemented and every test of them passed, because the tests call live.route() directly -
    // and server.cjs, which is what a real hub talks to, only forwarded four path prefixes to the
    // module. /api/friends/*, /api/leaderboard and /api/report were never handed over at all.
    //
    // This goes through the REAL server and asks the module which paths it owns, so a route added
    // to live.cjs and forgotten in server.cjs fails here instead of in the hub. No auth header:
    // 401 means the request reached live.cjs's auth check, which is all that is in question here.
    // A 404 is the bug.
    await test('every route live.cjs owns is reachable through server.cjs', async () => {
      const unreachable = [];
      for (const route of liveModule.NEEDS_AUTH) {
        const res = await post(lbase, route, null);
        if (res.status !== 401) unreachable.push(route + ' -> ' + res.status);
      }
      assert.deepEqual(unreachable, [], 'these never reached live.cjs (server.cjs did not route them)');
    });

    await test('the friends list answers with a code and three empty lists', async () => {
      const res = await fetch(lbase + '/api/friends/list', { headers: { authorization: 'Bearer ' + A } });
      assert.equal(res.status, 200, 'this is what the hub calls, and it was answering 404');
      const body = await res.json();
      assert.match(body.code, /^[A-Z2-9]+-[A-Z2-9]+$/, 'a friend code is minted on first ask');
      assert.deepEqual([body.friends, body.incoming, body.outgoing], [[], [], []]);
    });

    // ---------------------------------------------------------------- party invites
    const ID_A = '76561198000000001';
    const ID_B = '76561198000000002';
    const befriend = async () => {
      await post(lbase, '/api/friends/request', A, { target: ID_B });
      await post(lbase, '/api/friends/accept', B, { target: ID_A });
    };
    const unfriend = async () => { await post(lbase, '/api/friends/remove', A, { target: ID_B }); };

    await test('you can only invite a friend, and only into a party you are in', async () => {
      await clearParties();
      await unfriend();
      const a = await openStream(lbase, A);
      const b = await openStream(lbase, B);
      await a.wait('hello'); await b.wait('hello');

      // no party yet: there is no seat to offer
      const noParty = await post(lbase, '/api/party/invite', A, { target: ID_B });
      assert.equal(noParty.status, 409);
      assert.match((await noParty.json()).error, /not in a party/i);

      await post(lbase, '/api/party/create', A);
      await a.wait('party_update');

      // in a party, but a stranger: an invite is a thing somebody else can put on your screen,
      // so it is the friends list that decides who may
      const stranger = await post(lbase, '/api/party/invite', A, { target: ID_B });
      assert.equal(stranger.status, 409);
      assert.match((await stranger.json()).error, /friends list/i);

      a.close(); b.close();
      await clearParties();
    });

    await test('an invited friend gets an inbox and accepting puts them in the party', async () => {
      await clearParties();
      await befriend();
      const a = await openStream(lbase, A);
      const b = await openStream(lbase, B);
      await a.wait('hello'); await b.wait('hello');
      const code = (await (await post(lbase, '/api/party/create', A)).json()).code;
      await a.wait('party_update');

      const sent = await post(lbase, '/api/party/invite', A, { target: ID_B });
      assert.equal(sent.status, 200);

      const inbox = await waitInvites(b, (e) => e.invites.length === 1);
      assert.equal(inbox.invites[0].from.steam_id, ID_A);
      assert.equal(inbox.invites[0].code, code, 'the invite names the party it offers');
      assert.ok(inbox.invites[0].expires_in > 0, 'and says how long it stands');
      await b.wait('party_invite');        // the cue the toast is drawn from

      const joined = await post(lbase, '/api/party/invite/accept', B, { from: ID_A });
      assert.equal(joined.status, 200);
      const roster = await waitParty(a, (e) => e.code === code && e.members.length === 2);
      assert.deepEqual(roster.members.map((m) => m.steam_id).sort(), [ID_A, ID_B].sort());

      // and the invite is spent: the inbox empties itself rather than offering a seat I am in
      await waitInvites(b, (e) => e.invites.length === 0);
      a.close(); b.close();
      await clearParties();
    });

    await test('declining clears the invite, and accepting it afterwards is refused', async () => {
      await clearParties();
      await befriend();
      const a = await openStream(lbase, A);
      const b = await openStream(lbase, B);
      await a.wait('hello'); await b.wait('hello');
      await post(lbase, '/api/party/create', A);
      await a.wait('party_update');
      await post(lbase, '/api/party/invite', A, { target: ID_B });
      await waitInvites(b, (e) => e.invites.length === 1);

      const declined = await post(lbase, '/api/party/invite/decline', B, { from: ID_A });
      assert.equal(declined.status, 200);
      assert.equal((await declined.json()).declined, true);
      await waitInvites(b, (e) => e.invites.length === 0);

      const tooLate = await post(lbase, '/api/party/invite/accept', B, { from: ID_A });
      assert.equal(tooLate.status, 409);
      assert.match((await tooLate.json()).error, /expired/i);
      a.close(); b.close();
      await clearParties();
    });

    await test('an invite to somebody with no stream is refused, not queued', async () => {
      await clearParties();
      await befriend();
      const a = await openStream(lbase, A);
      await a.wait('hello');
      await post(lbase, '/api/party/create', A);
      await a.wait('party_update');
      // B has no stream open: an invite is delivered over it and nothing stores one
      const res = await post(lbase, '/api/party/invite', A, { target: ID_B });
      assert.equal(res.status, 409);
      assert.match((await res.json()).error, /online/i);
      a.close();
      await clearParties();
    });

    await test('a party survives a brief stream drop: reconnect replays the roster', async () => {
      await clearParties();
      const a1 = await openStream(lbase, A);
      await a1.wait('hello');
      const code = (await (await post(lbase, '/api/party/create', A)).json()).code;
      await a1.wait('party_update');
      a1.close();                       // the stream drops (inside the grace window)

      const a2 = await openStream(lbase, A);
      await a2.wait('hello');
      const replay = await a2.wait('party_update');
      assert.equal(replay.code, code, 'the reconnecting member gets the still-live party back');
      assert.equal(replay.members.length, 1);
      a2.close();
      await clearParties();
    });

    // ------------------------------------------------------------ the version gate
    // "people cant actually queue comp unless theyre on the most recent update" - but a match
    // already under way survives a release (Sam, 2026-09-15). Both halves are asserted here:
    // the refusal, and everything the refusal must NOT touch.
    const OLD_HUB = { hub: '0.0.1' };
    const OLD_MODE = { mode: '0.0.1' };

    await test('an out-of-date hub cannot join the queue', async () => {
      await clearParties();
      const a = await openStream(lbase, A, OLD_HUB);
      await a.wait('hello');
      const res = await post(lbase, '/api/queue/join', A, undefined, OLD_HUB);
      assert.equal(res.status, 426, 'Upgrade Required, and not one of the hub retry statuses');
      const body = await res.json();
      assert.equal(body.ok, false);
      assert.equal(body.outdated, true);
      assert.equal(body.what, 'hub');
      assert.equal(body.have_hub, '0.0.1');
      assert.equal(body.need_hub, CLIENT_VERSIONS.hub, 'the catalogue this server serves');
      assert.ok(!body.who, 'a solo refusal names nobody');
      a.close();
    });

    await test('an out-of-date gamemode cannot join the queue either', async () => {
      await clearParties();
      const a = await openStream(lbase, A, OLD_MODE);
      await a.wait('hello');
      const res = await post(lbase, '/api/queue/join', A, undefined, OLD_MODE);
      assert.equal(res.status, 426);
      const body = await res.json();
      assert.equal(body.what, 'mode', 'the hub is current; the pack is not');
      assert.equal(body.mode_id, liveModule.GATED_MODE_ID);
      assert.equal(body.need_mode, CLIENT_VERSIONS.mode);
      a.close();
    });

    await test('a hub with NO ranked pack installed cannot join the queue', async () => {
      // The hole Sam's friend fell through: this used to be waved through, on the reasoning that
      // the tab could not reach the queue without the pack. True of the Tk tab (gate screen),
      // false of the web UI (no gate) - so a fresh Lights Out queued for a mode it did not own.
      // Both headers ship in the same release, so hub-present + mode-absent is "not installed".
      await clearParties();
      const NO_PACK = { mode: '' };            // a current hub, saying nothing about the pack
      const a = await openStream(lbase, A, NO_PACK);
      await a.wait('hello');
      const res = await post(lbase, '/api/queue/join', A, undefined, NO_PACK);
      assert.equal(res.status, 426);
      const body = await res.json();
      assert.equal(body.what, 'mode', 'the hub is current; there is no pack at all');
      assert.equal(body.missing_mode, true, 'absent, not behind - the hub words it differently');
      assert.equal(body.have_mode, '');
      assert.equal(body.need_mode, CLIENT_VERSIONS.mode);
      a.close();
    });

    await test('a party is refused when a MEMBER is behind, and is told who', async () => {
      await clearParties();
      const a = await openStream(lbase, A);            // the leader is current
      const b = await openStream(lbase, B, OLD_HUB);   // the member is not
      await a.wait('hello'); await b.wait('hello');
      const code = (await (await post(lbase, '/api/party/create', A)).json()).code;
      // OLD_HUB on B's every request: one hub reports one version, and a member who sent a
      // current header on the join would simply be a member who had updated.
      await post(lbase, '/api/party/join', B, { code }, OLD_HUB);
      await a.wait('party_update'); await b.wait('party_update');

      const res = await post(lbase, '/api/queue/join', A);
      assert.equal(res.status, 426);
      const body = await res.json();
      assert.equal(body.outdated, true);
      assert.equal(body.who, '76561198000000002', 'the leader is told which member to chase');
      assert.match(body.error, /party/i);
      await post(lbase, '/api/party/leave', B, undefined, OLD_HUB);
      a.close(); b.close();
      await clearParties();
    });

    await test('A MATCH ALREADY UNDER WAY IS NOT GATED: it survives going out of date', async () => {
      await clearParties();
      const a = await openStream(lbase, A);
      const b = await openStream(lbase, B);
      await a.wait('hello'); await b.wait('hello');
      // Formed while both are current...
      await post(lbase, '/api/queue/join', A);
      await post(lbase, '/api/queue/join', B);
      await a.wait('match_found'); await b.wait('match_found');

      // ...and from here on both hubs are behind, exactly as a push mid-match makes them.
      // Accept is the first thing the gate must not touch: refuse it and both players are
      // blamed for a match they were trying to join.
      const acceptA = await post(lbase, '/api/match/accept', A, undefined, OLD_HUB);
      assert.equal(acceptA.status, 200, 'accept is never version-gated');
      const acceptB = await post(lbase, '/api/match/accept', B, undefined, OLD_HUB);
      assert.equal(acceptB.status, 200);
      const ready = await a.wait('match_ready', 6000);
      assert.ok(ready, 'the match goes ahead');

      // The stream is not gated either - it is how they are handed the lobby back.
      const c = await openStream(lbase, A, OLD_HUB);
      const hello = await c.wait('hello');
      assert.equal(hello.steam_id, '76561198000000001');
      c.close();

      // ...and leaving it still works, so nobody is trapped in a match by their own version.
      const leave = await post(lbase, '/api/match/leave', A, undefined, OLD_HUB);
      assert.equal(leave.status, 200, 'leaving a match is never version-gated');
      await post(lbase, '/api/queue/leave', A, undefined, OLD_HUB);
      await post(lbase, '/api/queue/leave', B, undefined, OLD_HUB);
      a.close(); b.close();
      await clearParties();
    });

  } finally {
    await stopServer(live.child);
  }

  // --- a hub from before the headers existed: it says nothing, and cannot be current ---
  {
    const silent = await startServer({
      COMP_MATCH_SIZE: '2',
      COMP_VERSION_GATE: 'strict',
      HUB_TEST_TOKENS: 'tok-s=76561198000000077',
    });
    const sbase = `http://127.0.0.1:${silent.port}`;
    try {
      await test('strict: a hub that reports no version at all is treated as behind', async () => {
        // Every header suppressed, on a server that has never heard from this account: a build
        // from before the hub started reporting, which by definition predates this release.
        const none = { hub: '', mode: '' };
        const a = await openStream(sbase, 'tok-s', none);
        await a.wait('hello');
        const res = await post(sbase, '/api/queue/join', 'tok-s', undefined, none);
        assert.equal(res.status, 426);
        assert.equal((await res.json()).what, 'both');
        a.close();
      });
    } finally {
      await stopServer(silent.child);
    }
  }

  // --- explicit legacy lenient mode: a hub that says nothing is let through, an old one is not ---
  // This is the day the gate ships: every hub alive predates the release that reports a version,
  // and refusing them all would tell the whole player base to update to the build they are on.
  {
    const lax = await startServer({
      COMP_VERSION_GATE: 'lenient',
      COMP_MATCH_SIZE: '2',
      HUB_TEST_TOKENS: 'tok-l=76561198000000078,tok-m=76561198000000079',
    });
    const lbase2 = `http://127.0.0.1:${lax.port}`;
    try {
      await test('lenient: a hub from before the headers existed may still queue', async () => {
        const none = { hub: '', mode: '' };
        const a = await openStream(lbase2, 'tok-l', none);
        await a.wait('hello');
        const res = await post(lbase2, '/api/queue/join', 'tok-l', undefined, none);
        assert.equal(res.status, 200, 'the release that adds the gate must not lock everyone out');
        await post(lbase2, '/api/queue/leave', 'tok-l', undefined, none);
        a.close();
      });

      await test('lenient: a hub that SAYS it is old is still refused', async () => {
        const old = { hub: '0.0.1', mode: '0.0.1' };
        const b = await openStream(lbase2, 'tok-m', old);
        await b.wait('hello');
        const res = await post(lbase2, '/api/queue/join', 'tok-m', undefined, old);
        assert.equal(res.status, 426, 'lenient is about SILENCE, not about being out of date');
        b.close();
      });
    } finally {
      await stopServer(lax.child);
    }
  }

  // --- COMP_VERSION_GATE=off: the escape hatch for a catalogue that locked everybody out ---
  {
    const off = await startServer({
      COMP_MATCH_SIZE: '2',
      COMP_VERSION_GATE: 'off',
      HUB_TEST_TOKENS: 'tok-a=76561198000000001',
    });
    const obase = `http://127.0.0.1:${off.port}`;
    try {
      await test('COMP_VERSION_GATE=off lets an ancient hub queue', async () => {
        const old = { hub: '0.0.1', mode: '0.0.1' };
        const a = await openStream(obase, 'tok-a', old);
        await a.wait('hello');
        const res = await post(obase, '/api/queue/join', 'tok-a', undefined, old);
        assert.equal(res.status, 200, 'the dial must work, or a bad catalogue needs a deploy to fix');
        await post(obase, '/api/queue/leave', 'tok-a', undefined, old);
        a.close();
      });
    } finally {
      await stopServer(off.child);
    }
  }

  // ---------------------------------------------------------------- surviving a redeploy
  // "we dont want redeploys to kill live matches" (Sam, 2026-09-15).
  //
  // A redeploy is two containers, one after the other, sharing one Upstash. That is what is
  // built here: two live.cjs instances over ONE fake store, so the handover is the real one and
  // not a description of it. The HTTP suite above cannot do this - each startServer() is its own
  // process with its own memory, and no store between them.
  {
    const liveModule = await import('../live.cjs').then((m) => m.default || m);

    /** Just enough Redis for the live-match keys: strings with an optional EX, and one set. */
    function fakeStore() {
      const strings = new Map();
      const sets = new Map();
      const boards = new Map();
      const calls = [];
      const cmd = async (args) => {
        const [op, key, ...rest] = args.map(String);
        calls.push(op);
        switch (op) {
          case 'EVAL': return rankScript(key, rest, { strings, sets, boards });
          case 'SET': strings.set(key, rest[0]); return 'OK';
          case 'GET': return strings.has(key) ? strings.get(key) : null;
          case 'DEL': strings.delete(key); sets.delete(key); return 1;
          case 'SADD': {
            if (!sets.has(key)) sets.set(key, new Set());
            sets.get(key).add(rest[0]);
            return 1;
          }
          case 'SREM': { const set = sets.get(key); if (set) set.delete(rest[0]); return 1; }
          case 'SMEMBERS': return [...(sets.get(key) || [])];
          default: return null;       // everything else this module writes is not under test
        }
      };
      cmd.strings = strings;
      cmd.sets = sets;
      cmd.calls = calls;
      return cmd;
    }

    const container = (store) => liveModule.create({
      whoami: async () => null, bearer: () => '', sendJson: () => {}, badRequest: () => {},
      readBody: async () => Buffer.alloc(0), upstashCmd: store, prefix: 'redeploy:',
    });

    const PLAYERS = ['76561198000000001', '76561198000000002'];
    const liveMatch = (over = {}) => ({
      id: 'm-redeploy', state: 'connecting', host: PLAYERS[0], map: 'Rome',
      players: PLAYERS.map((id) => ({ steam_id: id, persona: '', accepted: true, connected: false })),
      teams: { 1: [PLAYERS[0]], 2: [PLAYERS[1]] },
      left: [], timer: null, ban_timer: null, expiry: 'connect',
      deadline: Date.now() + 120_000, created: Date.now(),
      ...over,
    });

    await test('a live match is written through, and the next container brings it back', async () => {
      const store = fakeStore();
      const a = container(store);
      a._internals.matches.set('m-redeploy', liveMatch());
      for (const id of PLAYERS) a._internals.inMatch.set(id, 'm-redeploy');
      await a.shutdown();                      // SIGTERM: the last flush, then the timers go

      const b = container(store);
      const recovered = await b._internals.ready;
      assert.equal(recovered, 1, 'the new container read one match back');
      const match = b._internals.matches.get('m-redeploy');
      assert.ok(match, 'the match exists in the new container');
      assert.equal(match.state, 'connecting', 'in the phase it was actually in');
      assert.equal(match.map, 'Rome');
      assert.equal(match.host, PLAYERS[0]);
      assert.deepEqual(match.players.map((p) => p.steam_id), PLAYERS);
      assert.deepEqual(match.teams, { 1: [PLAYERS[0]], 2: [PLAYERS[1]] });
      for (const id of PLAYERS) {
        assert.equal(b._internals.inMatch.get(id), 'm-redeploy',
                     'and the player is in it, so their reconnect is answered with the match');
      }
      await b.shutdown();
    });

    await test('the recovered match keeps the REMAINING window, not a fresh one', async () => {
      const store = fakeStore();
      const a = container(store);
      // Four of its five minutes are already spent.
      const deadline = Date.now() + 60_000;
      a._internals.matches.set('m-redeploy', liveMatch({ deadline }));
      await a.shutdown();

      const b = container(store);
      await b._internals.ready;
      const match = b._internals.matches.get('m-redeploy');
      assert.equal(match.deadline, deadline,
                   'the deadline is absolute and survives: a redeploy must not hand anyone a whole new connect window');
      assert.ok(match.timer, 'and it is back on a clock, or nothing would ever expire it');
      assert.equal(match.expiry, 'connect', 'the SAME clock: which one it was is stored, not guessed');
      await b.shutdown();
    });

    await test('nobody pays for the handover: a window that ran out during it gets a floor', async () => {
      const store = fakeStore();
      const a = container(store);
      // The accept window expired while the service was restarting - which is not 30 seconds the
      // player ever had, so they must not be judged on it the instant the new container is up.
      a._internals.matches.set('m-redeploy', liveMatch({
        state: 'found', expiry: 'accept', deadline: Date.now() - 5_000,
      }));
      await a.shutdown();

      const b = container(store);
      await b._internals.ready;
      const match = b._internals.matches.get('m-redeploy');
      const left = Math.round((match.deadline - Date.now()) / 1000);
      assert.ok(left >= b._internals.RECOVERY_GRACE_SECONDS - 2,
                `a recovered window must not be about to fire (got ${left}s)`);
      assert.ok(match.recovered, 'and the match says it was recovered, so an odd window is explicable');
      await b.shutdown();
    });

    await test('a match that ENDED is not resurrected by the next container', async () => {
      const store = fakeStore();
      const a = container(store);
      a._internals.matches.set('m-redeploy', liveMatch());
      await a._internals.flushMatches();               // it is on disk...
      a._internals.matches.delete('m-redeploy');
      a._internals.forgetMatch('m-redeploy');          // ...and then the match ends
      await a.shutdown();

      const b = container(store);
      assert.equal(await b._internals.ready, 0, 'nothing to bring back');
      assert.equal(b._internals.matches.size, 0,
                   'ten people must not be put back into a match that is over');
      await b.shutdown();
    });

    await test('a state nobody can be waiting in is dropped rather than restored', async () => {
      const store = fakeStore();
      const a = container(store);
      a._internals.matches.set('m-redeploy', liveMatch({ state: 'collecting' }));
      await a.shutdown();

      const b = container(store);
      assert.equal(await b._internals.ready, 0);
      assert.equal(b._internals.matches.size, 0);
      await b.shutdown();
    });

    await test('serialising a match keeps the Maps that carry the in-game reports', async () => {
      const store = fakeStore();
      const a = container(store);
      const ingame = new Map([[PLAYERS[0], { team: 1, at: 123 }]]);
      a._internals.matches.set('m-redeploy', liveMatch({
        state: 'live', expiry: 'live', ingame,
        rounds: [{ at: 1, won: 1 }], kills: [{ killer: PLAYERS[0] }],
      }));
      await a.shutdown();

      const b = container(store);
      await b._internals.ready;
      const match = b._internals.matches.get('m-redeploy');
      assert.ok(match.ingame instanceof Map,
                'a Map must come back a Map - JSON.stringify turns one into {} and the score is gone');
      assert.deepEqual(match.ingame.get(PLAYERS[0]), { team: 1, at: 123 });
      assert.deepEqual(match.rounds, [{ at: 1, won: 1 }]);
      assert.deepEqual(match.kills, [{ killer: PLAYERS[0] }]);
      assert.equal(match.timer && typeof match.timer, 'object', 'the live clock is re-armed too');
      await b.shutdown();
    });

    await test('snapshot reconciliation preserves unchanged matches and persists changed state', async () => {
      const store = fakeStore();
      const a = container(store);
      a._internals.matches.set('m-redeploy', liveMatch());
      await a._internals.flushMatches();
      const saved = store.strings.get('redeploy:live:match:m-redeploy');
      await a._internals.flushMatches();
      await a._internals.flushMatches();
      // Every tick checks the durable receipt, including when another container finished an
      // otherwise unchanged match. That safety check intentionally replaced local write skipping.
      assert.equal(store.strings.get('redeploy:live:match:m-redeploy'), saved,
                   'checking for a settlement must not change a still-active snapshot');
      assert.equal(store.sets.get('redeploy:live:matches').has('m-redeploy'), true);
      a._internals.matches.get('m-redeploy').state = 'live';
      await a._internals.flushMatches();
      assert.equal(JSON.parse(store.strings.get('redeploy:live:match:m-redeploy')).state, 'live',
                   'a changed state must be durable when the flush resolves');
      await a.shutdown();
    });

    await test('no store at all is not an error: it is every local run and this suite', async () => {
      const bare = liveModule.create({
        whoami: async () => null, bearer: () => '', sendJson: () => {}, badRequest: () => {},
        readBody: async () => Buffer.alloc(0), prefix: 'nostore:',
      });
      assert.equal(await bare._internals.ready, 0);
      bare._internals.matches.set('m-redeploy', liveMatch());
      bare._internals.flushMatches();          // must not throw
      await bare.shutdown();
    });
  }

  await test('versionOlder: the whole test the gate makes', () => {
    const older = liveModule.versionOlder;
    assert.equal(older('2.0.17', '2.0.18'), true);
    assert.equal(older('2.0.18', '2.0.18'), false, 'equal is current');
    assert.equal(older('2.0.19', '2.0.18'), false, 'a dev build ahead of the catalogue queues');
    assert.equal(older('2.1', '2.0.18'), false, 'a short version is not padded into the past');
    assert.equal(older('2.0', '2.0.1'), true);
    assert.equal(older('', '2.0.18'), true, 'saying nothing cannot be current');
    assert.equal(older('2.0.17', ''), false, 'no requirement refuses nobody');
    assert.equal(older('', ''), false, 'an unreadable catalogue opens the queue, it does not shut it');
    assert.equal(older('1.0.6-beta', '1.0.6'), false, 'a tag is not a version difference');
  });

  // --- a full party rejects a join (COMP_MAX_PARTY=1 caps it at one) ---
  const fullp = await startServer({
    COMP_MATCH_SIZE: '2',
    COMP_MAX_PARTY: '1',
    HUB_TEST_TOKENS: 'tok-a=76561198000000001,tok-b=76561198000000002',
  });
  const fbase = `http://127.0.0.1:${fullp.port}`;
  try {
    await test('joining a full party is a 409', async () => {
      const a = await openStream(fbase, 'tok-a');
      await a.wait('hello');
      const code = (await (await post(fbase, '/api/party/create', 'tok-a')).json()).code;
      await a.wait('party_update');
      const res = await post(fbase, '/api/party/join', 'tok-b', { code });
      assert.equal(res.status, 409);
      a.close();
    });
  } finally {
    await stopServer(fullp.child);
  }

  // --- a stale grace timer must not evict the caller from a DIFFERENT party they just joined ---
  // Regression: drop() armed a grace timer keyed by steam id ONLY, not bound to the party it was
  // armed for. A member whose last stream closed in P1 then POSTed join {P2} in the reconnect gap;
  // the pending timer re-read partyOf (now pointing at P2) and wrongly removed them from P2. A
  // deliberate party action proves presence, so the handlers now clear the caller's own grace.
  // PARTY_GRACE_SECONDS clamps to a 2 s minimum, so we wait a hair past 2 s.
  const grace = await startServer({
    COMP_MATCH_SIZE: '2',
    COMP_PARTY_GRACE_SECONDS: '1',            // clamps up to the 2 s floor
    HUB_TEST_TOKENS: 'tok-a=76561198000000001,tok-b=76561198000000002',
  });
  const gbase = `http://127.0.0.1:${grace.port}`;
  const waitPartyG = (stream, pred, ms = 4000) => new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(new Error('no matching party_update in time')), ms);
    const check = () => {
      const found = stream.events.filter((e) => e.type === 'party_update' && pred(e));
      if (found.length) { clearTimeout(t); resolve(found[found.length - 1]); }
      else setTimeout(check, 25);
    };
    check();
  });
  try {
    await test('a stale P1 grace timer does not evict the caller from P2 after they join it', async () => {
      // B holds P2 open so we can watch its roster; A's stream will drop to arm the timer.
      const b = await openStream(gbase, 'tok-b');
      await b.wait('hello');
      const p2 = (await (await post(gbase, '/api/party/create', 'tok-b')).json()).code;
      await b.wait('party_update');

      const a = await openStream(gbase, 'tok-a');
      await a.wait('hello');
      const p1 = (await (await post(gbase, '/api/party/create', 'tok-a')).json()).code;
      await a.wait('party_update');
      assert.notEqual(p1, p2);

      a.close();               // A's last stream closes -> a grace timer is armed for A, keyed to P1

      // Before the grace fires, A deliberately joins P2 (the SSE reconnect-gap "click join").
      const joined = await post(gbase, '/api/party/join', 'tok-a', { code: p2 });
      assert.equal(joined.status, 200);
      const two = await waitPartyG(b, (e) => e.code === p2 && e.members && e.members.length === 2);
      assert.deepEqual(two.members.map((m) => m.steam_id).sort(),
                       ['76561198000000001', '76561198000000002']);
      // level/ping are explicit nulls in the payload, not omitted (asserted server-side here too)
      for (const m of two.members) {
        assert.equal(m.level, null, 'level is an explicit null');
        assert.equal(m.ping, null, 'ping is an explicit null');
      }

      // Wait well past the 2 s grace floor: the stale P1 timer must NOT touch P2.
      await sleep(3500);
      const updates = b.events.filter((e) => e.type === 'party_update');
      const last = updates[updates.length - 1];
      assert.equal(last.code, p2, 'P2 is still live');
      assert.equal(last.members.length, 2, 'A is STILL in P2 after the stale grace window');
      assert.ok(last.members.some((m) => m.steam_id === '76561198000000001'),
                'A must remain a member of P2 - no spurious "member left"');

      // P1 was correctly handled: A switched out of it, and as its only member it was dissolved,
      // so a join against the old P1 code is a 404 (A is no longer in P1).
      const staleP1 = await post(gbase, '/api/party/join', 'tok-b', { code: p1 });
      assert.equal(staleP1.status, 404, 'P1 no longer exists and A is no longer in it');
      // reset B back to solo so the process ends clean
      await post(gbase, '/api/party/leave', 'tok-b');
      b.close();
    });
  } finally {
    await stopServer(grace.child);
  }

  // --- the connect window and the no-show penalty, on a server with very short clocks ---
  // Sam, 2026-09-14: "if not everyone connects to the game after everything is decided for 3
  // minutes, the game is cancelled nobody except the person who didnt connect loses anything.
  // the person who didnt connect will lose a medium size of elo and get a 5 [minute] queue ban".
  // Three minutes and five minutes are the DEFAULTS; here they are seconds so the whole
  // offence can be committed and served inside one test run.
  const conn = await startServer({
    COMP_MATCH_SIZE: '2',
    COMP_ACCEPT_SECONDS: '5',
    COMP_LOBBY_SECONDS: '10',
    COMP_CONNECT_SECONDS: '5',
    COMP_LIVE_SECONDS: '6',
    COMP_NO_SHOW_BAN_SECONDS: '5',
    COMP_NO_SHOW_ELO: '25',
    // The coin is HELD in the air so all ten clients watch it land (live.cjs FLIP_SECONDS). That
    // is four seconds of every lobby by default, and driveLobby has to sit through it, so here it
    // is one - the same reason every other clock on this server is seconds rather than minutes.
    COMP_FLIP_SECONDS: '1',
    // The other three lobby stages are on clocks of their own now, and if one of them expired
    // mid-test the server would make the choice and driveLobby's next POST would be refused as
    // out of stage. Generous rather than short: these tests are about the connect window, and a
    // lobby clock firing inside one would be a confusing way to fail.
    COMP_PICK_SECONDS: '60',
    COMP_BAN_SECONDS: '60',
    HUB_TEST_TOKENS: 'tok-a=76561198000000001,tok-b=76561198000000002,'
                   + 'tok-c=76561198000000003,tok-d=76561198000000004,'
                   + 'tok-e=76561198000000005,tok-f=76561198000000006',
  });
  const cbase = `http://127.0.0.1:${conn.port}`;

  // Which token signs in as which SteamID64 - the inverse of HUB_TEST_TOKENS above. The lobby names
  // its captains by steam id and only THAT captain may act, so driving it means looking the token
  // back up rather than guessing that A is always team 1.
  const TOKEN_FOR = {
    '76561198000000001': 'tok-a', '76561198000000002': 'tok-b',
    '76561198000000003': 'tok-c', '76561198000000004': 'tok-d',
    '76561198000000005': 'tok-e', '76561198000000006': 'tok-f',
  };

  /**
   * Run a server lobby from the coin flip to a decided map, as the right captain at every step.
   *
   * WHY THIS EXISTS. `beginConnect` refuses with "The lobby is not decided yet" unless
   * `lobby.stage === 'ready'`, and the lobby has been SERVER-AUTHORITATIVE since 793fd3a: the coin
   * flip, the side pick and the veto all run in live.cjs now. readyPair used to stop at
   * `match_ready` and post straight to /api/match/connecting, which was correct when those ran in
   * the hub and has been silently wrong ever since - nine tests in this file were failing on it,
   * and none of them was a product bug.
   *
   * Each POST returns the whole lobbyPayload, so the next step is driven off the response rather
   * than off the stream: no race, and the caller gets the final map back to assert against instead
   * of the one it used to pass in (the server ignores body.map now - that is the point of it).
   */
  async function driveLobby(token) {
    const cap = (L, team) => TOKEN_FOR[(L.captains || {})[String(team)]];
    let L = await (await post(cbase, '/api/match/coin', token, { side: 'heads' })).json();
    assert.equal(L.ok, true, `coin: ${L.error || ''}`);
    // THE COIN IS IN THE AIR. flipCoin decides the face and then holds the lobby in `flipping` so
    // every client sees the same toss land; nothing may be chosen until it comes down. Waited out
    // rather than skipped past, because that hold is the product behaviour under test everywhere
    // else - a driver that bypassed it would be testing a lobby no player ever gets.
    assert.equal(L.stage, 'flipping', 'the coin should be in the air right after the call');
    await sleep((L.stage_seconds + 1) * 1000);
    L = await (await post(cbase, '/api/match/choose', cap(L, L.toss_winner), { kind: 'ban' })).json();
    assert.equal(L.ok, true, `choose: ${L.error || ''}`);
    L = await (await post(cbase, '/api/match/side', cap(L, L.side_picker), { side: 'attack' })).json();
    assert.equal(L.ok, true, `side: ${L.error || ''}`);
    // Ban down to one. Whoever's turn it is bans the first map still standing; which map survives
    // does not matter to these tests, only that the veto really ran.
    let guard = 0;
    while (L.stage === 'veto') {
      if (guard += 1, guard > 20) throw new Error('the veto never finished');
      const banned = new Set((L.bans || []).map((b) => b.map));
      const left = (L.pool || []).filter((m) => !banned.has(m));
      if (left.length <= 1) break;
      L = await (await post(cbase, '/api/match/ban', cap(L, L.ban_turn), { map: left[0] })).json();
      assert.equal(L.ok, true, `ban: ${L.error || ''}`);
    }
    assert.equal(L.stage, 'ready', 'the lobby has to reach ready or the connect window cannot open');
    assert.ok(L.map, 'and it has to have left exactly one map standing');
    return L;
  }

  /**
   * Two signed-in streams that have already been matched and have both accepted.
   *
   * `drive` also runs the lobby through to a decided map, which is what any test that opens a
   * connect window needs. It is opt-in rather than automatic because one test deliberately wants a
   * lobby that NEVER finishes, and leaving the default alone keeps the tests that already pass
   * byte-for-byte as they were. When driven, the decided lobby is returned as a third element.
   */
  async function readyPair(t1, t2, { drive = false } = {}) {
    const s1 = await openStream(cbase, t1);
    const s2 = await openStream(cbase, t2);
    await s1.wait('hello'); await s2.wait('hello');
    await post(cbase, '/api/queue/join', t1);
    await s1.wait('queued');
    await post(cbase, '/api/queue/join', t2);
    await s1.wait('match_found'); await s2.wait('match_found');
    await post(cbase, '/api/match/accept', t1);
    await post(cbase, '/api/match/accept', t2);
    const ready = await s1.wait('match_ready'); await s2.wait('match_ready');
    if (!drive) return [s1, s2];
    const lobby = await driveLobby(TOKEN_FOR[ready.coin_captain] || t1);
    return [s1, s2, lobby];
  }

  async function approveStart(stream) {
    const match = stream.events.filter(e => e.type === 'match_connecting').at(-1);
    const teams = match.teams || stream.events.filter(e => e.type === 'lobby').at(-1).teams;
    const rows = [1, 2].flatMap(side => (teams[String(side)] || []).map(id => `${id}:${side-1}:1;`));
    const hostStream = await openStream(cbase, TOKEN_FOR[match.host]);
    try {
      const hostMatch = await hostStream.wait('match_connecting');
      const response = await post(cbase, '/api/match-report/start-ready', hostMatch.report_token, { event_name: 'ch_start_ready',
        user_id: match.host, storefront: 'chm-' + match.match_id, platform: `${rows.length}|${rows.join('')}` });
      assert.equal(response.status, 200, await response.text());
    } finally { hostStream.close(); }
  }

  try {
    await test('a lobby replayed on reconnect is marked resumed, not fresh', async () => {
      // The coin flip and the veto run in the HUB, so a reconnecting client has none of them
      // and cannot be told to start a lobby: the other nine are already having one. The flag
      // is what lets it wait to be let back in instead of inventing a second lobby.
      const [a, b] = await readyPair(A, B);
      a.close();
      const a2 = await openStream(cbase, A);
      await a2.wait('hello');
      const found = await a2.wait('match_found');
      const ready = await a2.wait('match_ready');
      assert.equal(found.resumed, true, 'a replayed match_found says so');
      assert.equal(ready.resumed, true, 'a replayed match_ready must not start a second lobby');
      assert.ok(ready.lobby_seconds > 0, 'and hands back the real clock');
      await post(cbase, '/api/match/leave', A);
      await post(cbase, '/api/match/leave', B);
      a2.close(); b.close();
    });

    await test('a live match is replayed on reconnect, lobby and all', async () => {
      // Closing the hub, updating it or losing the network must not cost a match that is
      // being PLAYED. Nothing of it survives in the hub - the lobby ran there - so the whole
      // thing has to come back off the server or the player lands on the idle screen while a
      // match they are still in carries on without them.
      const [a, b, lobby] = await readyPair(A, B, { drive: true });
      // No teams/sides/bans in the body any more: the SERVER decided them in the lobby the
      // helper just ran, and beginConnect ignores whatever a client claims about them.
      await post(cbase, '/api/match/connecting', A, { host: '76561198000000002' });
      await a.wait('match_connecting');
      await post(cbase, '/api/match/connected', A);
      await post(cbase, '/api/match/connected', B);
      await approveStart(a);
      const live = await a.wait('match_live');
      assert.deepEqual(live.players.map((p) => p.steam_id).sort(),
                       ['76561198000000001', '76561198000000002'],
                       'even the first match_live carries the roster the hub needs');

      a.close();                        // the hub goes away mid-match
      const a2 = await openStream(cbase, A);
      await a2.wait('hello');
      const replay = await a2.wait('match_live');
      assert.equal(replay.resumed, true);
      assert.equal(replay.match_id, live.match_id);
      assert.equal(replay.map, lobby.map, 'the map the veto left standing, replayed');
      assert.equal(replay.host, '76561198000000002');
      assert.ok(replay.live_seconds > 0 && replay.live_seconds <= 6,
                'what is left of the clock, not the ceiling again');
      // the teams the MATCHMAKER chose, carried through the lobby and back out of the replay -
      // which is the property this branch exists to protect
      assert.deepEqual(replay.teams, lobby.teams);
      assert.deepEqual(replay.sides, lobby.sides);
      assert.deepEqual(replay.bans, lobby.bans);

      await post(cbase, '/api/match/leave', A);
      await post(cbase, '/api/match/leave', B);
      a2.close(); b.close();
    });

    await test('everyone connecting inside the window starts the match', async () => {
      const [a, b, lobby] = await readyPair(A, B, { drive: true });
      const opened = await (await post(cbase, '/api/match/connecting', A,
                                       { map: 'Rome', host: '76561198000000001' })).json();
      assert.equal(opened.ok, true);
      const openA = await a.wait('match_connecting');
      const openB = await b.wait('match_connecting');
      assert.equal(openA.match_id, openB.match_id);
      assert.equal(openA.map, lobby.map);
      assert.equal(openA.total, 2);
      // the roster rides along: it is all a hub that reconnects into this window ever gets
      assert.deepEqual(openB.players.map((p) => p.steam_id).sort(),
                       ['76561198000000001', '76561198000000002']);
      assert.ok(openA.connect_seconds > 0 && openA.connect_seconds <= 5, 'the clock is handed over');

      await post(cbase, '/api/match/connected', A);
      const half = await b.wait('match_connect');
      assert.deepEqual(half.connected, ['76561198000000001']);
      await post(cbase, '/api/match/connected', B);
      await approveStart(a);
      const liveA = await a.wait('match_live');
      const liveB = await b.wait('match_live');
      assert.equal(liveA.match_id, liveB.match_id);
      assert.equal(liveA.map, lobby.map);

      await post(cbase, '/api/match/leave', A);
      await post(cbase, '/api/match/leave', B);
      a.close(); b.close();
    });

    await test('a live match closes itself instead of holding its players for ever', async () => {
      const [a, b] = await readyPair(A, B, { drive: true });
      const foundBefore = a.events.filter((e) => e.type === 'match_found').length;
      const priorMatchIds=new Set(a.events.filter(e=>e.type==='match_found').map(e=>e.match_id));
      await post(cbase, '/api/match/connecting', A, { host: '76561198000000001' });
      await a.wait('match_connecting');
      await post(cbase, '/api/match/connected', A);
      await post(cbase, '/api/match/connected', B);
      await approveStart(a);
      const liveA = await a.wait('match_live');
      assert.equal(liveA.live_seconds, 6, 'the server hands over its own ceiling');

      // While it is genuinely running, being held is correct.
      const held = await post(cbase, '/api/queue/join', A);
      assert.equal(held.status, 409);

      // B-02, 2026-09-14. NOTHING can end a live match yet - there is no result service - so
      // before this ceiling existed the match stayed in `matches` and both players stayed in
      // `inMatch` for the life of the PROCESS, and queue/join answered 409 for ever after.
      const done = await a.wait('match_cancelled', 12000);
      assert.equal(done.match_id, liveA.match_id);
      assert.equal(done.reason, 'stalled', 'our code never reported back; that is not a player\'s fault');
      assert.equal(done.blamed, false, 'NOBODY is blamed for a match reaching its ceiling');
      assert.equal(done.penalty, null, 'and nobody is charged');

      // ...and they are back in circulation: both were innocent, so they went to the front of
      // the queue and (at MATCH_SIZE 2) were matched again straight away.
      await a.wait('match_found',8000,e=>!priorMatchIds.has(e.match_id));
      const foundAfter = a.events.filter((e) => e.type === 'match_found');
      assert.equal(foundAfter.length, foundBefore + 1, 'a NEW match, so the player is free again');
      assert.notEqual(foundAfter[foundAfter.length - 1].match_id, liveA.match_id);

      await post(cbase, '/api/match/leave', A);
      await post(cbase, '/api/match/leave', B);
      await post(cbase, '/api/queue/leave', A);
      await post(cbase, '/api/queue/leave', B);
      a.close(); b.close();
    });

    await test('a lobby that never finishes releases everyone and blames nobody', async () => {
      const [a, b] = await readyPair(A, B);
      // neither client ever opens the connect window: the coin flip or the veto has stalled,
      // which is OUR bug, so the server lets them both go with no penalty at all
      const cancelA = await a.wait('match_cancelled', 15000);
      const cancelB = await b.wait('match_cancelled', 3000);
      assert.equal(cancelA.reason, 'stalled');
      assert.equal(cancelA.blamed, false);
      assert.equal(cancelB.blamed, false);
      assert.equal(cancelA.penalty, null);
      assert.equal(cancelA.requeued, true, 'nobody did anything wrong, so everybody requeues');
      // requeueing both re-forms the match straight away; clear it down for the next test
      await a.wait('match_found', 3000);
      await post(cbase, '/api/match/leave', A);
      await post(cbase, '/api/match/leave', B);
      await post(cbase, '/api/queue/leave', A);
      await post(cbase, '/api/queue/leave', B);
      a.close(); b.close();
    });

    await test('only the player who never connects is punished, and the ban expires', async () => {
      const C = 'tok-c';
      const D = 'tok-d';
      const [c, d] = await readyPair(C, D, { drive: true });
      await post(cbase, '/api/match/connecting', C, { host: '76561198000000003' });
      await c.wait('match_connecting');
      await post(cbase, '/api/match/connected', C);       // C loads in, D never does

      const forC = await c.wait('match_cancelled', 12000);
      const forD = await d.wait('match_cancelled', 3000);
      assert.equal(forC.reason, 'no_show');
      assert.equal(forC.blamed, false);
      assert.equal(forC.penalty, null, 'the player who turned up loses nothing');
      assert.equal(forC.requeued, true);
      assert.equal(forD.blamed, true);
      assert.ok(forD.penalty, 'the no-show is told what it cost');
      assert.equal(forD.penalty.reason, 'no_show');
      assert.equal(forD.penalty.elo, 25);
      assert.equal(forD.penalty.seconds, 5, 'a first no-show costs the first rung');
      assert.equal(forD.penalty.count, 1);
      assert.equal(forD.penalty.next_seconds, 15, 'and the tab is warned what the next one costs');
      assert.equal(forD.requeued, false);

      const banned = await post(cbase, '/api/queue/join', D);
      assert.equal(banned.status, 403, 'a banned player cannot queue');
      const body = await banned.json();
      assert.equal(body.banned, true);
      assert.equal(body.reason, 'no_show');
      assert.ok(body.seconds > 0 && body.seconds <= 5);

      await post(cbase, '/api/queue/leave', C);
      await sleep(5200);
      const served = await post(cbase, '/api/queue/join', D);
      assert.equal(served.status, 200, 'the ban is a ban, not a removal');
      await post(cbase, '/api/queue/leave', D);
      c.close(); d.close();
    });

    await test('when the HOST never loads in, only the host pays', async () => {
      // Sam, 2026-09-16, from a live test: he let the connect window run out as the host, "i got
      // the rr penalty and the timer, but so did the joiner". A joiner cannot begin to join until
      // the host's game is up and findable - it is why CONNECT_SECONDS re-bases itself at the
      // host's report - so a joiner whose host never turned up was never given a window at all.
      const E = 'tok-e';
      const F = 'tok-f';
      const [e, f] = await readyPair(E, F, { drive: true });
      await post(cbase, '/api/match/connecting', E, { host: '76561198000000005' });
      await e.wait('match_connecting');
      // NOBODY posts /api/match/connected: the host never gets its game up, so the joiner sits
      // on a grey Launch button until the window closes.

      const forE = await e.wait('match_cancelled', 12000);
      const forF = await f.wait('match_cancelled', 3000);
      assert.equal(forE.reason, 'no_show');
      assert.equal(forE.blamed, true, 'the host is the one who never turned up');
      assert.ok(forE.penalty, 'and pays for it');
      assert.equal(forE.penalty.reason, 'no_show');
      assert.equal(forF.blamed, false, 'the joiner had nothing to join');
      assert.equal(forF.penalty, null, 'so it costs them nothing');
      assert.equal(forF.requeued, true, 'and they go back to the front of the queue');

      await post(cbase, '/api/queue/leave', F);
      f.close(); e.close();
    });

    await test('walking out of the connect window costs the same as never arriving', async () => {
      const [a, b] = await readyPair(A, B, { drive: true });
      await post(cbase, '/api/match/connecting', A, { host: '76561198000000001' });
      await a.wait('match_connecting');
      await post(cbase, '/api/match/connected', A);
      const left = await (await post(cbase, '/api/match/leave', B)).json();
      assert.equal(left.no_show, true);
      assert.equal(left.penalty.reason, 'no_show');
      const forA = await a.wait('match_cancelled', 4000);
      assert.equal(forA.reason, 'no_show');
      assert.equal(forA.penalty, null);
      const banned = await post(cbase, '/api/queue/join', B);
      assert.equal(banned.status, 403);
      await post(cbase, '/api/queue/leave', A);
      a.close(); b.close();
    });

    await test('a repeat no-show is banned for longer', async () => {
      // Sam, 2026-09-14: a flat five minutes is farmable. D already has one offence from the
      // test above, so this one must land on the SECOND rung, not the first.
      const C = 'tok-c';
      const D = 'tok-d';
      const [c, d] = await readyPair(C, D, { drive: true });
      await post(cbase, '/api/match/connecting', C, { host: '76561198000000003' });
      await c.wait('match_connecting');
      await post(cbase, '/api/match/connected', C);
      const forD = await d.wait('match_cancelled', 12000);
      assert.equal(forD.penalty.count, 2);
      assert.equal(forD.penalty.seconds, 15, 'second offence is 3x the first rung');
      assert.equal(forD.penalty.next_seconds, 30);
      assert.equal(forD.penalty.elo, 25, 'the Elo loss stays flat; the BAN is what escalates');
      // ...and C, who turned up twice, still has a clean record
      const clean = await post(cbase, '/api/queue/join', C);
      assert.equal(clean.status, 200);
      await post(cbase, '/api/queue/leave', C);
      c.close(); d.close();
    });

    await test('the connect window warns each player with THEIR next rung', async () => {
      // D is two offences deep, C is clean: the same window must quote them different numbers
      const C = 'tok-c';
      const D = 'tok-d';
      await sleep(15200);                      // let D's 15 s ban run out first
      const [c, d] = await readyPair(C, D, { drive: true });
      await post(cbase, '/api/match/connecting', C, { host: '76561198000000003' });
      const forC = await c.wait('match_connecting');
      const forD = await d.wait('match_connecting');
      assert.equal(forC.no_show_seconds, 5, 'a clean player is told the first rung');
      assert.equal(forD.no_show_seconds, 30, 'a repeat offender is told the truth');
      assert.equal(forC.no_show_elo, 25);
      await post(cbase, '/api/match/connected', C);
      await post(cbase, '/api/match/connected', D);
      await approveStart(c);
      await c.wait('match_live');
      await post(cbase, '/api/match/leave', C);
      await post(cbase, '/api/match/leave', D);
      c.close(); d.close();
    });

    await test('the connect window refuses a body it cannot trust', async () => {
      const [a, b, lobby] = await readyPair(A, B, { drive: true });
      const me = '76561198000000001';        // A
      const stranger = '76561198000000009';  // in no match at all

      // B-04: an ARRAY body used to start the match on a map called
      // "function map() { [native code] }", because `body.map` found Array.prototype.map.
      const arrayBody = await post(cbase, '/api/match/connecting', A, []);
      assert.equal(arrayBody.status, 409, 'an array is not a body');

      const badHost = await post(cbase, '/api/match/connecting', A, { host: stranger });
      assert.equal(badHost.status, 409, 'the host has to be someone in the match');
      assert.match((await badHost.json()).error, /host/i);

      // THE MAP IS NO LONGER THE BODY'S TO GIVE. B-04's two map checks - no map at all, and a map
      // called "function map() { [native code] }" - guarded the OLD client-reported path, and
      // beginConnect still applies them when a match has no server lobby. This match has one, so
      // the map comes off the veto and the body's is ignored outright. That is a stronger property
      // than refusing it, and it is the one worth asserting: a modified hub cannot open the window
      // on a map of its choosing, because the map is whatever the veto left standing.
      const good = await post(cbase, '/api/match/connecting', A,
                              { map: 'function map() { [native code] }', host: me });
      assert.equal(good.status, 200, 'a refusal must not wedge the match');
      const open = await a.wait('match_connecting');
      assert.equal(open.map, lobby.map, 'the veto decided the map, not the body');
      assert.notEqual(open.map, 'function map() { [native code] }');
      assert.equal(open.host, me);

      await post(cbase, '/api/match/leave', A);
      await post(cbase, '/api/match/leave', B);
      await post(cbase, '/api/queue/leave', A);
      await post(cbase, '/api/queue/leave', B);
      a.close(); b.close();
    });

    await test('the connect window cannot be opened out of turn', async () => {
      const a = await openStream(cbase, A);
      await a.wait('hello');
      const res = await post(cbase, '/api/match/connecting', A, { map: 'Rome' });
      assert.equal(res.status, 409);
      const reported = await post(cbase, '/api/match/connected', A);
      assert.equal(reported.status, 409);
      a.close();
    });

    await test('the new match endpoints refuse an unauthenticated caller', async () => {
      for (const path of ['/api/match/connecting', '/api/match/connected']) {
        const res = await fetch(`${cbase}${path}`, { method: 'POST' });
        assert.equal(res.status, 401, path);
      }
    });

  } finally {
    await stopServer(conn.child);
  }

  // ---------------------------------------------------------------- the team ruling
  // Unit tests, not HTTP ones: teamRuling is a pure read over match state, and reaching a
  // `connecting` match through the front door means driving the whole coin/veto lobby. The state is
  // built directly instead, which is also the only way to assert the refusals - a match with no
  // teams at all cannot be produced by the real flow, and that is exactly the case that must not
  // silently answer "team 2".
  {
    const liveModule = await import('../live.cjs').then((m) => m.default || m);
    const L_rating = await import('../rating.cjs').then((m) => m.default || m);
    const L_progress = await import('../progress.cjs').then((m) => m.default || m);
    const stub = () => liveModule.create({
      whoami: async () => null, bearer: () => '', sendJson: () => {}, badRequest: () => {},
      // These direct unit tests use the supported in-memory mode. A configured store must
      // implement durable writes; a function that returns null is a failed store, not no store.
      readBody: async () => Buffer.alloc(0), prefix: 'test',
    });
    const HOST = '76561198000000001';
    const MATE = '76561198000000002';
    const FOE = '76561198000000003';
    const STRANGER = '76561198000000099';

    const withMatch = (over = {}) => {
      const L = stub();
      const { matches, inMatch } = L._internals;
      const match = {
        id: 'm1', state: 'connecting', host: HOST,
        players: [HOST, MATE, FOE].map((id) => ({ steam_id: id, persona: '', accepted: true,
                                                  connected: false })),
        teams: { 1: [HOST, MATE], 2: [FOE] },
        left: [], timer: null, deadline: 0, map: 'Rome',
        ...over,
      };
      matches.set(match.id, match);
      for (const p of match.players) inMatch.set(p.steam_id, match.id);
      return { L, match };
    };

    // ---------------------------------------------------------------- arrival vs release
    // The joiners get ONE lobby search per launch, so releasing them before the host's lobby
    // EXISTS spends it on nothing. What they search for is the lobby's NAME, which the host stamps
    // into "Session Name" before its game ever creates one - so ch_lobby_read (t+12s, "1/10"
    // members, measured) is proof of everything they need. Waiting for GM_BB5's CH_MATCH write at
    // t+30s cost eighteen seconds nobody was using and is why the Launch button used to un-grey
    // only once the host's pre-round was over.
    await test('the host arriving RELEASES the joiners - it is their whole gate', () => {
      const { L, match } = withMatch({ state: 'connecting' });
      const read = L.gameReportedIn(HOST, 'ch_lobby_read');
      assert.equal(read.ok, true, 'the read marks the host arrived');
      assert.equal(read.stamped, true, 'and un-greys Launch: the lobby is up and named');
      assert.ok(read.released > 0, 'THIS is what lets the joiners search');
      assert.equal(match.lobby_stamped, true);
    });

    // ...AND THE JOINER HAS TO HEAR ABOUT IT. (2026-09-16) The release was real for weeks while
    // being completely invisible: match_connect is the only message that carries `stamped`, and it
    // was built in one place gameReportedIn could skip. The test above cannot catch that, because
    // `released` is a return value to the HOST's game and never goes near a joiner.
    await test('the release is BROADCAST to the joiners, not merely recorded', () => {
      const { L, match } = withMatch({ state: 'connecting' });
      const { clients, bySteam } = L._internals;
      const wire = [];
      clients.set('c-mate', { res: { write: (chunk) => wire.push(String(chunk)) },
                              steamId: MATE, persona: '' });
      bySteam.set(MATE, new Set(['c-mate']));
      const stampedFrames = () => wire.filter((f) => f.includes('"type":"match_connect"')
                                                  && f.includes('"stamped":true'));

      L.gameReportedIn(HOST, 'ch_lobby_read');
      assert.ok(stampedFrames().length > 0,
                'the joiner is TOLD at arrival; the Launch button greys on this field');
      assert.equal(match.lobby_stamped, true);
    });

    await test('the later CH_MATCH write releases nobody twice, but re-sends the frame', () => {
      const { L } = withMatch({ state: 'connecting' });
      const { clients, bySteam } = L._internals;
      const wire = [];
      clients.set('c-mate', { res: { write: (chunk) => wire.push(String(chunk)) },
                              steamId: MATE, persona: '' });
      bySteam.set(MATE, new Set(['c-mate']));

      const read = L.gameReportedIn(HOST, 'ch_lobby_read');
      const sent = wire.length;
      const write = L.gameReportedIn(HOST, 'ch_lobby_write');
      assert.ok(read.released > 0);
      assert.equal(write.released, undefined, 'refused as "already in" - there is nothing to do');
      assert.ok(wire.length > sent,
                'but it re-sends the snapshot, for a hub that was reconnecting at t+12');
    });

    await test('the CH_MATCH write with no preceding read still releases', () => {
      // the read is an eighteen-second-earlier timer and can be lost in transit; either arrival
      // event alone has to be sufficient
      const { L } = withMatch({ state: 'connecting' });
      const write = L.gameReportedIn(HOST, 'ch_lobby_write');
      assert.equal(write.ok, true);
      assert.ok(write.released > 0);
    });

    // ---------------------------------------------------------------- the connect clock
    // Sam, 2026-09-16: "the time to connect timer is still ticking down and the timer could run out
    // before the hoster respawns". The deadline is re-armed in three places; none of them used to
    // reach a screen, because only the payload that OPENED the window carried the number.
    await test('progress carries how long is left, so the hubs can re-base their countdown', () => {
      const { L, match } = withMatch({ state: 'connecting', expiry: 'connect',
                                       deadline: Date.now() + 200000 });
      const { clients, bySteam } = L._internals;
      const wire = [];
      clients.set('c-mate', { res: { write: (chunk) => wire.push(String(chunk)) },
                              steamId: MATE, persona: '' });
      bySteam.set(MATE, new Set(['c-mate']));

      const progress = L.gameReportedIn(HOST, 'ch_lobby_read');
      assert.equal(progress.ok, true);
      const frame = wire.find((f) => f.includes('"type":"match_connect"'));
      assert.ok(frame && /"connect_seconds":\d+/.test(frame), 'the remaining seconds go out');
      // the host reporting in re-arms it, so what ships is the FRESH window, not the old 200 s
      assert.ok(match.deadline > Date.now() + 250000, 'and the deadline really was re-armed');
    });

    await test('the teams gate does not masquerade as the connect clock', () => {
      const { L } = withMatch({ state: 'connecting', expiry: 'teams',
                               deadline: Date.now() + 20000,
                               players: [{ steam_id: HOST, persona: '', connected: true }] });
      const { clients, bySteam } = L._internals;
      const wire = [];
      clients.set('c-host', { res: { write: (chunk) => wire.push(String(chunk)) },
                              steamId: HOST, persona: '' });
      bySteam.set(HOST, new Set(['c-host']));
      L.gameReportedIn(HOST, 'ch_lobby_read');
      const frame = wire.find((f) => f.includes('"type":"match_connect"')) || '';
      assert.ok(!frame.includes('connect_seconds'),
                'a 20-second teams gate must never be shown as the connect window');
    });

    // The host's game asking for its travel permit: booted, in the lobby world, about to travel.
    // Not an arrival - but every second of its 40-60 s boot used to come out of the same five
    // minutes the joiners then had to launch inside.
    await test('the host launching extends the connect window without connecting anyone', () => {
      const { L, match } = withMatch({ state: 'connecting', expiry: 'connect',
                                       deadline: Date.now() + 10000 });
      assert.equal(L.noteHostLaunching(HOST), true);
      assert.ok(match.deadline > Date.now() + 100000, 'a fresh window');
      assert.equal(match.players.every((p) => !p.connected), true, 'and nobody is "in" yet');
      assert.equal(match.joiners_released, undefined, 'nor are the joiners released');
    });

    await test('the host launching never shortens the window, and only the host may', () => {
      const far = Date.now() + 900000;
      const { L, match } = withMatch({ state: 'connecting', expiry: 'connect', deadline: far });
      assert.equal(L.noteHostLaunching(HOST), false, 'the deadline is already further out');
      assert.equal(match.deadline, far);
      assert.equal(L.noteHostLaunching(MATE), false, 'a joiner asking proves nothing about the host');
      assert.equal(L.noteHostLaunching(STRANGER), false);
    });

    await test('only the host may report arrival or a stamp', () => {
      const { L } = withMatch({ state: 'connecting' });
      assert.equal(L.gameReportedIn(MATE, 'ch_lobby_write').ok, false);
      assert.equal(L.gameReportedIn(STRANGER, 'ch_lobby_write').ok, false);
    });

    await test('the ruling puts each player on the team the matchmaker chose', () => {
      const { L } = withMatch();
      assert.equal(L.teamRuling(HOST, HOST, 'side').team, 1);
      assert.equal(L.teamRuling(HOST, MATE, 'side').team, 1);
      assert.equal(L.teamRuling(HOST, FOE, 'side').team, 2);
      // 200 is team 1 and 404 is team 2: the bool the game reads must track the team, both ways
      assert.equal(L.teamRuling(HOST, MATE, 'side').yes, true);
      assert.equal(L.teamRuling(HOST, FOE, 'side').yes, false);
    });

    await test('the ruling admits the roster and refuses a stranger', () => {
      const { L } = withMatch();
      assert.equal(L.teamRuling(HOST, MATE, 'member').yes, true);
      assert.equal(L.teamRuling(HOST, STRANGER, 'member').yes, false);
      // ...and a stranger has no side to be asked about, rather than defaulting to one
      assert.equal(L.teamRuling(HOST, STRANGER, 'side').ok, false);
    });

    await test('the kick ask is positive: only a known stranger is a yes', () => {
      const { L } = withMatch();
      // the whole reason this ask exists - a refusal must not read as "kick them"
      assert.equal(L.teamRuling(HOST, STRANGER, 'stranger').yes, true, 'a real outsider');
      assert.equal(L.teamRuling(HOST, MATE, 'stranger').yes, false, 'a team mate is never a kick');
      assert.equal(L.teamRuling(HOST, HOST, 'stranger').yes, false, 'nor is the host itself');

      // Every refusal below answers 409, which the pak reads as false - so nobody is kicked when
      // the backend cannot rule. Asserting `ok` is false is asserting the kick does not happen.
      assert.equal(L.teamRuling(MATE, STRANGER, 'stranger').ok, false, 'a non-host may not kick');
      assert.equal(L.teamRuling(STRANGER, STRANGER, 'stranger').ok, false, 'nor may an outsider');
      assert.equal(withMatch({ state: 'ready' }).L.teamRuling(HOST, STRANGER, 'stranger').ok, false,
                   'not before the connect window opens');
      assert.equal(L.teamRuling(HOST, '', 'stranger').ok, false, 'a bot sends an empty id');
      assert.equal(L.teamRuling(HOST, 'nonsense', 'stranger').ok, false);

      // A match with no decided teams can still say who belongs: a stranger is a roster question,
      // not a team one, so the refusal that protects `side` must NOT disarm the kick.
      const noTeams = withMatch({ teams: null, lobby: null });
      assert.equal(noTeams.L.teamRuling(HOST, STRANGER, 'stranger').yes, true);
      assert.equal(noTeams.L.teamRuling(HOST, MATE, 'stranger').yes, false);

      // and a live match still kicks - this is the exact scenario the feature is for: two players
      // with the pak inviting an outsider into a match that is already running.
      assert.equal(withMatch({ state: 'live' }).L.teamRuling(HOST, STRANGER, 'stranger').yes, true);
    });

    await test('a leaver becomes kickable, and the two asks never disagree', () => {
      const { L, match } = withMatch();
      match.players = match.players.filter((p) => p.steam_id !== FOE);
      match.left = [FOE];
      assert.equal(L.teamRuling(HOST, FOE, 'stranger').yes, true, 'a leaver who walks back in');
      for (const id of [HOST, MATE, FOE, STRANGER]) {
        const m = L.teamRuling(HOST, id, 'member');
        const k = L.teamRuling(HOST, id, 'stranger');
        assert.equal(k.yes, !m.yes, `member and stranger must be opposites for ${id}`);
      }
    });

    await test('only the host of a connecting or live match may ask', () => {
      const { L } = withMatch();
      assert.equal(L.teamRuling(MATE, FOE, 'side').ok, false, 'a non-host may not ask');
      assert.equal(L.teamRuling(STRANGER, FOE, 'side').ok, false, 'an outsider may not ask');
      const ready = withMatch({ state: 'ready' });
      assert.equal(ready.L.teamRuling(HOST, FOE, 'side').ok, false, 'not before the window opens');
      const live = withMatch({ state: 'live' });
      assert.equal(live.L.teamRuling(HOST, FOE, 'side').team, 2, 'a live match still rules');
    });

    await test('a match with no decided teams refuses instead of guessing a side', () => {
      const { L } = withMatch({ teams: null, lobby: null });
      const r = L.teamRuling(HOST, MATE, 'side');
      assert.equal(r.ok, false);
      // the distinction that matters: still a MEMBER, just not placeable
      assert.equal(L.teamRuling(HOST, MATE, 'member').yes, true);
    });

    await test('the ruling refuses malformed ids and unknown asks', () => {
      const { L } = withMatch();
      assert.equal(L.teamRuling('nonsense', MATE, 'side').ok, false);
      assert.equal(L.teamRuling(HOST, 'nonsense', 'side').ok, false);
      assert.equal(L.teamRuling(HOST, MATE, 'whatever').ok, false);
    });

    await test('someone who walked out of the match is no longer on the roster', () => {
      const { L, match } = withMatch();
      match.players = match.players.filter((p) => p.steam_id !== FOE);
      match.left = [FOE];
      assert.equal(L.teamRuling(HOST, FOE, 'member').yes, false, 'a leaver is a stranger again');
    });

    // ---------------------------------------------------------------- the start gate
    //
    // "lets implement logic that the game also only starts if the teams in-game match the team
    // distribution in the hub" (Sam, 2026-09-15). Everyone being in the match world was the old
    // condition; agreeing about who is on which side is the new one.
    //
    // The roster here is HOST + MATE on our team 1 and FOE on our team 2. The GAME's ids are its
    // own - 0 and 1 in every match measured - and the host pins which of them is our 1.
    const allIn = (match) => { for (const p of match.players) p.connected = true; };

    await test('incremental agreement is diagnostic until the complete start snapshot', () => {
      const { L, match } = withMatch({ state: 'connecting' });
      allIn(match);
      L.gameReportedTeam(HOST, { subject: HOST, team: 0 });
      L.gameReportedTeam(HOST, { subject: MATE, team: 0 });
      assert.equal(match.state, 'connecting', 'two of three reported: not yet');
      const last = L.gameReportedTeam(HOST, { subject: FOE, team: 1 });
      assert.equal(last.agree, true);
      assert.equal(match.state, 'connecting', 'an accumulated report set cannot start it');
    });

    await test('the game disagreeing with the lobby HOLDS the start', () => {
      const { L, match } = withMatch({ state: 'connecting' });
      allIn(match);
      L.gameReportedTeam(HOST, { subject: HOST, team: 0 });
      // MATE is on our team 1 with the host, but the game has put them with FOE
      L.gameReportedTeam(HOST, { subject: MATE, team: 1 });
      const last = L.gameReportedTeam(HOST, { subject: FOE, team: 1 });
      assert.equal(last.agree, false);
      assert.equal(match.state, 'connecting', 'the match does NOT start');
      const v = L.teamsAgree(match);
      assert.equal(v.wrong.length, 1, 'and it names exactly who is on the wrong side');
      assert.equal(v.wrong[0].steam_id, MATE);
      assert.equal(v.wrong[0].want, 1);
      assert.equal(v.wrong[0].got, 2);
    });

    await test('the in-game ids may be anything: the host pins the mapping', () => {
      // 7 and 9 rather than 0 and 1. Nothing in the gate may depend on the numbers themselves -
      // we never call SetTeamId, so the game chooses them and they mean nothing on their own.
      const { L, match } = withMatch({ state: 'connecting' });
      allIn(match);
      L.gameReportedTeam(HOST, { subject: HOST, team: 9 });
      L.gameReportedTeam(HOST, { subject: MATE, team: 9 });
      L.gameReportedTeam(HOST, { subject: FOE, team: 7 });
      assert.equal(match.state, 'connecting');
      assert.equal(L.teamsAgree(match).host_ingame_team, 9);
      assert.equal(L.teamsAgree(match).host_side, 1);
    });

    await test('a team id of -1 is "not yet", never a team', () => {
      // ABodycamPlayerState::TeamID is -1 for the first ~30 s of the match world (measured
      // 2026-09-15). Reading that as a team would fail every match on its first sweep.
      const { L, match } = withMatch({ state: 'connecting' });
      allIn(match);
      L.gameReportedTeam(HOST, { subject: HOST, team: 0 });
      L.gameReportedTeam(HOST, { subject: MATE, team: 0 });
      const early = L.gameReportedTeam(HOST, { subject: FOE, team: -1 });
      assert.equal(early.ready, false);
      assert.equal(match.state, 'connecting', 'an unassigned player is not a disagreement');
      assert.equal(L.teamsAgree(match).missing.length, 1);

      // ...and a player who HAD a team and goes back to -1 is forgotten rather than left stale
      L.gameReportedTeam(HOST, { subject: FOE, team: 1 });
      assert.equal(match.state, 'connecting');
      assert.equal(L.teamsAgree(match).agree, true);
    });

    await test('everyone on one in-game team is a disagreement, not a match', () => {
      const { L, match } = withMatch({ state: 'connecting' });
      allIn(match);
      for (const id of [HOST, MATE, FOE]) L.gameReportedTeam(HOST, { subject: id, team: 0 });
      assert.equal(match.state, 'connecting');
      assert.match(L.teamsAgree(match).reason, /1 team id/);
    });

    await test('the gate waits for reports even when the last player arrives first', () => {
      const { L, match } = withMatch({ state: 'connecting' });
      allIn(match);
      const started = L._internals.goLiveIfReady(match);
      assert.equal(started.started, false);
      assert.equal(started.gated, true);
      assert.equal(match.state, 'connecting');
      assert.notEqual(match.teams_gate_armed, true);
    });

    await test('the gate does not start a match that is not fully connected', () => {
      const { L, match } = withMatch({ state: 'connecting' });
      L.gameReportedTeam(HOST, { subject: HOST, team: 0 });
      L.gameReportedTeam(HOST, { subject: MATE, team: 0 });
      L.gameReportedTeam(HOST, { subject: FOE, team: 1 });
      assert.equal(match.state, 'connecting', 'teams agreeing is not a substitute for being in');
    });

    await test('only the host may report a team, and only about the roster', () => {
      const { L } = withMatch({ state: 'connecting' });
      assert.equal(L.gameReportedTeam(MATE, { subject: FOE, team: 0 }).ok, false, 'a non-host');
      assert.equal(L.gameReportedTeam(STRANGER, { subject: FOE, team: 0 }).ok, false, 'an outsider');
      assert.equal(L.gameReportedTeam(HOST, { subject: STRANGER, team: 0 }).ok, false, 'a stranger');
      assert.equal(L.gameReportedTeam('nonsense', { subject: FOE, team: 0 }).ok, false);
      assert.equal(L.gameReportedTeam(HOST, { subject: FOE, team: 'x' }).ok, false, 'no team id');
    });

    await test('a legacy gate timeout cancels instead of starting an unconfirmed match', () => {
      const { L, match } = withMatch({ state: 'connecting' });
      allIn(match);
      L.gameReportedTeam(HOST, { subject: HOST, team: 0 });
      L.gameReportedTeam(HOST, { subject: MATE, team: 1 });
      L.gameReportedTeam(HOST, { subject: FOE, team: 1 });
      assert.equal(match.state, 'connecting', 'held first');
      assert.notEqual(match.teams_gate_armed, true, 'no bypass timer is armed');

      L._internals.expireTeamsGate(match.id);
      assert.equal(match.state, 'cancelled', 'a timeout cannot authorize a start');
    });

    await test('even late incremental agreement cannot make an expired gate start', () => {
      const { L, match } = withMatch({ state: 'connecting' });
      allIn(match);
      L.gameReportedTeam(HOST, { subject: HOST, team: 0 });
      L.gameReportedTeam(HOST, { subject: MATE, team: 1 });
      assert.equal(match.state, 'connecting');
      match.ingame.set(MATE, 0); match.ingame.set(FOE, 1);   // the sweep fixed it in the meantime
      L._internals.expireTeamsGate(match.id);
      assert.equal(match.state, 'cancelled');
    });

    // THE VISIBLE LADDER IS progress.cjs's, and only progress.cjs's. These used to be asserted
    // against rating.rankOf, which named a rank from the player's matchmaking rating - a second answer to
    // the same question, from a module that had no business answering it.
    await test('the visible rank: ranks, divisions and 0-99 RR', () => {
      const P = L_progress;
      const l = P.ranks();

      // RR is 0 at the bottom of a division and never reaches 100 - reaching it IS the promotion.
      // BELOW THE COUNTING BAND ONLY: from `counting_at` up the figure stops resetting per
      // division and runs as one count, which is the next test.
      const counting = l.counting_at / l.rr_per_division;
      for (let band = 0; band < counting; band += 1) {
        const bottom = band * l.rr_per_division;
        assert.equal(P.withinLevel(bottom), 0, `band ${band} does not start at 0 RR`);
        const top = bottom + l.rr_per_division - 1;
        assert.ok(P.withinLevel(top) < 100, `band ${band} reached ${P.withinLevel(top)} RR`);
        assert.equal(P.stepOf(top), band, `band ${band} leaked into the next one early`);
      }

      // a division is 1-based within its rank, and the rank name follows it
      assert.equal(P.rankName(0), l.names[0]);
      assert.equal(P.divisionOf(0), 1, 'the bottom is Rookie 1, not Rookie 0');
      assert.equal(P.rankName(l.rr_per_division), l.names[0]);
      assert.equal(P.divisionOf(l.rr_per_division), 2);
      const secondRank = l.divisions * l.rr_per_division;
      assert.equal(P.rankName(secondRank), l.names[1], 'three divisions then the next rank');
      assert.equal(P.divisionOf(secondRank), 1);
    });

    // Sam, 2026-09-16: "lets swap it so a penalty doesnt cost any matchmaking rating and instead
    // costs RR". It used to come off the Glicko rating, which is the one number we have promised
    // never to show anybody - so the punishment was invisible on the day it landed and only
    // surfaced over the following matches, as convergence dragged the rank down after it.
    await test('a penalty comes off RR, and off nothing else', () => {
      const before = { rating: 1500, rd: 120, vol: 0.06, matches: 40, wins: 20, losses: 20,
                       progress: 250 };
      const hit = L_progress.penalise(before, 25);
      assert.equal(hit.delta, -25);
      assert.equal(hit.progress, 225);
      // ...and the hidden half is untouched. This is the assertion the swap exists for.
      const after = L_rating.normalise({ ...before, progress: hit.progress });
      assert.equal(after.rating, 1500, 'a no-show teaches the matchmaker nothing');
      assert.equal(after.rd, L_rating.normalise(before).rd);
      assert.equal(after.matches, 40);
      assert.equal(after.wins, 20);
      assert.equal(after.losses, 20);
    });

    await test('a penalty cannot take RR a player does not have', () => {
      const hit = L_progress.penalise({ rating: 1500, matches: 40, progress: 10 }, 25);
      assert.equal(hit.progress, 0, 'the floor holds');
      assert.equal(hit.delta, -10, 'and it reports what it actually took');
    });

    await test('a placing player loses no RR, because they have none yet', () => {
      // The queue ban is the whole of the penalty until they have placed - there is no visible
      // rank to take it off, and moving one they cannot see is how a rank arrives from nowhere.
      const hit = L_progress.penalise({ rating: 1500, matches: 1, progress: 0 }, 25);
      assert.equal(hit.delta, 0);
      assert.equal(hit.placing, true);
    });

    await test('demotion protection does not soften a penalty', () => {
      // Division protection exists so a bad MATCH cannot drop you out of a division on its own.
      // A penalty is not a match, and holding somebody at 0 RR after they abandoned nine other
      // people would make the last rung of every division free.
      const atFloor = { rating: 1500, matches: 40, progress: 200, demoteArmed: false };
      const hit = L_progress.penalise(atFloor, 25);
      assert.equal(hit.progress, 175, 'it drops out of the division it was pinned to');
      assert.ok(hit.divisionChanged < 0);
    });

    await test('the rank ladder has no gaps and no unreachable bands', () => {
      const P = L_progress;
      const l = P.ranks();
      const seen = new Set();
      for (let rr = 0; rr < l.capstone_at; rr += 1) {
        seen.add(P.rankName(rr) + ' ' + P.divisionOf(rr));
      }
      assert.equal(seen.size, l.bands, `${seen.size} distinct divisions, expected ${l.bands}`);
    });

    await test('below the floor is the bottom rank, not a negative one', () => {
      const P = L_progress;
      const l = P.ranks();
      assert.equal(P.rankName(-5000), l.names[0]);
      assert.equal(P.divisionOf(-5000), 1);
      assert.equal(P.withinLevel(-5000), 0, 'there is nothing under Rookie 1 to fall into');
    });

    await test('the top rank counts RR instead of resetting it every division', () => {
      // Sam, 2026-09-16, for the last named rank: "spectre 1 ... until 99 RR. then at 100 RR they
      // become spectre 2 ... Then once a user hits 200 RR+ they become Spectre 3." Valorant's
      // Immortal, and the reason the board can order the top of the ladder at all.
      const P = L_progress;
      const l = P.ranks();
      const band = l.counting_at;
      assert.equal(band, (l.names.length - 1) * l.divisions * l.rr_per_division,
                   'the counting band is the floor of the LAST named rank');
      assert.equal(P.rankName(band), l.names[l.names.length - 1]);
      assert.equal(l.counting_rank, l.names.length);
      for (let d = 1; d <= l.divisions; d += 1) {
        const at = band + (d - 1) * l.rr_per_division;
        assert.equal(P.divisionOf(at), d, `${l.rr_per_division * (d - 1)} RR is division ${d}`);
        assert.equal(P.withinLevel(at), (d - 1) * l.rr_per_division,
                     'and the figure does not reset into the new division');
      }
      // The last division has NO CEILING: it is 200+ at 200 and still 200+ at 4000.
      assert.equal(P.divisionOf(band + 4000), l.divisions, 'there is no division 4');
      assert.equal(P.withinLevel(band + 4000), 4000, 'the figure is uncapped');
      assert.equal(P.bdrOf(band + 4000), 4000, 'and BDR is that same count under its old name');
      assert.equal(P.bdrOf(band - 1), null, 'which does not exist below the band');
    });

    await test('the capstone is 150 seats, and RR alone can never award it', () => {
      // "once a user hits 300 RR, they become eligible to become reaper if their RR is in the top
      // 150 highest RR values on the leaderboard" (Sam, 2026-09-16). Eligibility is a number and
      // this module can answer it; the SEAT is a leaderboard question, so live.cjs passes it in.
      const P = L_progress;
      const l = P.ranks();
      assert.equal(l.capstone_at, l.counting_at + l.top_at);
      assert.equal(l.top_slots, 150);
      assert.equal(P.rankOf(l.capstone_at), l.names.length, 'still the last NAMED rank');
      assert.equal(P.rankOf(l.capstone_at + 99999), l.names.length, 'however much RR they have');
      assert.equal(P.rankName(l.capstone_at + 99999), l.names[l.names.length - 1]);
      assert.equal(P.rankOf(0), 1, 'and the bottom is rank 1');
      assert.ok(!P.reaperEligible(l.capstone_at - 1));
      assert.ok(P.reaperEligible(l.capstone_at));

      const rec = { rating: 2000, matches: 99, progress: l.capstone_at + 12 };
      const seated = P.publicProgress(rec, { top: true });
      assert.equal(seated.rank_name, l.top);
      assert.equal(seated.rank, l.names.length + 1, 'the rung ABOVE the last named one');
      assert.equal(seated.division, null, 'Radiant is not Radiant 1');
      assert.equal(seated.rr, l.top_at + 12, 'and it goes on counting in the same figure');
      const unseated = P.publicProgress(rec, { top: false });
      assert.equal(unseated.rank_name, l.names[l.names.length - 1], 'outside the seats');
      assert.equal(unseated.top_eligible, true, 'but told why, so the UI can explain it');
      assert.equal(P.publicProgress({ ...rec, progress: l.capstone_at - 1 }, { top: true }).top,
                   false, 'and a bad cut cannot seat someone short of the RR');
    });

    await test('a placing player has no visible rank at all', () => {
      const P = L_progress;
      const pub = P.publicProgress({ rating: 1500, matches: 0 });
      assert.equal(pub.placing, true);
      assert.equal(pub.rank, null);
      assert.equal(pub.level, null);
      assert.equal(pub.rank_name, '');
      // and the hidden half says so the same way, so nothing has to learn a second shape
      assert.equal(L_rating.levelOf({ rating: 1500, matches: 0 }), null);
      const placed = P.publicProgress({ rating: 1500, matches: 99, progress: 450 });
      assert.ok(placed.rank !== null && placed.rank_name);
    });

    // A VISIBLE RANK PLAYERS SEE AND A matchmaking rating THEY DO NOT (Sam, 2026-09-15). The board is
    // where the two used to collide: it was ORDERED by the Glicko-2 rating and LABELLED from it,
    // so it could seat a player above somebody whose visible rank was higher, and it shipped the
    // hidden number to the client in every row.
    // An in-memory stand-in for the store, shared by the board tests below. Only the handful of
    // commands the leaderboard actually uses, and they behave the way Upstash's do - including
    // ZREVRANGE WITHSCORES returning one flat array rather than pairs.
    const memoryStore = () => {
      const mem = { kv: new Map(), z: new Map(), sets: new Map() };
      return async (cmd) => {
        const [op, key, ...rest] = cmd;
        if (op === 'EVAL') return rankScript(key, rest,
          { strings: mem.kv, boards: mem.z, sets: mem.sets });
        if (op === 'SET') { mem.kv.set(key, rest[0]); return 'OK'; }
        if (op === 'GET') { return mem.kv.has(key) ? mem.kv.get(key) : null; }
        if (op === 'SMEMBERS') return [...(mem.sets.get(key) || [])];
        if (op === 'ZADD') {
          const z = mem.z.get(key) || new Map();
          z.set(String(rest[1]), Number(rest[0]));
          mem.z.set(key, z);
          return 1;
        }
        if (op === 'ZREM') return Number(mem.z.get(key)?.delete(String(rest[0])) || 0);
        if (op === 'ZREVRANK') {
          const rows = [...(mem.z.get(key) || new Map())]
            .sort((a, b) => b[1] - a[1] || b[0].localeCompare(a[0]));
          const position = rows.findIndex(([id]) => id === String(rest[0]));
          return position < 0 ? null : position;
        }
        if (op === 'ZREVRANGE') {
          const z = [...(mem.z.get(key) || new Map())]
            .sort((a, b) => b[1] - a[1] || b[0].localeCompare(a[0]));
          const page = z.slice(Number(rest[0]), Number(rest[1]) + 1);
          // WITHSCORES comes back as Upstash returns it: a FLAT [member, score, member, ...].
          // The capstone cut reads this form, and a fake that ignored the flag would let a
          // mis-parse pass here and only fail against the real store.
          if (String(rest[2] || '').toUpperCase() === 'WITHSCORES') {
            return page.flatMap(([member, score]) => [member, String(score)]);
          }
          return page.map(([member]) => member);
        }
        if (op === 'ZCOUNT') {
          const min = String(rest[0]);
          const exclusive = min.startsWith('(');
          const lo = Number(exclusive ? min.slice(1) : min);
          const z = mem.z.get(key) || new Map();
          return [...z.values()].filter((v) => (exclusive ? v > lo : v >= lo)).length;
        }
        return null;
      };
    };

    await test('the leaderboard is ordered by the VISIBLE rank, not by matchmaking rating', async () => {
      const L = liveModule.create({
        whoami: async () => null, bearer: () => '', sendJson: () => {}, badRequest: () => {},
        readBody: async () => Buffer.alloc(0), upstashCmd: memoryStore(), prefix: 'lb',
      });
      const CLIMBED = '76561198000000011';   // poor MMR, but they have climbed the ladder
      const STRONG = '76561198000000012';    // strong MMR, sitting near the bottom of it
      L._internals.saveRating(CLIMBED,
        { rating: 1000, rd: 60, matches: 30, wins: 15, losses: 15, progress: 2000 });
      L._internals.saveRating(STRONG,
        { rating: 2300, rd: 60, matches: 30, wins: 20, losses: 10, progress: 300 });
      // a placing player is never indexed: their rating is real, their RANK is not shown yet
      L._internals.saveRating('76561198000000013',
        { rating: 2400, rd: 60, matches: 1, wins: 1, losses: 0, progress: 0 });
      await Promise.all([CLIMBED, STRONG, '76561198000000013'].map(L._internals.drainRatingWrites));

      const board = await L._internals.leaderboard(CLIMBED, 10);
      assert.equal(board.available, true);
      assert.equal(board.rows.length, 2, 'the placing player must not be on the board');
      assert.equal(board.rows[0].steam_id, CLIMBED,
                   'RR orders the board - a higher MMR must NOT outrank a higher visible rank');
      assert.equal(board.rows[0].rank_name, 'Nightmare');
      assert.equal(board.rows[0].division, 3);
      assert.equal(board.rows[1].steam_id, STRONG);
      assert.equal(board.rows[1].rank_name, 'Private');
      assert.equal(board.rows[1].division, 1);
      // ...and the hidden number does not leave the server
      assert.equal('rating' in board.rows[0], false, 'MMR must not be shipped to a client');
      assert.equal(board.rows[0].progress, 2000, 'the visible total is what it is sorted on');
      // the board's labels are the SAME call the hero and the match result are drawn from
      const k = L_progress.publicProgress({ rating: 1000, matches: 30, progress: 2000 });
      assert.equal(board.rows[0].rank_name, k.rank_name);
      assert.equal(board.rows[0].rr, k.rr);
    });

    // THE CAPSTONE IS THE TOP OF THE BOARD, NOT THE TOP OF THE LADDER (Sam, 2026-09-16: "once a
    // user hits 300 RR, they become eligible to become reaper if their RR is in the top 150
    // highest RR values on the leaderboard"). progress.cjs cannot answer that about one player's
    // record, so live.cjs reads the cut off the board and hands it in.
    await test('the capstone goes to the top N of the board, and only to them', async () => {
      const L = liveModule.create({
        whoami: async () => null, bearer: () => '', sendJson: () => {}, badRequest: () => {},
        readBody: async () => Buffer.alloc(0), upstashCmd: memoryStore(), prefix: 'rp',
      });
      const { REAPER_AT, REAPER_SLOTS, isReaper, refreshReaperCut, reaperState } = L._internals;
      const id = (n) => `7656119900000${String(n).padStart(4, '0')}`;
      const save = (n, progress) => L._internals.saveRating(id(n),
        { rating: 1800, rd: 60, matches: 40, wins: 20, losses: 20, progress });

      // WITH SEATS TO SPARE, eligibility IS the capstone: a board of three cannot have a 150th
      // place to miss out on, and refusing everyone would be a rule nobody could satisfy.
      save(1, REAPER_AT + 500);
      save(2, REAPER_AT);
      save(3, REAPER_AT - 1);          // one RR short, and that is the whole difference
      await Promise.all([1, 2, 3].map(n => L._internals.drainRatingWrites(id(n))));
      await refreshReaperCut(true);
      assert.equal(reaperState().full, false);
      assert.equal(isReaper(id(1), REAPER_AT + 500), true);
      assert.equal(isReaper(id(2), REAPER_AT), true);
      assert.equal(isReaper(id(3), REAPER_AT - 1), false, 'eligibility is still a hard floor');

      // NOW FILL IT. Every seat taken, and two eligible players left outside: the badge is what
      // the board has room for, so climbing past somebody is the only way in.
      for (let n = 10; n < 10 + REAPER_SLOTS + 2; n += 1) save(n, REAPER_AT + n);
      await Promise.all(Array.from({ length: REAPER_SLOTS + 2 },
        (_, i) => L._internals.drainRatingWrites(id(i + 10))));
      await refreshReaperCut(true);
      const cut = reaperState();
      assert.equal(cut.full, true);
      assert.equal(cut.ids.length, REAPER_SLOTS, 'exactly the seats, never more');
      // Player 2 was seated a moment ago on exactly REAPER_AT and has been pushed out by the
      // crowd without losing a single RR. That is the rank working, not a bug: it is 150 seats.
      assert.equal(isReaper(id(2), REAPER_AT), false,
                   'eligible, and outside the top slots: no capstone');
      assert.equal(isReaper(id(1), REAPER_AT + 500), true, 'the top of the board holds it');
      assert.equal(isReaper(id(10 + REAPER_SLOTS + 1), REAPER_AT + 10 + REAPER_SLOTS + 1), true,
                   'and so does everyone above the cut');
      // Somebody who climbs past the cut between reads has the seat already - the snapshot is
      // what is stale, not their rank.
      assert.equal(isReaper('76561199999999999', cut.cut + 1), true);
      assert.equal(isReaper('76561199999999999', cut.cut - 1), false);

      // AND THE BOARD SAYS SO IN ITS OWN ROWS, which decide it from the position rather than the
      // cached cut - the one place that does not need the cut at all.
      const board = await L._internals.leaderboard(id(1), 5);
      assert.equal(board.rows[0].top, true, 'the top of the board wears the capstone');
      assert.equal(board.rows[0].division, null, 'which is the rank with no division');
      assert.equal(board.rows[0].rr, REAPER_AT + 500 - L_progress.COUNT_AT,
                   'and its figure is the running count, not a 0-99 slice of a division');
      assert.equal(board.rows[0].counting, true);
    });

    await test('the ladder is published as data, not described', () => {
      // The hub explains ranked from THIS, because every band is an env dial. A client shipping
      // its own table would keep explaining the old bands the day one is retuned, and nothing
      // would fail to make that visible.
      const rating = L_rating;
      const l = rating.ladder();
      assert.ok(Array.isArray(l.levels) && l.levels.length >= 2);
      assert.equal(l.levels[0].level, 1);
      // contiguous: every band starts where the last one ended, or a rating falls in no band
      for (let i = 1; i < l.levels.length; i += 1) {
        assert.equal(l.levels[i].min, l.levels[i - 1].max + 1,
                     `gap between level ${i} and ${i + 1}`);
      }
      assert.equal(l.levels[l.levels.length - 1].max, null, 'the top band has no ceiling');
      // and the published bands must agree with levelOf, or the UI explains one system while the
      // server runs another
      for (const band of l.levels) {
        assert.equal(rating.levelOf({ rating: band.min, matches: 99 }), band.level,
                     `levelOf disagrees at the bottom of level ${band.level}`);
        if (band.max !== null) {
          assert.equal(rating.levelOf({ rating: band.max, matches: 99 }), band.level,
                       `levelOf disagrees at the top of level ${band.level}`);
        }
      }
      assert.equal(rating.levelOf({ rating: l.start_rating, matches: 0 }), null,
                   'a placing player has no level');
    });

    // ---------------------------------------------------------------- account bans
    await test('a ban is tied to the steam account, and only an admin can set one', async () => {
      const L = stub();
      assert.equal((await L.banAccount(MATE, { steam_id: FOE })).ok, false, 'not an admin');
      L._internals.ADMIN_IDS.add(HOST);
      try {
        const r = await L.banAccount(HOST, { steam_id: FOE, reason: 'cheating' });
        assert.equal(r.ok, true);
        assert.ok(L.banOf(FOE), 'the ban is live');
        assert.equal(L.banOf(FOE).by, HOST, 'and it records WHO, because a ban is a decision');
        assert.equal(L.banOf(FOE).until, 0, 'no days given means permanent');
        assert.equal(L.banOf(MATE), null, 'nobody else is touched');
      } finally {
        L._internals.ADMIN_IDS.delete(HOST);
      }
    });

    await test('a ban expires on its own when one was given days', async () => {
      const L = stub();
      L._internals.ADMIN_IDS.add(HOST);
      try {
        await L.banAccount(HOST, { steam_id: FOE, days: 7, reason: 'griefing' });
        assert.ok(L.banOf(FOE).until > Date.now());
        // stored as an absolute instant, so it cannot restart itself on a redeploy
        L._internals.bans.get(FOE).until = Date.now() - 1000;
        assert.equal(L.banOf(FOE), null, 'an expired ban is not a ban');
      } finally {
        L._internals.ADMIN_IDS.delete(HOST);
      }
    });

    await test('an admin cannot be banned, and an unban lifts it', async () => {
      const L = stub();
      L._internals.ADMIN_IDS.add(HOST);
      L._internals.ADMIN_IDS.add(MATE);
      try {
        assert.equal((await L.banAccount(HOST, { steam_id: MATE })).ok, false,
                     'banning an admin is how a console locks itself out');
        await L.banAccount(HOST, { steam_id: FOE, reason: 'x' });
        assert.ok(L.banOf(FOE));
        assert.equal((await L.unbanAccount(HOST, { steam_id: FOE })).ok, true);
        assert.equal(L.banOf(FOE), null);
        assert.equal((await L.unbanAccount(FOE, { steam_id: FOE })).ok, false, 'not an admin');
      } finally {
        L._internals.ADMIN_IDS.delete(HOST);
        L._internals.ADMIN_IDS.delete(MATE);
      }
    });

    await test('banning someone pulls them out of the queue immediately', async () => {
      // A ban that waits until they close the app is not a ban.
      const L = stub();
      L._internals.ADMIN_IDS.add(HOST);
      try {
        L._internals.enqueue([FOE], '', Date.now());
        assert.equal(L._internals.queuedPlayers(), 1);
        await L.banAccount(HOST, { steam_id: FOE, reason: 'x' });
        assert.equal(L._internals.queuedPlayers(), 0);
      } finally {
        L._internals.ADMIN_IDS.delete(HOST);
      }
    });

    await test('the console can watch a live match round by round', async () => {
      const L = stub();
      const { matches, inMatch } = L._internals;
      const match = {
        id: 'lv1', state: 'live', host: HOST, map: 'Rome',
        players: [HOST, MATE, FOE].map((id) => ({ steam_id: id, persona: '', accepted: true,
                                                  connected: true })),
        teams: { 1: [HOST, MATE], 2: [FOE] }, left: [], timer: null, deadline: 0,
      };
      matches.set(match.id, match);
      for (const p of match.players) inMatch.set(p.steam_id, match.id);
      L.gameReportedScore(HOST, { scores: '0|0:1|1:0', limit: '7' });
      L.gameReportedScore(HOST, { scores: '0|0:1|1:1', limit: '7' });

      L._internals.ADMIN_IDS.add(HOST);
      try {
        const view = await L.adminOverview(HOST, {});
        const m = view.matches.find((x) => x.id === 'lv1');
        assert.ok(m, 'the live match is listed');
        assert.equal(m.map, 'Rome');
        assert.deepEqual(m.rounds.map((r) => r.won), [1, 2], 'how it is going, not just where it stands');
        assert.equal(m.players.length, 3);
        assert.equal(m.players[0].team, 1, 'with the side each player is on');
      } finally {
        L._internals.ADMIN_IDS.delete(HOST);
      }
    });

    // ---------------------------------------------------------------- admin console
    await test('an empty admin list means NOBODY, not everybody', async () => {
      // An unset variable is the state a fresh deploy is in, and that state must be closed.
      const L = stub();
      assert.equal(L._internals.ADMIN_IDS.size, 0, 'the suite must run with no admins configured');
      assert.equal(L.isAdmin(HOST), false);
      const r = await L.adminOverview(HOST, {});
      assert.equal(r.ok, false);
      assert.equal(r.reported, undefined, 'a refusal must not leak the payload');
    });

    await test('an admin sees the console; nobody else does', async () => {
      const L = stub();
      L._internals.ADMIN_IDS.add(HOST);
      try {
        const mine = await L.adminOverview(HOST, {});
        assert.equal(mine.ok, true);
        assert.ok(Array.isArray(mine.reported) && Array.isArray(mine.team_kills));
        assert.equal((await L.adminOverview(MATE, {})).ok, false, 'a normal player is refused');
        assert.equal((await L.adminOverview('', {})).ok, false);
      } finally {
        L._internals.ADMIN_IDS.delete(HOST);
      }
    });

    await test('the console ranks by DISTINCT reporters, not raw count', async () => {
      // Ten reports from one person is one person with a grudge; three from three is a pattern.
      // Sorting on the raw count puts the grudge at the top of a moderator's day.
      const L = stub();
      const { matches, inMatch } = L._internals;
      const match = {
        id: 'am1', state: 'live', host: HOST,
        players: [HOST, MATE, FOE, STRANGER].map((id) => ({ steam_id: id, persona: '',
                                                            accepted: true, connected: true })),
        teams: { 1: [HOST, MATE], 2: [FOE, STRANGER] }, left: [], timer: null, deadline: 0,
      };
      matches.set(match.id, match);
      for (const p of match.players) inMatch.set(p.steam_id, match.id);

      // FOE: one reporter. STRANGER: two different reporters.
      await L.reportPlayer(HOST, { target: FOE, reason: 'cheating' });
      await L.reportPlayer(HOST, { target: STRANGER, reason: 'griefing' });
      await L.reportPlayer(MATE, { target: STRANGER, reason: 'afk' });

      L._internals.ADMIN_IDS.add(HOST);
      try {
        const view = await L.adminOverview(HOST, {});
        assert.equal(view.reported[0].steam_id, STRANGER, 'two reporters must outrank one');
        assert.equal(view.reported[0].reporters, 2);
        assert.equal(view.reported[0].total, 2);
      } finally {
        L._internals.ADMIN_IDS.delete(HOST);
      }
    });

    await test('the console says whether enforcement is actually on', async () => {
      // A moderator reading team-kill verdicts has to know whether anything came of them.
      const L = stub();
      L._internals.ADMIN_IDS.add(HOST);
      try {
        assert.equal((await L.adminOverview(HOST, {})).notes.enforcement, 'off');
        process.env.COMP_TK_ENFORCE = '1';
        assert.equal((await L.adminOverview(HOST, {})).notes.enforcement, 'on');
      } finally {
        delete process.env.COMP_TK_ENFORCE;
        L._internals.ADMIN_IDS.delete(HOST);
      }
    });

    // ---------------------------------------------------------------- friends
    //
    // Keyed by SteamID64 and persisted, so signing in elsewhere keeps your list. Reached by a
    // friend code so nobody has to hand out an id that identifies them everywhere else on Steam.
    await test('a friend code is stable, and finds its owner', async () => {
      const L = stub();
      const code = await L.friendCode(HOST);
      const len = L._internals.FRIEND_CODE_LEN;
      assert.equal(code.replace('-', '').length, len);
      assert.match(code, /^[A-Z2-9]+-[A-Z2-9]+$/);
      assert.equal(await L.friendCode(HOST), code, 'asking twice must not mint a new one');
      assert.equal(await L._internals.ownerOfCode(code), HOST);
      // typed the way a person types it: lower case, spaces, no dash
      assert.equal(await L._internals.ownerOfCode(code.replace('-', ' ').toLowerCase()), HOST);
    });

    await test('refreshing a code kills the old one immediately', async () => {
      // The button exists for someone who read their code out on stream. A grace period would
      // keep the exact problem it was pressed to solve.
      const L = stub();
      const old = await L.friendCode(HOST);
      const fresh = await L.refreshFriendCode(HOST);
      assert.notEqual(fresh, old);
      assert.equal(await L._internals.ownerOfCode(old), '', 'the old code still works');
      assert.equal(await L._internals.ownerOfCode(fresh), HOST);
    });

    // ---------------------------------------------------------------- offline players have names
    //
    // Sam, 2026-09-16: the leaderboard and the friends list printed a bare 17-digit SteamID for
    // anybody who was not connected at that moment. Two causes, and only fixing both fixes it:
    // `personaOf` was DECLARED TWICE in one scope (the second, weaker one winning for every
    // caller), and even the strong one knew nothing about a player who was neither connected nor
    // in a match - which is what "offline" means.
    await test('there is exactly ONE personaOf and ONE avatarOf in live.cjs', async () => {
      // A SOURCE-LEVEL GUARD, deliberately. This bug was invisible for weeks precisely because a
      // duplicate declaration is legal JavaScript that throws nothing, logs nothing, and passes
      // every test written against connected players. The only cheap way to catch the next one is
      // to count the declarations.
      const src = await readFile_(path.join(__dirname, '..', 'live.cjs'), 'utf8');
      const count = (re) => (src.match(re) || []).length;
      assert.equal(count(/\n  function personaOf\(/g), 1,
                   'a second personaOf silently replaces the first for every caller');
      assert.equal(count(/\n  function avatarOf\(/g), 1, 'and the same for avatarOf');
    });

    await test('an offline player is a name, not a SteamID64', async () => {
      const L = stub();
      // Nobody is connected and nobody is in a match: this is exactly the state the bug was
      // visible in, and the old personaOf returned '' here.
      assert.equal(L._internals.personaOf(MATE), '');
      L._internals.rememberProfile(MATE, 'Luigi', 'https://avatars.example/luigi.jpg');
      assert.equal(L._internals.personaOf(MATE), 'Luigi');
      assert.equal(L._internals.avatarOf(MATE), 'https://avatars.example/luigi.jpg',
                   'the face is remembered too - a URL that has expired removes itself in the hub '
                   + 'and leaves the initials plate, so a stale one costs nothing (ui.js avatar())');
    });

    await test('a live client still wins over what we remember', async () => {
      const L = stub();
      const { clients, bySteam } = L._internals;
      L._internals.rememberProfile(MATE, 'Old Name', '');
      clients.set('c1', { res: { write: () => {} }, steamId: MATE, persona: 'New Name' });
      bySteam.set(MATE, new Set(['c1']));
      assert.equal(L._internals.personaOf(MATE), 'New Name');
    });

    await test('a blank never overwrites a name', async () => {
      // whoami answers persona:'' with no STEAM_WEB_API_KEY set, or while Steam is having a bad
      // minute - and one such sign-in must not erase a name we have been holding for weeks.
      const L = stub();
      L._internals.rememberProfile(MATE, 'Luigi', 'https://avatars.example/luigi.jpg');
      L._internals.rememberProfile(MATE, '', '');
      assert.equal(L._internals.personaOf(MATE), 'Luigi');
      assert.equal(L._internals.avatarOf(MATE), 'https://avatars.example/luigi.jpg');
    });

    await test('an offline friend keeps their name in the friends list', async () => {
      const L = stub();
      const code = await L.friendCode(MATE);
      await L.requestFriend(HOST, { code });
      await L.acceptFriend(MATE, { target: HOST });
      L._internals.rememberProfile(MATE, 'Luigi', '');

      const list = await L.friendList(HOST);
      assert.equal(list.friends.length, 1);
      assert.equal(list.friends[0].online, false, 'nobody is connected - the bug\'s own state');
      assert.equal(list.friends[0].persona, 'Luigi');
    });

    await test('a name survives a redeploy, because it is written through', async () => {
      const kv = new Map();
      const cmd = async ([op, key, val]) => {
        if (op === 'SET') { kv.set(key, val); return 'OK'; }
        if (op === 'GET') return kv.has(key) ? kv.get(key) : null;
        return null;
      };
      const opts = { whoami: async () => null, bearer: () => '', sendJson: () => {},
                     badRequest: () => {}, readBody: async () => Buffer.alloc(0),
                     upstashCmd: cmd, prefix: 'pf' };
      const before = liveModule.create(opts);
      before._internals.rememberProfile(MATE, 'Luigi', '');
      await new Promise((r) => setTimeout(r, 0));          // let the write land
      assert.ok([...kv.keys()].some((k) => k.includes('profile:')), 'it reached the store');

      const after = liveModule.create(opts);               // a fresh process, empty memory
      assert.equal(after._internals.personaOf(MATE), '', 'nothing is loaded until it is asked for');
      await after._internals.loadProfiles([MATE]);
      assert.equal(after._internals.personaOf(MATE), 'Luigi');
    });

    await test('Steam is asked for a player we have never met - once, and capped', async () => {
      const asked = [];
      const many = Array.from({ length: 30 },
                             (_, i) => `765611980000001${String(i).padStart(2, '0')}`);
      const L = liveModule.create({
        whoami: async () => null, bearer: () => '', sendJson: () => {}, badRequest: () => {},
        readBody: async () => Buffer.alloc(0), upstashCmd: async () => null, prefix: 'bf',
        profileOf: async (id) => { asked.push(id); return { persona: 'P' + id.slice(-2) }; },
      });
      await L._internals.loadProfiles(many);
      assert.equal(asked.length, 24, 'PROFILE_BACKFILL caps what one request can cost');
      assert.equal(L._internals.personaOf(many[0]), 'P00', 'and the answers are kept');

      const soFar = asked.length;
      await L._internals.loadProfiles(many);
      assert.equal(asked.length - soFar, 6,
                   'the 24 already answered are never asked again; only the 6 still nameless are');
      const second = asked.length;
      await L._internals.loadProfiles(many);
      assert.equal(asked.length, second, 'and once asked, never again this process');
    });

    await test('no profileOf is not an error - it is the board we have today', async () => {
      const L = stub();            // no profileOf injected, exactly like the unit harness
      await L._internals.loadProfiles([MATE, HOST]);
      assert.equal(L._internals.personaOf(MATE), '');
    });

    await test('request, accept, and both sides see each other', async () => {
      const L = stub();
      const code = await L.friendCode(MATE);
      const sent = await L.requestFriend(HOST, { code });
      assert.equal(sent.ok, true);
      assert.equal(sent.sent, true);

      let mine = await L.friendList(HOST);
      let theirs = await L.friendList(MATE);
      assert.equal(mine.outgoing.length, 1);
      assert.equal(theirs.incoming.length, 1);
      assert.equal(mine.friends.length, 0, 'asking is not being friends');

      assert.equal((await L.acceptFriend(MATE, { target: HOST })).ok, true);
      mine = await L.friendList(HOST);
      theirs = await L.friendList(MATE);
      // BOTH sides. A one-sided friendship is the bug that makes a list look haunted.
      assert.deepEqual(mine.friends.map((f) => f.steam_id), [MATE]);
      assert.deepEqual(theirs.friends.map((f) => f.steam_id), [HOST]);
      assert.equal(mine.outgoing.length, 0, 'the request is gone, not left pending');
      assert.equal(theirs.incoming.length, 0);
    });

    await test('two people adding each other at once become friends, not two requests', async () => {
      const L = stub();
      await L.requestFriend(HOST, { target: MATE });
      const crossed = await L.requestFriend(MATE, { target: HOST });
      assert.equal(crossed.ok, true);
      assert.equal(crossed.accepted, true, 'it should accept, not queue a mirror request');
      assert.deepEqual((await L.friendList(HOST)).friends.map((f) => f.steam_id), [MATE]);
    });

    await test('a friend code is long enough that collisions are not a dated bug', async () => {
      // Six characters of a 32-letter alphabet collides at ~33k codes (birthday problem). The
      // space has to be big enough that "every player who ever had one" never meets itself.
      const L = stub();
      const len = L._internals.FRIEND_CODE_LEN;
      assert.ok(len >= 10, `${len} characters is not enough for a permanent code`);
      const seen = new Set();
      for (let i = 0; i < 4000; i += 1) seen.add(L._internals.mintFriendCode());
      assert.equal(seen.size, 4000, 'the minter repeated itself within four thousand codes');
    });

    await test('you cannot friend yourself, or a code nobody owns', async () => {
      const L = stub();
      const mine = await L.friendCode(HOST);
      assert.equal((await L.requestFriend(HOST, { code: mine })).ok, false);
      assert.equal((await L.requestFriend(HOST, { code: 'ZZZZ-ZZ' })).ok, false);
      assert.equal((await L.requestFriend(HOST, { code: 'nonsense' })).ok, false);
    });

    await test('declining clears the request from both sides', async () => {
      const L = stub();
      await L.requestFriend(HOST, { target: MATE });
      assert.equal((await L.declineFriend(MATE, { target: HOST })).ok, true);
      assert.equal((await L.friendList(MATE)).incoming.length, 0);
      assert.equal((await L.friendList(HOST)).outgoing.length, 0,
                   'a declined request must not sit in the sender list for ever');
      assert.equal((await L.friendList(HOST)).friends.length, 0);
    });

    await test('cancelling withdraws your own request', async () => {
      const L = stub();
      await L.requestFriend(HOST, { target: MATE });
      assert.equal((await L.cancelFriendRequest(HOST, { target: MATE })).ok, true);
      assert.equal((await L.friendList(MATE)).incoming.length, 0);
      assert.equal((await L.friendList(HOST)).outgoing.length, 0);
    });

    await test('removing a friend removes them from both lists', async () => {
      const L = stub();
      await L.requestFriend(HOST, { target: MATE });
      await L.acceptFriend(MATE, { target: HOST });
      assert.equal((await L.removeFriend(HOST, { target: MATE })).ok, true);
      assert.equal((await L.friendList(HOST)).friends.length, 0);
      assert.equal((await L.friendList(MATE)).friends.length, 0,
                   'one-sided removal leaves them with a friend who cannot see them');
    });

    await test('accepting a request nobody sent does nothing', async () => {
      const L = stub();
      assert.equal((await L.acceptFriend(HOST, { target: MATE })).ok, false);
      assert.equal((await L.friendList(HOST)).friends.length, 0);
    });

    // ---------------------------------------------------------------- reports
    const reportMatch = (over = {}) => {
      const L = stub();
      const { matches, inMatch } = L._internals;
      const match = {
        id: 'rp1', state: 'live', host: HOST,
        players: [HOST, MATE, FOE].map((id) => ({ steam_id: id, persona: '', accepted: true,
                                                  connected: true })),
        teams: { 1: [HOST, MATE], 2: [FOE] },
        left: [], timer: null, deadline: 0, map: 'Rome', ...over,
      };
      matches.set(match.id, match);
      for (const p of match.players) inMatch.set(p.steam_id, match.id);
      return { L, match };
    };

    await test('a report is recorded against the person reported', async () => {
      const { L } = reportMatch();
      const r = await L.reportPlayer(HOST, { target: FOE, reason: 'cheating' });
      assert.equal(r.ok, true);
      assert.equal(r.recorded, true);
      const seen = await L.reportsFor(FOE);
      assert.equal(seen.total, 1);
      assert.equal(seen.reporters, 1);
      assert.deepEqual(seen.by_reason, { cheating: 1 });
    });

    await test('the reporter is told nothing about the pile', async () => {
      // A count handed back is a tool for deciding whether a pile-on is working.
      const { L } = reportMatch();
      await L.reportPlayer(HOST, { target: FOE, reason: 'griefing' });
      const r = await L.reportPlayer(MATE, { target: FOE, reason: 'griefing' });
      assert.equal(r.ok, true);
      assert.equal(r.total, undefined);
      assert.equal(r.reporters, undefined);
      assert.equal(r.reports, undefined);
    });

    await test('distinct reporters are what count, not repeat presses', async () => {
      const { L } = reportMatch();
      assert.equal((await L.reportPlayer(HOST, { target: FOE, reason: 'afk' })).recorded, true);
      const again = await L.reportPlayer(HOST, { target: FOE, reason: 'cheating' });
      assert.equal(again.ok, true, 'a double press is not an error');
      assert.equal(again.recorded, false);
      assert.equal(again.already, true);
      await L.reportPlayer(MATE, { target: FOE, reason: 'afk' });
      const seen = await L.reportsFor(FOE);
      assert.equal(seen.total, 2, 'one each, not three');
      assert.equal(seen.reporters, 2);
    });

    await test('you can only report someone you actually played with', async () => {
      // Without this it is an endpoint for burying a stranger under reports from accounts that
      // never played them.
      const { L } = reportMatch();
      const r = await L.reportPlayer(HOST, { target: STRANGER, reason: 'cheating' });
      assert.equal(r.ok, false);
      const seen = await L.reportsFor(STRANGER);
      assert.equal(seen.total, 0);
    });

    await test('you cannot report yourself, or for a reason nobody can check', async () => {
      const { L } = reportMatch();
      assert.equal((await L.reportPlayer(HOST, { target: HOST, reason: 'afk' })).ok, false);
      assert.equal((await L.reportPlayer(HOST, { target: FOE, reason: 'ugly' })).ok, false);
      assert.equal((await L.reportPlayer(HOST, { target: 'nope', reason: 'afk' })).ok, false);
    });

    await test('a finished match can still be reported from history', async () => {
      // The history screen is one of the places the button lives, and by then the live match is
      // gone - so the archive has to answer "were these two in a match together".
      const { L, match } = reportMatch();
      L._internals.archiveMatch(match, { outcome: 'played', reason: '' });
      L._internals.matches.delete(match.id);
      L._internals.inMatch.delete(HOST);
      const r = await L.reportPlayer(HOST, { target: FOE, reason: 'text_abuse',
                                             match_id: match.id });
      assert.equal(r.ok, true, 'the archive remembers who was there');
      assert.equal(r.recorded, true);
      // ...but not a match neither of them was in
      const bogus = await L.reportPlayer(HOST, { target: FOE, reason: 'afk', match_id: 'nope' });
      assert.equal(bogus.ok, false);
    });

    await test('the same person in two matches is two pieces of evidence', async () => {
      const { L, match } = reportMatch();
      await L.reportPlayer(HOST, { target: FOE, reason: 'griefing' });
      L._internals.archiveMatch(match, { outcome: 'played', reason: '' });
      // a second match with the same two players
      const { matches, inMatch } = L._internals;
      const second = { ...match, id: 'rp2', archived: false,
                       players: match.players.map((p) => ({ ...p })) };
      matches.set(second.id, second);
      for (const p of second.players) inMatch.set(p.steam_id, second.id);
      const r = await L.reportPlayer(HOST, { target: FOE, reason: 'griefing' });
      assert.equal(r.recorded, true, 'one per match, not one per lifetime');
      assert.equal((await L.reportsFor(FOE)).total, 2);
    });

    // ---------------------------------------------------------------- bug reports
    //
    // Sam, 2026-09-16: a Bug report screen in the hub, reaching the admin console with the
    // reporter's steam id and name, and one report every five seconds. The hub guards the five
    // seconds too (hub/competitive.py send_bug_report) - these are about the guard that holds when
    // the hub is not the thing on the other end of the socket.

    // A signed-in hub, as personaOf sees one: an open /api/live stream. That is how the server
    // learns a player's name, and a hub filing a bug always has one.
    const connectedAs = (L, steamId, persona) => {
      const clientId = `c-${steamId}`;
      L._internals.clients.set(clientId, { steamId, persona });
      L._internals.bySteam.set(steamId, new Set([clientId]));
    };

    await test('a bug report is recorded with who filed it', async () => {
      const L = stub();
      connectedAs(L, HOST, 'Sam');
      const r = await L.submitBugReport(HOST, { text: '  the queue button does nothing  ' });
      assert.equal(r.ok, true);
      assert.equal(r.recorded, true);
      const [row] = await L.bugReports(10);
      assert.equal(row.by, HOST, 'the steam id is on the record');
      assert.equal(row.persona, 'Sam', '...and so is the name');
      assert.equal(row.text, 'the queue button does nothing', 'trimmed, not as typed');
      assert.ok(row.at > 0);
    });

    await test('a bug report carries what they were running', async () => {
      // The first question anyone reading one asks, and it costs nothing: every authenticated
      // request already stamps its versions (noteVersions).
      const L = stub();
      L._internals.versions.set(HOST, { hub: '2.0.21', mode: '1.4.0', at: Date.now() });
      await L.submitBugReport(HOST, { text: 'it crashed' });
      const [row] = await L.bugReports(10);
      assert.equal(row.hub, '2.0.21');
      assert.equal(row.mode, '1.4.0');
    });

    await test('one bug report every five seconds, per account', async () => {
      const L = stub();
      assert.equal((await L.submitBugReport(HOST, { text: 'first' })).ok, true);
      const again = await L.submitBugReport(HOST, { text: 'second' });
      assert.equal(again.ok, false);
      assert.equal(again.error, 'too_fast');
      assert.ok(again.retry_after_ms > 0 && again.retry_after_ms <= 5000, again.retry_after_ms);
      assert.equal((await L.bugReports(10)).length, 1, 'the second one was not stored');
      // Per ACCOUNT: one person on a cooldown must not stop everybody else filing.
      assert.equal((await L.submitBugReport(MATE, { text: 'mine' })).ok, true);
      assert.equal((await L.bugReports(10)).length, 2);
    });

    await test('the cooldown lets go once it has run', async () => {
      const L = stub();
      await L.submitBugReport(HOST, { text: 'first' });
      // Wind their stamp back rather than sleeping five seconds in a test suite.
      L._internals.bugCooldown.set(HOST, Date.now() - (liveModule.BUG_COOLDOWN_MS + 10));
      assert.equal((await L.submitBugReport(HOST, { text: 'second' })).ok, true);
      assert.equal((await L.bugReports(10)).length, 2);
    });

    await test('an empty report is refused, and a long one is truncated rather than lost', async () => {
      const L = stub();
      assert.equal((await L.submitBugReport(HOST, { text: '   \n  ' })).ok, false);
      assert.equal((await L.submitBugReport(HOST, {})).error, 'empty');
      assert.equal((await L.submitBugReport('nope', { text: 'hi' })).ok, false);

      const L2 = stub();
      const huge = 'x'.repeat(liveModule.BUG_TEXT_MAX + 500);
      assert.equal((await L2.submitBugReport(HOST, { text: huge })).ok, true,
                   'somebody who pasted a stack trace must not lose all of it');
      assert.equal((await L2.bugReports(10))[0].text.length, liveModule.BUG_TEXT_MAX);
    });

    await test('bug reports reach the admin console, newest first', async () => {
      const L = stub();
      await L.submitBugReport(HOST, { text: 'the older one' });
      L._internals.bugCooldown.clear();
      await L.submitBugReport(MATE, { text: 'the newer one' });
      L._internals.ADMIN_IDS.add(HOST);
      try {
        const view = await L.adminOverview(HOST, {});
        assert.equal(view.ok, true);
        assert.equal(view.bugs.length, 2);
        assert.equal(view.bugs[0].text, 'the newer one', 'newest first');
        assert.equal(view.bugs[0].by, MATE);
      } finally {
        L._internals.ADMIN_IDS.delete(HOST);
      }
    });

    await test('/api/bug is a route this module owns', () => {
      // A fully-implemented feature can be dead on arrival if server.cjs is never told to hand the
      // path over - which is exactly what happened to /api/friends and /api/report once.
      assert.equal(liveModule.owns('/api/bug'), true);
    });

    await test('the admin console page shows the bug, the name and the steam id', async () => {
      // The END of what Sam asked for: "that bug will then get sent to the admin console with
      // their steam id and name". Everything above proves the record exists; this proves it is on
      // the page a moderator actually opens.
      const adminModule = require_('../admin.cjs');
      const L = stub();
      connectedAs(L, HOST, 'Sam');
      await L.submitBugReport(HOST, { text: 'the hub reopens Bodycam on every reconnect' });

      const TOKEN = 'a'.repeat(64);
      const sessions = { [`admin:adminsession:${TOKEN}`]: JSON.stringify({ steam_id: HOST }) };
      const admin = adminModule.create({
        prefix: 'admin:',
        upstashCmd: async ([verb, key]) => (verb === 'GET' ? (sessions[key] || null) : null),
        live: () => L,
        verifyWithSteam: async () => true,
      });

      let body = '';
      const res = { writeHead() {}, end(b) { body = String(b); } };
      const req = { headers: { host: 'hub.test', cookie: `hubadmin=${TOKEN}` } };
      L._internals.ADMIN_IDS.add(HOST);
      try {
        const handled = await admin.route(req, res, 'GET', '/admin',
                                          new URL('https://hub.test/admin'));
        assert.equal(handled, true);
      } finally {
        L._internals.ADMIN_IDS.delete(HOST);
      }
      assert.ok(body.includes('Bug reports'), 'the console has no bug reports card');
      assert.ok(body.includes('the hub reopens Bodycam on every reconnect'), 'the report is not on the page');
      assert.ok(body.includes('Sam'), 'the reporter\'s name is not on the page');
      assert.ok(body.includes(HOST), 'the reporter\'s steam id is not on the page');
    });

    await test('a bug report cannot inject markup into the console', async () => {
      // It is free text a stranger typed, rendered into a page a moderator opens while signed in.
      const adminModule = require_('../admin.cjs');
      const L = stub();
      await L.submitBugReport(HOST, { text: '<script>alert(1)</script>' });

      const TOKEN = 'b'.repeat(64);
      const sessions = { [`admin2:adminsession:${TOKEN}`]: JSON.stringify({ steam_id: HOST }) };
      const admin = adminModule.create({
        prefix: 'admin2:',
        upstashCmd: async ([verb, key]) => (verb === 'GET' ? (sessions[key] || null) : null),
        live: () => L,
        verifyWithSteam: async () => true,
      });
      let body = '';
      const res = { writeHead() {}, end(b) { body = String(b); } };
      const req = { headers: { host: 'hub.test', cookie: `hubadmin=${TOKEN}` } };
      L._internals.ADMIN_IDS.add(HOST);
      try {
        await admin.route(req, res, 'GET', '/admin', new URL('https://hub.test/admin'));
      } finally {
        L._internals.ADMIN_IDS.delete(HOST);
      }
      assert.ok(!body.includes('<script>alert(1)</script>'), 'the report was rendered as markup');
      assert.ok(body.includes('&lt;script&gt;'), 'the report was not rendered at all');
    });

    // ---------------------------------------------------------------- the player directory
    //
    // Sam, 2026-09-16: "a player search either through steamid or steam name ... each player
    // thats ever logged on being a row", and then "certain buttons that we can press for each
    // user such as ban/unban/reset rank/change elo/change rank".
    //
    // The line these all sit on: the directory MIRRORS the ladder and never becomes a second
    // source of truth for it. Every test below that touches wins, rank or MMR is really asking
    // whether the mirror still agrees with the record it was copied from.
    const admins = (L, fn) => {
      L._internals.ADMIN_IDS.add(HOST);
      return Promise.resolve(fn()).finally(() => L._internals.ADMIN_IDS.delete(HOST));
    };

    await test('signing in puts an account in the directory, named', async () => {
      const L = stub();
      L._internals.noteSeen(MATE, 'Logan');
      L._internals.noteSeen(MATE, 'Logan');          // a second session, same person
      await admins(L, async () => {
        const view = await L.adminPlayers(HOST, {});
        assert.equal(view.ok, true);
        const row = view.rows.find((r) => r.steam_id === MATE);
        assert.ok(row, 'somebody who signed in is not in the directory');
        assert.equal(row.persona, 'Logan');
        assert.equal(row.sessions, 2, 'both sign-ins should be counted');
        assert.ok(row.first_seen > 0 && row.last_seen >= row.first_seen);
      });
    });

    await test('a blank persona never erases the name we already had', async () => {
      // The rule rememberPersona has, for the reason it has it: a hub running without
      // STEAM_WEB_API_KEY signs in with an empty persona, and letting that land would turn every
      // row in the console back into a 17-digit number.
      const L = stub();
      L._internals.noteSeen(MATE, 'Logan');
      L._internals.noteSeen(MATE, '');
      assert.equal(L._internals.careerOf(MATE).persona, 'Logan');
    });

    await test('the search takes a steam name or any part of the id', async () => {
      const L = stub();
      L._internals.noteSeen(HOST, 'Sam');
      L._internals.noteSeen(MATE, 'Logan');
      L._internals.noteSeen(FOE, 'Vex');
      await admins(L, async () => {
        const byName = await L.adminPlayers(HOST, { q: 'log' });
        assert.deepEqual(byName.rows.map((r) => r.steam_id), [MATE], 'a name should match');
        assert.equal(byName.total, 3, 'the total is everybody, not the matches');
        assert.equal(byName.found, 1);

        const upper = await L.adminPlayers(HOST, { q: 'LOGAN' });
        assert.equal(upper.found, 1, 'the search must not care about case');

        // The thing a moderator has in front of them is as often the last four digits off a
        // screenshot as it is the whole number.
        const byTail = await L.adminPlayers(HOST, { q: MATE.slice(-4) });
        assert.deepEqual(byTail.rows.map((r) => r.steam_id), [MATE]);

        const none = await L.adminPlayers(HOST, { q: 'nobody' });
        assert.equal(none.found, 0);
        assert.equal(none.rows.length, 0);
      });
    });

    await test('the directory is refused to anybody who is not an admin', async () => {
      const L = stub();
      assert.equal((await L.adminPlayers(MATE, {})).ok, false);
      assert.equal((await L.adminPlayers('', {})).ok, false);
    });

    await test('wins and rank come off the rating record, not a second counter', async () => {
      const L = stub();
      L._internals.noteSeen(MATE, 'Logan');
      L._internals.saveRating(MATE, { rating: 1200, matches: 20, wins: 13, losses: 7,
                                      progress: 640 });
      await admins(L, async () => {
        const [row] = (await L.adminPlayers(HOST, { q: MATE })).rows;
        assert.equal(row.matches, 20);
        assert.equal(row.wins, 13);
        assert.equal(row.losses, 7);
        assert.equal(row.win_rate, 65);
        assert.equal(row.mmr, 1200);
        assert.equal(row.placing, false);
        // Named by the same call the hub draws its badge from, not by a string in this test:
        // the ladder is an environment dial, and a hard-coded name here would be a second copy
        // of it that goes wrong the day one is turned.
        assert.equal(row.rank_name,
                     L._internals.progress.publicProgress({ progress: 640, matches: 20 }).rank_name,
                     'the console must name the rank the same way the hub does');
      });
    });

    await test('a match adds its kills, rounds and minutes - and NOT its wins', async () => {
      // The kills come from the FEED, where a kill is a named event. The stat row's `k` is a net
      // score a team kill decrements (measured 2026-09-15), and counting that as kills is the
      // mistake this is written to catch.
      const L = stub();
      const match = {
        id: 'd1', state: 'live', host: HOST, map: 'Rome',
        created: Date.now() - 900000, live_at: Date.now() - 600000,
        players: [HOST, MATE, FOE].map((id) => ({ steam_id: id, persona: '', accepted: true,
                                                  connected: true })),
        teams: { 1: [HOST, MATE], 2: [FOE] }, left: [],
        kills: [
          { killer: HOST, victim: FOE, teamKill: false },
          { killer: HOST, victim: FOE, teamKill: false },
          { killer: HOST, victim: MATE, teamKill: true },
          { killer: FOE, victim: HOST, teamKill: false },
        ],
        stats: { rounds: 6, players: [{ steamId: HOST, kills: 1, deaths: 1, clutches: 2 }] },
      };
      L._internals.creditMatch(match, { played: true });
      const me = L._internals.careerOf(HOST);
      assert.equal(me.kills, 2, 'the team kill is not a kill');
      assert.equal(me.team_kills, 1);
      assert.equal(me.deaths, 1);
      assert.equal(me.clutches, 2, 'clutches still come off the stat row');
      assert.equal(me.rounds, 6);
      assert.equal(me.played, 1);
      assert.ok(me.seconds >= 600 && me.seconds < 660, `ten minutes, got ${me.seconds}`);
      assert.equal(me.wins, 0, 'the rating record owns wins; the directory must not invent them');
      assert.equal(L._internals.careerOf(MATE).deaths, 1);

      // Twice is once. finishMatch and closeMatch can both reach a match on a bad day.
      L._internals.creditMatch(match, { played: true });
      assert.equal(L._internals.careerOf(HOST).kills, 2, 'a match must not be counted twice');
    });

    await test('a cancelled match counts the abandon and none of the time', async () => {
      const L = stub();
      const match = {
        id: 'd2', state: 'ready', host: HOST, map: 'Rome', created: Date.now() - 300000,
        players: [{ steam_id: HOST, persona: '', accepted: false }],
        teams: { 1: [HOST], 2: [] }, left: [],
      };
      L._internals.creditMatch(match, { played: false, reason: 'no_show', blame: [HOST] });
      const me = L._internals.careerOf(HOST);
      assert.equal(me.abandons, 1);
      assert.equal(me.no_shows, 1);
      assert.equal(me.played, 0, 'a match nobody played is not a match played');
      assert.equal(me.seconds, 0, 'waiting is not time played');
    });

    await test('reset rank puts an account back on placements', async () => {
      const L = stub();
      L._internals.saveRating(MATE, { rating: 1800, matches: 40, wins: 25, losses: 15,
                                      progress: 1500 });
      await admins(L, async () => {
        assert.equal((await L.resetRank(MATE, { steam_id: FOE })).ok, false, 'not an admin');
        const out = await L.resetRank(HOST, { steam_id: MATE });
        assert.equal(out.ok, true);
        const [row] = (await L.adminPlayers(HOST, { q: MATE })).rows;
        assert.equal(row.placing, true);
        assert.equal(row.progress, 0);
        assert.equal(row.matches, 0);
        assert.equal(row.rank_name, '');
      });
    });

    await test('changing the MMR does not move the rank, and the other way round', async () => {
      // The two ladders are separate on purpose (docs/ranks.md): MMR is what the matchmaker
      // reads, RR is what the player sees. A button that quietly moved both would make every
      // correction a matchmaking change nobody asked for.
      const L = stub();
      L._internals.saveRating(MATE, { rating: 1500, matches: 30, wins: 15, losses: 15,
                                      progress: 900 });
      await admins(L, async () => {
        assert.equal((await L.setElo(HOST, { steam_id: MATE, rating: 1750 })).ok, true);
        let [row] = (await L.adminPlayers(HOST, { q: MATE })).rows;
        assert.equal(row.mmr, 1750);
        assert.equal(row.progress, 900, 'the visible ladder must not have moved');

        assert.equal((await L.setRank(HOST, { steam_id: MATE, rank: 3, division: 2 })).ok, true);
        [row] = (await L.adminPlayers(HOST, { q: MATE })).rows;
        assert.equal(row.mmr, 1750, 'the hidden ladder must not have moved');
        const asked = L._internals.progress.publicProgress({ progress: row.progress, matches: 30 });
        assert.equal(row.rank_name, asked.rank_name);
        assert.equal(row.division, 2, 'the division asked for is the division set');
      });
    });

    await test('a hand-set rank lifts a placing account out of placements', async () => {
      // Otherwise the button writes a number nothing draws: RR is not shown at all until the
      // placement matches are done, so the admin presses it, sees nothing, and presses it again.
      const L = stub();
      L._internals.saveRating(MATE, { rating: 1500, matches: 1, wins: 1, losses: 0 });
      await admins(L, async () => {
        assert.equal((await L.setRank(HOST, { steam_id: MATE, rank: 4, division: 1 })).ok, true);
        const [row] = (await L.adminPlayers(HOST, { q: MATE })).rows;
        assert.equal(row.placing, false, 'a rank nobody can see is not a rank');
        assert.equal(row.rank_name,
                     L._internals.progress.publicProgress({ progress: row.progress,
                                                            matches: 99 }).rank_name);
      });
    });

    await test('the rank buttons refuse what they cannot honestly do', async () => {
      const L = stub();
      await admins(L, async () => {
        assert.equal((await L.setRank(HOST, { steam_id: MATE, rank: 99 })).ok, false,
                     'the capstone is a seat on the board, not a rank RR can reach');
        assert.equal((await L.setRank(HOST, { steam_id: MATE, rank: 0 })).ok, false);
        assert.equal((await L.setRank(HOST, { steam_id: MATE, rank: 2, division: 9 })).ok, false);
        assert.equal((await L.setRank(HOST, { steam_id: 'nope', rank: 2 })).ok, false);
        assert.equal((await L.setElo(HOST, { steam_id: MATE, rating: 'lots' })).ok, false);
        // A typed extra zero is a typo, not an instruction.
        const huge = await L.setElo(HOST, { steam_id: MATE, rating: 99999 });
        assert.equal(huge.rating, liveModule.ELO_CEILING);
      });
      assert.equal((await L.setElo(MATE, { steam_id: FOE, rating: 1200 })).ok, false,
                   "a normal player cannot set another player's rating");
    });

    await test('an RR total can be set outright', async () => {
      const L = stub();
      await admins(L, async () => {
        const out = await L.setRank(HOST, { steam_id: MATE, progress: 1450 });
        assert.equal(out.ok, true);
        assert.equal(out.progress, 1450);
      });
    });

    await test('the actions column is not a field, and cannot be sorted on', async () => {
      const L = stub();
      L._internals.noteSeen(HOST, 'Sam');
      await admins(L, async () => {
        const view = await L.adminPlayers(HOST, { sort: 'actions' });
        assert.equal(view.sort, 'last_seen', 'sorting on a column with no value is not sorting');
      });
    });

    await test('every column the console offers is one the directory fills in', () => {
      // The console renders this table and validates saved presets against it. A key in the table
      // that no row carries draws an empty column rather than saying so, which is the drift this
      // catches on the day somebody adds a column and forgets the row.
      const L = stub();
      L._internals.noteSeen(HOST, 'Sam');
      const row = L._internals.directoryRow(HOST);
      for (const column of liveModule.PLAYER_COLUMNS) {
        if (column.key === 'actions') continue;        // buttons, not a field
        assert.ok(Object.prototype.hasOwnProperty.call(row, column.key),
                  `the console offers a ${column.key} column that no row has`);
      }
    });

    await test('a saved column set is checked against the real columns', () => {
      const adminModule = require_('../admin.cjs');
      const clean = adminModule.cleanPrefs({
        presets: [{ id: 'mine', name: 'Mine', columns: ['persona', 'not_a_column', 'kills'],
                    sort: 'kills', dir: 'asc' },
                  { id: 'empty', name: 'Empty', columns: ['also_not_one'] },
                  { id: '', name: 'No id', columns: ['kills'] }],
        active: 'empty',
      });
      assert.deepEqual(clean.presets.map((p) => p.id), ['mine'],
                       'a preset with no usable column would draw a blank page');
      assert.deepEqual(clean.presets[0].columns, ['persona', 'kills', 'cheater_score']);
      assert.equal(clean.cheater_score_column_v1,true);
      const chosen=adminModule.cleanPrefs({...clean,presets:[{...clean.presets[0],columns:['persona']}]});
      assert.deepEqual(chosen.presets[0].columns,['persona'],'after migration a moderator may hide the column');
      assert.equal(clean.active, 'mine', 'the active set must be one that exists');

      const none = adminModule.cleanPrefs(null);
      assert.ok(none.presets.length >= 1, 'a moderator with nothing saved still gets columns');
      assert.equal(none.active, none.presets[0].id);
      for (const preset of none.presets) {
        for (const key of preset.columns) {
          assert.ok(liveModule.PLAYER_COLUMNS.some((c) => c.key === key),
                    `the default set asks for a ${key} column that does not exist`);
        }
      }
    });

    await test('the directory page is behind the same sign-in as the rest of the console', async () => {
      const adminModule = require_('../admin.cjs');
      const L = stub();
      const admin = adminModule.create({
        prefix: 'admin3:',
        upstashCmd: async () => null,              // no session record: nobody is signed in
        live: () => L,
        verifyWithSteam: async () => true,
      });
      let body = '';
      const res = { writeHead() {}, end(b) { body = String(b); } };
      const req = { headers: { host: 'hub.test' } };
      const handled = await admin.route(req, res, 'GET', '/admin/players',
                                        new URL('https://hub.test/admin/players'));
      assert.equal(handled, true);
      assert.ok(body.includes('Sign in with Steam'), 'a stranger was shown the directory');
      assert.ok(!body.includes('Search by Steam name'), 'a stranger was shown the search bar');
    });

    await test('the directory page carries the columns and the ladder it needs', async () => {
      const adminModule = require_('../admin.cjs');
      const L = stub();
      L._internals.noteSeen(HOST, 'Sam');
      const TOKEN = 'd'.repeat(64);
      const sessions = { [`admin4:adminsession:${TOKEN}`]: JSON.stringify({ steam_id: HOST }) };
      const admin = adminModule.create({
        prefix: 'admin4:',
        upstashCmd: async ([verb, key]) => (verb === 'GET' ? (sessions[key] || null) : null),
        live: () => L,
        verifyWithSteam: async () => true,
      });
      let body = '';
      let status = 0;
      const res = { writeHead(s) { status = s; }, end(b) { body = String(b); } };
      const req = { headers: { host: 'hub.test', cookie: `hubadmin=${TOKEN}` } };
      L._internals.ADMIN_IDS.add(HOST);
      try {
        await admin.route(req, res, 'GET', '/admin/players',
                          new URL('https://hub.test/admin/players'));
      } finally {
        L._internals.ADMIN_IDS.delete(HOST);
      }
      assert.equal(status, 200);
      assert.match(body, /<input[^>]*id="q"[^>]*type="search"/, 'no search bar');
      const boot = JSON.parse(body.split('id="boot">')[1].split('</script>')[0]
        .replace(/\\u003c/g, '<'));
      assert.equal(boot.columns.length, liveModule.PLAYER_COLUMNS.length,
                   'the page and the service must agree about the columns');
      assert.ok(boot.ladder.names.length >= 1, 'the rank prompt cannot name a rank');
      assert.ok(boot.prefs.presets.length >= 1);
      // The script the browser gets has to be a script the browser can parse. It is written
      // inside a template literal, where one unescaped backslash turns a string into a syntax
      // error, and nothing else in this suite would notice.
      const script = body.split('<script>')[1].split('</script>')[0];
      new Function(script);            // eslint-disable-line no-new-func
    });

    // ---------------------------------------------------------------- team kills
    //
    // Friendly fire is ON in Bodybomb, so a team kill is not an offence by itself. Everything here
    // is about separating the accident from the intent, and about never punishing the accident.
    const tkMatch = (over = {}) => {
      const L = stub();
      const { matches, inMatch } = L._internals;
      const match = {
        id: 'k1', state: 'live', host: HOST,
        players: [HOST, MATE, FOE].map((id) => ({ steam_id: id, persona: '', accepted: true,
                                                  connected: true })),
        teams: { 1: [HOST, MATE], 2: [FOE] },
        left: [], timer: null, deadline: 0, map: 'Rome', ...over,
      };
      matches.set(match.id, match);
      for (const p of match.players) inMatch.set(p.steam_id, match.id);
      return { L, match };
    };
    // a plain mid-round team kill with enemies still alive: an accident, as far as we can tell
    const tk = (L, over = {}) => L.teamKillReported(HOST, {
      killer: HOST, victim: MATE, team: 0, elapsed: 45, round: 2, alive0: 3, alive1: 4, ...over });

    await test('an ordinary mid-round team kill is not an offence', () => {
      const { L, match } = tkMatch();
      const r = tk(L);
      assert.equal(r.ok, true);
      assert.deepEqual(r.flags, [], 'friendly fire is on; this is Tuesday');
      assert.equal(r.malicious, false);
      assert.equal(match.teamkills.length, 1, 'recorded anyway - it is evidence, not an accusation');
    });

    await test('an early legacy kill is diagnostic and cannot establish intent', () => {
      // Nobody has engaged an enemy yet, so there is no crossfire to have been caught in.
      const { L } = tkMatch();
      const r = tk(L, { elapsed: 1 });
      assert.ok(r.flags.includes('early'));
      assert.equal(r.malicious, false);
    });

    await test('...but not during warm-up, where there is no round to start', () => {
      const { L } = tkMatch();
      const r = tk(L, { elapsed: 1, round: 0 });
      assert.equal(r.flags.includes('early'), false, 'round 0 is warm-up, not a round start');
    });

    await test('an empty-enemy count is diagnostic and cannot establish intent', () => {
      // There was nothing else in the world to be shooting at.
      const { L } = tkMatch();
      const r = tk(L, { team: 0, alive0: 2, alive1: 0 });
      assert.ok(r.flags.includes('no_enemy'));
      // ...and the enemy is whichever team the killer is NOT on, so the mirror case must not fire
      const other = tkMatch();
      const r2 = other.L.teamKillReported(HOST, { killer: HOST, victim: MATE, team: 1,
                                                  elapsed: 45, round: 2, alive0: 0, alive1: 2 });
      assert.ok(r2.flags.includes('no_enemy'));
      const neither = tkMatch();
      const r3 = neither.L.teamKillReported(HOST, { killer: HOST, victim: MATE, team: 1,
                                                    elapsed: 45, round: 2, alive0: 2, alive1: 0 });
      assert.equal(r3.flags.includes('no_enemy'), false, 'his own team being dead proves nothing');
    });

    await test('the same teammate twice is malicious; two different ones is not', () => {
      const { L } = tkMatch();
      assert.equal(tk(L).flags.includes('repeat'), false);
      assert.ok(tk(L).flags.includes('repeat'), 'crossfire does not pick favourites');

      const fresh = tkMatch();
      fresh.L.teamKillReported(HOST, { killer: HOST, victim: MATE, team: 0, elapsed: 45,
                                       round: 2, alive0: 3, alive1: 4 });
      const r = fresh.L.teamKillReported(HOST, { killer: HOST, victim: FOE, team: 0, elapsed: 45,
                                                 round: 2, alive0: 3, alive1: 4 });
      assert.equal(r.flags.includes('repeat'), false);
    });

    await test('three in a match is a pattern', () => {
      const { L } = tkMatch();
      assert.equal(tk(L, { victim: MATE }).flags.includes('volume'), false);
      assert.equal(tk(L, { victim: FOE }).flags.includes('volume'), false, 'two is a bad night');
      const third = L.teamKillReported(HOST, { killer: HOST, victim: MATE, team: 0, elapsed: 45,
                                               round: 2, alive0: 3, alive1: 4 });
      assert.ok(third.flags.includes('volume'));
    });

    await test('a suicide is never a team kill', () => {
      // Fall damage and your own grenade both arrive with instigator == victim. The pak refuses
      // to send one; this refuses again, because a ban must not rest on one guard.
      const { L, match } = tkMatch();
      const r = L.teamKillReported(HOST, { killer: HOST, victim: HOST, team: 0, elapsed: 1,
                                           round: 2, alive0: 3, alive1: 4 });
      assert.equal(r.ok, false);
      assert.equal(match.teamkills, undefined, 'and nothing is recorded against them');
    });

    await test('nobody is punished while enforcement is off', () => {
      // It compares TeamID, and the pak now WRITES TeamID itself. If the sweep is wrong, every
      // enemy kill looks like a team kill - so this stays off until a real match proves it.
      const { L } = tkMatch();
      const r = tk(L, { elapsed: 1 });
      assert.equal(r.malicious, false);
      assert.equal(r.enforced, false, 'a verdict is not a punishment');
      assert.equal(r.penalty, null);
      assert.equal(L._internals.penalties.size, 0, 'no ban was written');
    });

    await test('legacy kill evidence cannot charge RR even with enforcement on', () => {
      const { L } = tkMatch();
      process.env.COMP_TK_ENFORCE = '1';
      try {
        const r = tk(L, { elapsed: 1 });
        assert.equal(r.enforced, false);
        assert.equal(r.penalty, null);
        assert.equal(L._internals.penalties.size, 0);
      } finally { delete process.env.COMP_TK_ENFORCE; }
    });

    await test('an ACCIDENT is never punished, even with enforcement on', () => {
      const { L } = tkMatch();
      process.env.COMP_TK_ENFORCE = '1';
      try {
        const r = tk(L);                       // mid-round, enemies alive, first offence
        assert.equal(r.malicious, false);
        assert.equal(r.enforced, false);
        assert.equal(L._internals.penalties.size, 0);
      } finally {
        delete process.env.COMP_TK_ENFORCE;
      }
    });

    await test('only the host may report, and only about the roster', () => {
      const { L } = tkMatch();
      assert.equal(L.teamKillReported(MATE, { killer: HOST, victim: FOE, team: 0 }).ok, false);
      assert.equal(L.teamKillReported(HOST, { killer: STRANGER, victim: MATE, team: 0 }).ok, false);
      assert.equal(L.teamKillReported(HOST, { killer: HOST, victim: STRANGER, team: 0 }).ok, false);
      assert.equal(L.teamKillReported('nonsense', { killer: HOST, victim: MATE }).ok, false);
    });

    await test('the verdicts are archived with the match', () => {
      const { L, match } = tkMatch();
      tk(L, { elapsed: 1 });
      const rec = L._internals.archiveMatch(match, { outcome: 'played', reason: '' });
      assert.equal(rec.teamkills.length, 1);
      assert.ok(rec.teamkills[0].flags.includes('early'),
                'the admin console reads this, and so does the decision to enforce');
    });

    // ---------------------------------------------------------------- the round timeline
    //
    // A finished match used to remember "7-3" and nothing about how it got there, which is the
    // one thing a match-detail screen cannot reconstruct afterwards.
    const liveMatch = (over = {}) => {
      const L = stub();
      const { matches, inMatch } = L._internals;
      const match = {
        id: 'r1', state: 'live', host: HOST,
        players: [HOST, MATE, FOE].map((id) => ({ steam_id: id, persona: '', accepted: true,
                                                  connected: true })),
        teams: { 1: [HOST, MATE], 2: [FOE] },
        left: [], timer: null, deadline: 0, map: 'Rome', ...over,
      };
      matches.set(match.id, match);
      for (const p of match.players) inMatch.set(p.steam_id, match.id);
      return { L, match };
    };
    // the host is on our side 1 and is in-game team 0
    const report = (L, a, b) => L.gameReportedScore(HOST, { scores: `0|0:${a}|1:${b}`, limit: '7' });

    await test('every score step is recorded as a round, with who took it', () => {
      const { L, match } = liveMatch();
      report(L, 1, 0);
      report(L, 1, 1);
      report(L, 2, 1);
      assert.equal(match.rounds.length, 3);
      assert.deepEqual(match.rounds.map((r) => r.won), [1, 2, 1]);
      assert.equal(match.rounds[2][1], 2, 'the running score rides along');
      assert.equal(match.rounds[2][2], 1);
    });

    // THE PAK STAMPS A PLACEHOLDER, and it used to refuse every report it sent. bb5_graphs.py
    // LOBBY_VALUE is the hardcoded 'ch-test-4821', written onto the lobby as CH_MATCH and echoed
    // back on every report as `fields.match`. It is never a real 16-hex match id, so the late-report
    // guard rejected 100% of reports and no match could settle. Measured 2026-09-16: every
    // ch_bb5_score in the probe log carries sf=ch-test-4821.
    await test("the pak's placeholder match id does not refuse the report", () => {
      const { L, match } = liveMatch();
      const r = L.gameReportedScore(HOST, { scores: '0|0:1|1:0', limit: '7', match: 'ch-test-4821' });
      assert.equal(r.ok, true, 'the placeholder means "no id", exactly like an absent one');
      assert.equal(match.rounds.length, 1, 'and the round is actually recorded');
    });

    await test('a REAL match id from another match is still refused', () => {
      const { L, match } = liveMatch();
      const r = L.gameReportedScore(HOST, { scores: '0|0:1|1:0', limit: '7',
                                            match: 'deadbeefdeadbeef' });
      assert.equal(r.ok, false, 'the late-report guard still does its job');
      assert.match(String(r.error), /wrong match id/);
      // `rounds` is created lazily by the first ACCEPTED report, so a refused one leaves it absent.
      assert.ok(!match.rounds || match.rounds.length === 0, 'and nothing was applied');
    });

    await test('the match\'s OWN id is accepted', () => {
      const { L, match } = liveMatch();
      const r = L.gameReportedScore(HOST, { scores: '0|0:1|1:0', limit: '7', match: match.id });
      assert.equal(r.ok, true);
      assert.equal(match.rounds.length, 1);
    });

    // THE HEARTBEAT'S COPY OF THE SCORELINE (2026-09-16). ch_bb5_score sits behind a GetTeams()
    // branch in the pak and that branch was false for Sam's whole 1v1 that evening - zero score
    // reports in a match that produced 270 stats rows - so the match never settled and the hub
    // never closed a game. ch_bb5_state has no branch in front of it and now carries the same
    // numbers in `first_session_timestamp`. These tests pin the exact string GM_BB5 builds.
    await test('the heartbeat scoreline parses into what gameReportedScore takes', () => {
      assert.deepEqual(liveModule.parseHeartbeatScore('0|0:2|1:1;lim=2'),
                       { scores: '0|0:2|1:1', limit: '2' });
      // Warm-up: TeamID is -1 until the game assigns one, and the limit can be any int.
      assert.deepEqual(liveModule.parseHeartbeatScore('-1|0:0|1:0;lim=7'),
                       { scores: '-1|0:0|1:0', limit: '7' });
    });

    await test('anything that is not a scoreline is not a score report', () => {
      // Every build before BB5 1.0.13 sends '' here, and the Platform payload rides in a different
      // field. A null answer is what keeps those heartbeats from reading as a 0-0 scoreline, which
      // the stale-report guard would then use to refuse the real ones.
      for (const junk of ['', null, undefined, 'n=3;w=-1;sec=5;a0=1;a1=0', '0|0:2|1:1',
                          '0|0:2|1:1;lim=', 'x|0:2|1:1;lim=2']) {
        assert.equal(liveModule.parseHeartbeatScore(junk), null, `${junk} is not a scoreline`);
      }
    });

    await test('a match settles from heartbeat scorelines alone', () => {
      // The end-to-end claim: with ch_bb5_score never arriving, the heartbeat is enough on its own.
      const { L, match } = liveMatch();
      const beat = (raw) => L.gameReportedScore(HOST, { match: 'ch-test-4821',
                                                       ...liveModule.parseHeartbeatScore(raw) });
      assert.equal(beat('0|0:1|1:0;lim=2').finished, false);
      assert.equal(beat('0|0:1|1:0;lim=2').finished, false, 'an unchanged beat decides nothing');
      const done = beat('0|0:2|1:0;lim=2');
      assert.equal(done.finished, true, 'first to 2 wins, off the heartbeat');
      assert.equal(done.winner, 1, 'and the host side takes it');
      assert.equal(match.rounds.length, 2, 'both rounds recorded, the repeat not counted');
    });

    // ------------------------------------------------------------------ the DERIVED scoreline
    //
    // REPLAYED FROM SAM'S MATCH OF 2026-09-17 00:00-00:02 UTC, exactly as the probe ring holds it.
    // BB5's native team score is empty in a two-player match - every ch_bb5_score_none reads
    // `teams=0` - so nothing told the backend who won and the hub never closed anybody's game.
    // These rows are what DID arrive, and they are enough.
    //
    //   00:00:36  round n=0  a0=1;a1=0     kill 933 -> 294      team 0 took it
    //   00:01:11  round n=1  a0=0;a1=1     kill 294 -> 933      team 1
    //   00:01:52  round n=2  a0=0;a1=1     kill 294 -> 933      team 1, and that is 2
    //
    // The sweep had both players mapped (ch_team_write: 933 -> team 0, 294 -> team 1), which is
    // what lets a round be attributed to one of OUR sides at all.
    const derivedMatch = () => {
      const { L, match } = liveMatch({
        players: [HOST, FOE].map((id) => ({ steam_id: id, persona: '', accepted: true,
                                            connected: true })),
        teams: { 1: [HOST], 2: [FOE] },
        ingame: new Map([[HOST, 0], [FOE, 1]]),     // what the team sweep reported
        game_score_limit: 2,                        // what the heartbeat said GetScoreLimit() is
      });
      return { L, match };
    };
    const roundRow = (L, n, a0, a1) =>
      L._internals.gameReportedRound(HOST, { row: `n=${n};w=-1;sec=20;a0=${a0};a1=${a1}`, source: 'delegate' });

    await test('a round is scored by who was left alive when it ended', () => {
      const { L, match } = derivedMatch();
      const r = roundRow(L, 0, 1, 0);
      assert.ok(r.ok, 'the row is still filed as a snapshot');
      assert.equal(r.scored.side, 1, 'in-game team 0 is our side 1, via the sweep');
      assert.deepEqual(match.score, { 1: 1, 2: 0 });
      assert.equal(r.scored.finished, false, 'one round is not a match');
    });

    await test("Sam's 1v1 settles at the limit, from the round rows alone", () => {
      const { L, match } = derivedMatch();
      roundRow(L, 0, 1, 0);
      const second = roundRow(L, 1, 0, 1);
      assert.deepEqual(match.score, { 1: 1, 2: 1 });
      assert.equal(second.scored.finished, false);
      const third = roundRow(L, 2, 0, 1);
      assert.equal(third.scored.finished, true, 'first to 2 - the match is decided');
      assert.equal(third.scored.winner, 2, 'and the friend took it, which is what happened');
      assert.equal(third.scored.limit, 2, 'the limit came off the heartbeat, not the default');
    });

    await test('the kill that ended the round scores it when the delegate does not', () => {
      // MEASURED: OnRoundEnded fired twice for three rounds on 2026-09-16, and three times for
      // three on 2026-09-17. The kill feed is the witness that does not miss.
      const { L, match } = derivedMatch();
      const r = L._internals.gameReportedKill(HOST, {
        row: `k=${FOE};v=${HOST};n=0;t=22;a0=0;a1=1`,
      });
      assert.ok(r.ok, r.error);
      assert.equal(r.scored.side, 2);
      assert.deepEqual(match.score, { 1: 0, 2: 1 });
    });

    await test('one round witnessed twice is still one round', () => {
      // The delegate row and the kill that ended it describe the SAME round. Counting both would
      // hand out a win at half the rounds actually played.
      const { L, match } = derivedMatch();
      L._internals.gameReportedKill(HOST, { row: `k=${HOST};v=${FOE};n=0;t=22;a0=1;a1=0` });
      roundRow(L, 0, 1, 0);
      roundRow(L, 0, 1, 0);
      assert.deepEqual(match.score, { 1: 1, 2: 0 }, 'credited once, by round number');
    });

    await test('an ambiguous round is skipped, never guessed', () => {
      // A bomb round or a timeout leaves both sides alive; a last-man trade leaves neither. Neither
      // shape says who won, and inventing an answer hands somebody a win they did not earn. An
      // uncounted round costs a settle the match timeout already covers.
      const { L, match } = derivedMatch();
      assert.equal(roundRow(L, 0, 1, 1).scored, undefined, 'both alive: no winner named');
      assert.equal(roundRow(L, 1, 0, 0).scored, undefined, 'both wiped: no winner named');
      assert.ok(!match.score || (match.score[1] === 0 && match.score[2] === 0));
    });

    await test('a heartbeat round row does not score, only the delegate does', () => {
      // A heartbeat is a SAMPLE: its alive counts may have been taken after the next round's
      // respawn, which would credit the round to whoever happened to be standing.
      const { L, match } = derivedMatch();
      const r = L._internals.gameReportedRound(HOST, { row: 'n=0;w=-1;sec=20;a0=1;a1=0', source: 'heartbeat' });
      assert.ok(r.ok);
      assert.equal(r.scored, undefined);
      assert.ok(!match.score || match.score[1] === 0);
    });

    await test('a round cannot be scored while the teams are unmapped', () => {
      // The sweep may not have reached everybody yet. Naming a side we cannot map would be a
      // coin toss, so the round is left uncredited and a later one carries the match.
      const { L, match } = derivedMatch();
      match.ingame = new Map();                    // nobody has reported a team yet
      delete match.team_map;
      assert.equal(roundRow(L, 0, 1, 0).scored, undefined);
      assert.ok(!match.score || match.score[1] === 0, 'and nothing was applied');
    });

    await test('a repeated score is not a second round', () => {
      // The pak reports every 15 s whether anything moved or not.
      const { L, match } = liveMatch();
      report(L, 1, 0);
      report(L, 1, 0);
      report(L, 1, 0);
      assert.equal(match.rounds.length, 1);
    });

    await test('a lost report is attributed rather than swallowed', () => {
      // One tick lost in transit means a report carrying two rounds. Recording it as one would
      // make the strip shorter than the score - a disagreement that makes a UI look broken.
      const { L, match } = liveMatch();
      report(L, 2, 0);
      assert.equal(match.rounds.length, 1);
      assert.equal(match.rounds[0].steps, 2, 'it says how many rounds it is standing in for');
      assert.equal(match.rounds[0][1], 2);
    });

    await test('the timeline is archived with the match', () => {
      const { L, match } = liveMatch();
      report(L, 1, 0);
      report(L, 1, 1);
      const rec = L._internals.archiveMatch(match, { outcome: 'played', reason: '' });
      assert.ok(Array.isArray(rec.rounds) && rec.rounds.length === 2,
                'a detail screen has nothing to draw without this');
      assert.deepEqual(rec.rounds.map((r) => r.won), [1, 2]);
    });

    await test('a match that never reported a score archives an empty timeline, not a missing one', () => {
      const { L, match } = liveMatch();
      const rec = L._internals.archiveMatch(match, { outcome: 'played', reason: '' });
      assert.deepEqual(rec.rounds, [], 'absent and empty must not look the same to a reader');
    });

    // ---------------------------------------------------------------- the forced score limit
    //
    // COMP_LO_SCORE_LIMIT_FORCE is the testing switch that lets a Railway variable decide a match
    // in two rounds instead of seven. Without it, whatever the gamemode reports wins - which is
    // the production behaviour and must stay the default.
    await test('the reported limit beats COMP_LO_SCORE_LIMIT when the switch is off', () => {
      const { L, match } = liveMatch();
      const r = report(L, 3, 0);
      assert.equal(match.score_limit, 7);
      assert.equal(r.finished, false, 'three of seven is not a win');
    });

    await test('COMP_LO_SCORE_LIMIT_FORCE decides the match at COMP_LO_SCORE_LIMIT instead', () => {
      const { L, match } = liveMatch();
      process.env.COMP_LO_SCORE_LIMIT_FORCE = '1';
      process.env.COMP_LO_SCORE_LIMIT = '2';
      let r;
      try {
        // The pak still says seven, because the pak still plays to seven.
        r = report(L, 2, 0);
      } finally {
        delete process.env.COMP_LO_SCORE_LIMIT_FORCE;
        delete process.env.COMP_LO_SCORE_LIMIT;
      }
      assert.equal(match.score_limit, 2, 'the forced limit is what the match was decided at');
      assert.equal(r.finished, true, 'two rounds ends a forced two-round match');
      assert.equal(r.winner, 1);
    });

    await test('the switch alone, with no limit set, is the ordinary 7', () => {
      const { L, match } = liveMatch();
      process.env.COMP_LO_SCORE_LIMIT_FORCE = 'true';
      let r;
      try {
        r = L.gameReportedScore(HOST, { scores: '0|0:3|1:0', limit: '2' });
      } finally {
        delete process.env.COMP_LO_SCORE_LIMIT_FORCE;
      }
      assert.equal(match.score_limit, 7, 'a pak reporting 2 cannot shorten a forced match');
      assert.equal(r.finished, false);
    });

    // ---------------------------------------------------------------- the ban clock
    //
    // "add a timer for each ban selection so someone cant stall the lobby out forever". The veto
    // used to be bounded only by LOBBY_SECONDS - ten minutes, then the whole match cancelled - so
    // one captain walking away held nine people hostage and then cost them the match.
    const lobbyMatch = (lobby) => {
      const L = stub();
      const { matches, inMatch } = L._internals;
      const match = {
        id: 'v1', state: 'ready', host: HOST,
        players: [HOST, MATE, FOE].map((id) => ({ steam_id: id, persona: '', accepted: true })),
        left: [], timer: null, ban_timer: null, stage_timer: null, deadline: 0,
        lobby: {
          teams: { 1: [HOST, MATE], 2: [FOE] },
          captains: { 1: HOST, 2: FOE },
          coin_captain: HOST,
          stage: 'veto', first_ban: 1, ban_turn: 1, bans: [],
          pool: ['Rome', 'Pool', 'Russian', 'Airsoft'], map: null,
          stage_deadline: 0, ban_deadline: 0,
          ...lobby,
        },
      };
      matches.set(match.id, match);
      for (const p of match.players) inMatch.set(p.steam_id, match.id);
      return { L, match };
    };
    const vetoMatch = () => lobbyMatch({});

    await test('a ban turn that runs out is banned FOR them, not cancelled', () => {
      const { L, match } = vetoMatch();
      L._internals.armStageTurn(match);
      assert.ok(match.lobby.stage_deadline > Date.now(), 'the turn is on a clock');

      L._internals.expireStageTurn(match.id);
      assert.equal(match.state, 'ready', 'the match must NOT be cancelled');
      assert.equal(match.lobby.bans.length, 1, 'a ban was recorded for the absent captain');
      assert.equal(match.lobby.bans[0].team, 1, 'and it counts against THEIR side');
      assert.equal(match.lobby.bans[0].auto, true, 'flagged, so the UI can say who really chose');
      assert.equal(match.lobby.ban_turn, 2, 'the turn passes on, so the veto keeps its shape');
      assert.ok(match.lobby.stage_deadline > Date.now(), 'and the next turn is on the clock too');
    });

    await test('an auto-ban finishes the veto exactly like a real one', () => {
      const { L, match } = vetoMatch();
      match.lobby.pool = ['Rome', 'Pool'];        // one ban from done
      L._internals.armStageTurn(match);
      L._internals.expireStageTurn(match.id);
      assert.equal(match.lobby.stage, 'ready');
      assert.ok(['Rome', 'Pool'].includes(match.lobby.map), 'a map was decided');
      assert.equal(match.stage_timer, null, 'and the clock is stopped, not left running');
      assert.equal(match.lobby.stage_deadline, 0);
    });

    await test('the auto-ban is not predictable', () => {
      // "the first remaining map" would let a captain farm a known ban by timing out on purpose.
      const picks = new Set();
      for (let i = 0; i < 40; i += 1) {
        const { L, match } = vetoMatch();
        L._internals.armStageTurn(match);
        L._internals.expireStageTurn(match.id);
        picks.add(match.lobby.bans[0].map);
      }
      assert.ok(picks.size > 1, `40 auto-bans all chose ${[...picks]}`);
    });

    await test('the ban clock does not extend the lobby backstop', () => {
      // arm() owns the ONE timer a match has, and that one is the lobby deadline. If the ban clock
      // used it, every ban would push the deadline out and a veto could outlive its own bound.
      const { L, match } = vetoMatch();
      match.timer = null;
      match.deadline = 12345;
      L._internals.armStageTurn(match);
      assert.equal(match.deadline, 12345, 'the lobby deadline was moved by a ban turn');
      assert.ok(match.stage_timer, 'the ban turn has its own timer');
    });
    // ------------------------------------------------- the other three lobby clocks
    //
    // "too many points where a user could infinitly stall the lobby". The ban clock above bounded
    // one of four places a captain can simply not click. These are the other three, and each of
    // them was worse than the ban was: the veto at least had a clock once it started, while a
    // captain who never called heads held the lobby at its FIRST screen until LOBBY_SECONDS
    // cancelled the match for all ten.

    await test('the coin call is on a clock, and running out calls it for them', () => {
      const { L, match } = lobbyMatch({ stage: 'coin', ban_turn: null, first_ban: null });
      L._internals.armStageTurn(match);
      assert.ok(match.lobby.stage_deadline > Date.now(), 'the coin call is on a clock');

      L._internals.expireStageTurn(match.id);
      assert.equal(match.state, 'ready', 'the match must NOT be cancelled');
      assert.ok(['heads', 'tails'].includes(match.lobby.coin_side), 'a call was made for them');
      assert.ok(['heads', 'tails'].includes(match.lobby.coin_result), 'and the coin was flipped');
      assert.ok([1, 2].includes(match.lobby.toss_winner), 'and a winner came out of it');
      assert.equal(match.lobby.coin_auto, true, 'flagged, so the hub can say the clock did it');
      assert.equal(match.lobby.stage, 'flipping', 'it goes in the air like a real call');
    });

    await test('the advantage choice is on a clock, and running out chooses for them', () => {
      const { L, match } = lobbyMatch({ stage: 'choice', toss_winner: 1, ban_turn: null });
      L._internals.armStageTurn(match);
      assert.ok(match.lobby.stage_deadline > Date.now(), 'the choice is on a clock');

      L._internals.expireStageTurn(match.id);
      assert.equal(match.state, 'ready', 'the match must NOT be cancelled');
      assert.ok(['side', 'ban'].includes(match.lobby.advantage), 'an advantage was taken');
      assert.equal(match.lobby.advantage_auto, true);
      // The shape has to survive it: BOTH halves of the advantage are handed out, and the ban
      // order is set from them, exactly as a real choice would.
      assert.ok([1, 2].includes(match.lobby.side_picker));
      assert.ok([1, 2].includes(match.lobby.ban_advantage));
      assert.notEqual(match.lobby.side_picker, match.lobby.ban_advantage,
                      'one team cannot hold both advantages');
      assert.equal(match.lobby.ban_turn, match.lobby.first_ban, 'the veto order was set');
      assert.equal(match.lobby.stage, 'side');
      assert.ok(match.lobby.stage_deadline > Date.now(), 'and the NEXT stage is on the clock too');
    });

    await test('the side pick is on a clock, and running out picks for them', () => {
      const { L, match } = lobbyMatch({ stage: 'side', toss_winner: 1, side_picker: 1,
                                        ban_advantage: 2 });
      L._internals.armStageTurn(match);
      L._internals.expireStageTurn(match.id);
      assert.equal(match.state, 'ready', 'the match must NOT be cancelled');
      assert.ok(['attack', 'defend'].includes(match.lobby.sides[1]), 'a side was picked');
      assert.equal(match.lobby.sides[2], match.lobby.sides[1] === 'attack' ? 'defend' : 'attack',
                   'and the other team got the opposite, as a real pick gives them');
      assert.equal(match.lobby.side_auto, true);
      assert.equal(match.lobby.stage, 'veto');
      assert.ok(match.lobby.stage_deadline > Date.now(), 'the first ban turn is on the clock');
    });

    await test('no auto-choice is predictable', () => {
      // Same reason the auto-ban is random: a fixed fallback is an outcome a captain could FARM by
      // refusing to click. Stalling has to be neutral, never profitable.
      const coins = new Set(); const kinds = new Set(); const sides = new Set();
      for (let i = 0; i < 40; i += 1) {
        const a = lobbyMatch({ stage: 'coin' });
        a.L._internals.expireStageTurn(a.match.id);
        coins.add(a.match.lobby.coin_side);
        const b = lobbyMatch({ stage: 'choice', toss_winner: 1 });
        b.L._internals.expireStageTurn(b.match.id);
        kinds.add(b.match.lobby.advantage);
        const c = lobbyMatch({ stage: 'side', side_picker: 1 });
        c.L._internals.expireStageTurn(c.match.id);
        sides.add(c.match.lobby.sides[1]);
      }
      assert.equal(coins.size, 2, `40 auto coin calls all chose ${[...coins]}`);
      assert.equal(kinds.size, 2, `40 auto advantages all chose ${[...kinds]}`);
      assert.equal(sides.size, 2, `40 auto side picks all chose ${[...sides]}`);
    });

    await test('the coin is held in the air so all ten see it land', () => {
      // It used to go straight from the call to 'choice', so only the captain who pressed the
      // button ever saw a coin at all - their hub faked a local spin. Everyone else got the result
      // line with nothing in between.
      const { L, match } = lobbyMatch({ stage: 'coin' });
      L._internals.flipCoin(HOST, { side: 'heads' });
      assert.equal(match.lobby.stage, 'flipping', 'the toss is a real stage, not a client animation');
      assert.ok(match.lobby.coin_result, 'the face is decided when it goes UP');
      assert.ok([1, 2].includes(match.lobby.toss_winner), 'and so is the winner');
      assert.equal(match.lobby.coin_auto, false, 'a real call is not flagged as the clock\'s');
      const held = L._internals.lobbyPayload(match);
      assert.ok(held.stage_seconds > 0, 'and every client is told how long it is in the air');

      L._internals.expireStageTurn(match.id);
      assert.equal(match.lobby.stage, 'choice', 'it comes down on its own');
      assert.ok([1, 2].includes(match.lobby.toss_winner), 'onto the face it was already on');
      assert.ok(match.lobby.stage_deadline > Date.now(), 'and the choice is on its clock');
    });

    await test('every stage a person can hold up carries a clock in the payload', () => {
      // The hub draws `stage_seconds` and nothing else, so a stage the payload reports as 0 is a
      // stage with no visible clock - which is exactly the bug, wearing a payload.
      for (const stage of ['coin', 'flipping', 'choice', 'side', 'veto']) {
        const { L, match } = lobbyMatch({ stage, toss_winner: 1, side_picker: 1 });
        L._internals.armStageTurn(match);
        const p = L._internals.lobbyPayload(match);
        assert.ok(p.stage_seconds > 0, `${stage} has no clock in the payload`);
        assert.ok(p.stage_total_seconds > 0, `${stage} has no window in the payload`);
      }
      // 'ready' is waiting on the connect window, not on a person, so it must NOT have one.
      const { L, match } = lobbyMatch({ stage: 'ready', map: 'Rome' });
      L._internals.armStageTurn(match);
      assert.equal(L._internals.lobbyPayload(match).stage_seconds, 0);
      assert.equal(match.stage_timer, null, 'and no timer was left running on it');
    });

    await test('a restored match gets its stage clock back, under either name', () => {
      // A match written by the PREVIOUS release stored the veto deadline as `ban_deadline`. Read
      // only `stage_deadline` and that match comes back with no clock, which is the stall this
      // whole section exists to prevent, arriving by way of a redeploy.
      for (const key of ['stage_deadline', 'ban_deadline']) {
        const { L, match } = lobbyMatch({ stage: 'veto', stage_deadline: 0, ban_deadline: 0 });
        match.lobby[key] = Date.now() + 9000;
        L._internals.rearm(match);
        assert.ok(match.stage_timer, `a match restored with ${key} got no clock`);
        L._internals.clearStageTurn(match);
      }
    });

    await test('closing a match stops its stage clock', () => {
      const { L, match } = lobbyMatch({ stage: 'coin' });
      L._internals.armStageTurn(match);
      L._internals.closeMatch(match, 'stalled', []);
      assert.equal(match.stage_timer, null, 'a dead match was left with a timer running');
    });


    await test('a one-sided match agrees on one team id, not two', () => {
      // COMP_MATCH_SIZE=1 is how the whole flow is tested by one person, and a real match whose
      // second team all walked out looks the same. Demanding two in-game ids makes both of them
      // permanently un-startable.
      const { L, match } = withMatch({ state: 'connecting', teams: { 1: [HOST, MATE, FOE], 2: [] } });
      allIn(match);
      for (const id of [HOST, MATE, FOE]) L.gameReportedTeam(HOST, { subject: id, team: 0 });
      assert.equal(match.state, 'connecting');
      assert.equal(L.teamsAgree(match).one_sided, true);
      assert.equal(L.teamsAgree(match).mapping.length, 1);
    });

    await test('a one-sided match still catches a player the game split off', () => {
      const { L, match } = withMatch({ state: 'connecting', teams: { 1: [HOST, MATE, FOE], 2: [] } });
      allIn(match);
      L.gameReportedTeam(HOST, { subject: HOST, team: 0 });
      L.gameReportedTeam(HOST, { subject: MATE, team: 0 });
      L.gameReportedTeam(HOST, { subject: FOE, team: 1 });
      assert.equal(match.state, 'connecting', 'two ids where the lobby uses one is a disagreement');
      assert.match(L.teamsAgree(match).reason, /the lobby uses 1/);
    });

    await test('a match with no decided teams cannot satisfy the gate', () => {
      const { L, match } = withMatch({ state: 'connecting', teams: null, lobby: null });
      allIn(match);
      for (const id of [HOST, MATE, FOE]) L.gameReportedTeam(HOST, { subject: id, team: 0 });
      assert.equal(match.state, 'connecting');
      assert.equal(L.teamsAgree(match).ok, false, 'it refuses rather than guessing agreement');
    });

    // THE TRAVEL PERMIT survives a dropped reply. It used to be deleted by the asking, so a lobby
    // pak whose held reply never came back had spent its one question and could never travel - the
    // match ran its connect window out and cancelled with nobody at fault (2026-09-16).
    await test('asking for the travel permit does not spend it', () => {
      const { L } = withMatch({ state: 'connecting' });
      const I = L._internals;
      L.grantHostPermit(HOST, 300);
      assert.equal(L.takeHostPermit(HOST), true, 'the first ask is answered');
      assert.equal(L.takeHostPermit(HOST), true, 'and so is a retry after a dropped reply');
    });

    await test('arriving in the match world revokes the permit', () => {
      const { L, match } = withMatch({ state: 'connecting' });
      const I = L._internals;
      L.grantHostPermit(HOST, 300);
      L.gameReportedIn(HOST, 'ch_lobby_read');
      assert.equal(L.takeHostPermit(HOST), false,
                   'a later lobby load must not travel them back into a match they have left');
      assert.equal(match.players.find((p) => p.steam_id === HOST).connected, true);
    });

    await test('an expired permit is refused, and not kept around', () => {
      const { L } = withMatch({ state: 'connecting' });
      const I = L._internals;
      L.grantHostPermit(HOST, -1);                 // already in the past
      assert.equal(L.takeHostPermit(HOST), false);
      assert.equal(I.hostPermits.has(HOST), false, 'the dead entry is cleared on the way past');
    });
  }

  const failed = results.filter((r) => !r.ok);
  console.log(`\n${results.length - failed.length}/${results.length} tests passed`);
  if (failed.length > 0) {
    process.exitCode = 1;
  }
}

main().catch((err) => {
  console.error('[test] fatal:', err);
  process.exitCode = 1;
});
