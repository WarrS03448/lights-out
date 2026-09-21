'use strict';

/**
 * Lights Out — download page + gamemode catalogue service.
 *
 * Plain Node.js (>=20), CommonJS. Optional account email uses Nodemailer;
 * routing and the game services use built-in modules.
 */

const http = require('http');
const fs = require('fs');
const path = require('path');
const authModule = require('./auth.cjs');
const adminModule = require('./admin.cjs');
const liveModule = require('./live.cjs');
const analyticsModule = require('./analytics.cjs');
const rulesBuildModule = require('./rulesbuild.cjs');
const { describeHeaders, describeBody } = require('./diagnostic-privacy.cjs');
// The rank ladder the site advertises is the one the service actually ranks people on: the
// names live in ladder.cjs and nowhere else (docs/ranks.md), and a rank renamed by an
// environment dial renames it on this page too.
const ladder = require('./ladder.cjs');

const PUBLIC_DIR = path.join(__dirname, 'public');
const INDEX_HTML_PATH = path.join(PUBLIC_DIR, 'index.html');
const ABOUT_HTML_PATH = path.join(PUBLIC_DIR, 'about.html');
// The three sections that were anchors on the front page until 2026-09-16 (#ranked, #how, #faq).
// They are pages now, rendered through the same placeholder pass as the other two, so the ladder
// on /ranked still comes from ladder.cjs and the version in every footer still comes from the
// catalogue. Extensionless paths are the canonical ones; the .html spellings redirect nowhere and
// simply render the same page, as /about.html already did.
const RANKED_HTML_PATH = path.join(PUBLIC_DIR, 'ranked.html');
const HOW_HTML_PATH = path.join(PUBLIC_DIR, 'how.html');
const FAQ_HTML_PATH = path.join(PUBLIC_DIR, 'faq.html');
const CATALOGUE_JSON_PATH = path.join(PUBLIC_DIR, 'catalogue.json');
const HUB_DIR = path.join(PUBLIC_DIR, 'hub');
const PACKS_DIR = path.join(PUBLIC_DIR, 'packs');
// The site's own stylesheet and the two font families it renders with. Flat, like the other two
// static dirs: serveStaticFile takes ONE path segment and rejects anything with a separator in it.
const ASSETS_DIR = path.join(PUBLIC_DIR, 'assets');

const PKG = JSON.parse(fs.readFileSync(path.join(__dirname, 'package.json'), 'utf8'));

const BODY_LIMIT_BYTES = 4 * 1024; // 4 KB
const STORE_PREFIX = process.env.HUB_STORE_PREFIX || 'hub:';
const recordingModule=require('./recording.cjs');
const recording=recordingModule.config();
const ACCOUNT_PREFIX=process.env.HUB_ACCOUNT_STORE_PREFIX||STORE_PREFIX;
const privateAccounts=recording?.mode==='account-test';
let analyticsService;
function analytics() {
  if (!analyticsService) analyticsService = analyticsModule.create({
    store: process.env.UPSTASH_REDIS_REST_URL && process.env.UPSTASH_REDIS_REST_TOKEN ? (args,options)=>upstashCmd(args,{...options,timeout:5000}) : null,
    prefix: STORE_PREFIX,
  });
  return analyticsService;
}
const telemetryLimits = new Map();
async function handleTelemetry(req, res) {
  const account = await auth().whoami(auth().bearer(req));
  if (!account) return sendJson(res, 401, { ok:false, error:'Sign in required.' });
  const now = Date.now(), id = account.player_id || account.steam_id;
  let limit = telemetryLimits.get(id);
  if (!limit || now-limit.at>60000) { limit={at:now,count:0}; telemetryLimits.set(id,limit); }
  if (telemetryLimits.size>10000) for (const [key,value] of telemetryLimits) if(now-value.at>60000) telemetryLimits.delete(key);
  if (++limit.count>30) return sendJson(res,429,{ok:false,error:'Telemetry rate limit.'},{'retry-after':'60'});
  let body;
  try { body=JSON.parse((await readBody(req,65536)).toString('utf8')); }
  catch { return sendJson(res,400,{ok:false,error:'Invalid telemetry batch.'}); }
  try {
    const accepted=await analytics().ingest(body.events,{actor_id:id,source:'client'});
    return sendJson(res,200,{ok:true,accepted});
  } catch(e) { return sendJson(res,/Expected|Invalid/.test(e.message)?400:503,{ok:false,error:'Telemetry batch was not saved.'}); }
}

// ---------------------------------------------------------------------------
// Native lobby clock and bounded operational match diagnostics.
// Current ranked reports use a per-match capability. The old public diagnostic
// viewer, clearing and unused permit endpoints have been retired.
// ---------------------------------------------------------------------------
const PROBE_BODY_LIMIT_BYTES = 16 * 1024;   // bigger than /api/event: we do not know the shape yet
// THE RING HAS TO OUTLAST A MATCH, and 50 did not. A live BB5 match reports from five
// mechanisms at once - the state beat, the stats sweep, the score report, the kill feed and the
// team sweep - which is roughly eighty rows a minute together. At 50 the window was about ninety
// seconds, so by the time a run could be looked at, the part worth looking at had already been
// pushed out: the 2026-09-15 23:43 match came back as fifty rows spanning 87 s, and a mechanism
// that had been silent for the whole match was indistinguishable from one whose rows had simply
// aged out. Six hundred is about seven minutes at full rate, which covers the opening of a match,
// where every question we currently have is decided.
const PROBE_KEEP = 600;                     // most recent entries kept
const PROBE_KEY = `${STORE_PREFIX}probe`;
const probeLog = [];                        // newest first; also mirrored to Upstash when configured

const STATIC_ALLOWED_EXTENSIONS = new Set([
  '.exe', '.zip', '.json',
  // ...and the site's own presentation, served from /assets/.
  '.css', '.js', '.woff2', '.svg', '.png', '.txt',
]);

const CONTENT_TYPES = {
  '.exe': 'application/vnd.microsoft.portable-executable',
  '.zip': 'application/zip',
  '.json': 'application/json',
  '.html': 'text/html; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.txt': 'text/plain; charset=utf-8',
  '.woff2': 'font/woff2',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
};

// Extensions whose content never changes for a given filename, so the browser may keep them for a
// year. A font file is the definition of that; the stylesheet deliberately is NOT (it is edited in
// place, and a cached one would render the next page revision wrong).
const IMMUTABLE_EXTENSIONS = new Set(['.zip', '.exe', '.woff2']);

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

function sendJson(res, statusCode, payload, extraHeaders) {
  const body = Buffer.from(JSON.stringify(payload));
  res.writeHead(statusCode, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': body.length,
    'cache-control': 'no-store',
    ...extraHeaders,
  });
  res.end(body);
}

function notFound(res) {
  sendJson(res, 404, { error: 'Not found.' });
}

function badRequest(res, message, extraHeaders) {
  sendJson(res, 400, { error: message || 'Bad request.' }, extraHeaders);
}

function readCatalogue() {
  const raw = fs.readFileSync(CATALOGUE_JSON_PATH, 'utf8');
  return JSON.parse(recording?recording.catalogue(raw):raw);
}

/**
 * What the competitive queue demands: the versions of the catalogue THIS server is serving.
 *
 * There is no second list. Publishing a release - bumping hub.version, or the ranked gamemode's
 * version, in public/catalogue.json - is what raises the bar, so the thing players download and
 * the thing the queue insists on can never disagree. live.cjs explains what is gated (the queue,
 * and only the queue: a match already under way is untouched).
 *
 * Cached for a few seconds so a queue join never pays for a disk read, and re-read after that so
 * a catalogue edited in place takes effect without a restart. An UNREADABLE catalogue returns
 * null, which opens the queue rather than closing it: a broken file must not lock the world out.
 */
let requiredCache = { at: 0, value: null };
function requiredVersions() {
  const now = Date.now();
  if (requiredCache.value && now - requiredCache.at < 10_000) return requiredCache.value;
  let value = null;
  try {
    const catalogue = readCatalogue();
    const modes = Array.isArray(catalogue.gamemodes) ? catalogue.gamemodes : [];
    const entry = modes.find((m) => m && m.id === liveModule.GATED_MODE_ID);
    value = {
      hub: String((catalogue.hub && catalogue.hub.version) || ''),
      // AS SERVED, not as stored. readCatalogue() is the file on disk and never carries the
      // override, so without this the gate would demand 1.0.15 from a hub that had correctly
      // installed 1.0.15.2 - and, worse, would accept 1.0.15 from one that had not updated.
      mode: overriddenVersion(entry),
    };
  } catch {
    value = null;
  }
  requiredCache = { at: now, value };
  return value;
}

/**
 * Reads the request body up to a byte limit. Resolves with a Buffer, or
 * rejects with an Error tagged `.tooLarge = true` if the limit is exceeded.
 */
function readBody(req, limitBytes) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let total = 0;
    let settled = false;

    req.on('data', (chunk) => {
      if (settled) return;
      total += chunk.length;
      if (total > limitBytes) {
        settled = true;
        // Stop accumulating, but do not destroy the socket here — req and
        // res share the same underlying connection, and destroying it now
        // would prevent the 400 response below from ever reaching the
        // client. We ask the client to close via the "connection: close"
        // response header instead once the error response has been sent.
        req.pause();
        req.removeAllListeners('data');
        const err = new Error('Request body too large.');
        err.tooLarge = true;
        reject(err);
        return;
      }
      chunks.push(chunk);
    });

    req.on('end', () => {
      if (settled) return;
      settled = true;
      resolve(Buffer.concat(chunks));
    });

    req.on('error', (err) => {
      if (settled) return;
      settled = true;
      reject(err);
    });
  });
}

/**
 * Resolves `requestedName` (a single, already-decoded path segment) inside
 * `baseDir`, verifying with a realpath containment check that the result
 * cannot escape baseDir (defense in depth against path traversal, on top of
 * URL normalization and the segment checks done by the caller).
 *
 * Returns the real, absolute file path on success, or null if the file does
 * not exist / resolves outside baseDir.
 */
function resolveStaticFile(baseDir, requestedName) {
  const candidate = path.join(baseDir, requestedName);

  let realBase;
  try {
    realBase = fs.realpathSync(baseDir);
  } catch {
    return null;
  }

  let realCandidate;
  try {
    realCandidate = fs.realpathSync(candidate);
  } catch {
    return null;
  }

  const rel = path.relative(realBase, realCandidate);
  const escapesBase = rel === '' || rel.startsWith('..') || path.isAbsolute(rel);
  if (escapesBase) return null;

  return realCandidate;
}

function serveStaticFile(req, res, baseDir, rawSegment) {
  let decoded;
  try {
    decoded = decodeURIComponent(rawSegment);
  } catch {
    return badRequest(res);
  }

  if (
    !decoded ||
    decoded.includes('\0') ||
    decoded.includes('/') ||
    decoded.includes('\\') ||
    decoded === '.' ||
    decoded === '..'
  ) {
    return badRequest(res);
  }

  const ext = path.extname(decoded).toLowerCase();
  if (!STATIC_ALLOWED_EXTENSIONS.has(ext)) {
    return notFound(res);
  }

  const realPath = resolveStaticFile(baseDir, decoded);
  if (!realPath) return notFound(res);

  let stat;
  try {
    stat = fs.statSync(realPath);
  } catch {
    return notFound(res);
  }
  if (!stat.isFile()) return notFound(res);

  const headers = {
    'content-type': CONTENT_TYPES[ext] || 'application/octet-stream',
    'content-length': stat.size,
  };
  if (IMMUTABLE_EXTENSIONS.has(ext)) {
    headers['cache-control'] = 'public, max-age=31536000, immutable';
  } else {
    headers['cache-control'] = 'no-store';
  }

  res.writeHead(200, headers);

  if (req.method === 'HEAD') {
    res.end();
    return;
  }

  const stream = fs.createReadStream(realPath);
  stream.on('error', () => {
    // The response may already be partially sent; just terminate it.
    res.destroy();
  });
  stream.pipe(res);
}

// ---------------------------------------------------------------------------
// Route handlers
// ---------------------------------------------------------------------------

function handleIndex(req, res) {
  return handleHtmlPage(req, res, INDEX_HTML_PATH);
}

function handleAbout(req, res) {
  return handleHtmlPage(req, res, ABOUT_HTML_PATH);
}

function handleRanked(req, res) {
  return handleHtmlPage(req, res, RANKED_HTML_PATH);
}

function handleHow(req, res) {
  return handleHtmlPage(req, res, HOW_HTML_PATH);
}

function handleFaq(req, res) {
  return handleHtmlPage(req, res, FAQ_HTML_PATH);
}

function escapeHtml(value) {
  return String(value)
    .split('&').join('&amp;')
    .split('<').join('&lt;')
    .split('>').join('&gt;')
    .split('"').join('&quot;');
}

function formatBytes(bytes) {
  const n = Number(bytes);
  if (!Number.isFinite(n) || n <= 0) return '';
  if (n >= 1024 * 1024) return `${Math.round(n / (1024 * 1024))} MB`;
  return `${Math.round(n / 1024)} KB`;
}

// The download page lists the modes it is offering. Rendered from the catalogue rather than typed
// into the HTML, because the two drift the moment a mode ships: the old page still said "currently
// Capture the Flag 10v10" a release after Bodybomb 5v5 went out.

// The rule chips on a mode card, read out of that mode's OWN English ruleset. Each pattern either
// matches and contributes a chip or it does not, so a mode whose ruleset is worded differently
// simply shows fewer chips — never a wrong one, never an empty row. Deriving them beats a table
// keyed by mode id for the same reason the card text is rendered rather than typed: a hand-kept
// table is a second place to forget when a mode's rules change.
const RULE_CHIPS = [
  [/First team to (\d+)/i, (m) => `First to ${m[1]}`],
  [/One life per round/i, () => 'One life'],
  [/Respawn: (\d+) seconds/i, (m) => `${m[1]}s respawn`],
  [/Rounds last (\d+) minutes/i, (m) => `${m[1]} min rounds`],
  [/(\d+)-minute time limit/i, (m) => `${m[1]} min`],
  [/Sides swap after round (\d+)/i, () => 'Sides swap'],
];

const MAX_CHIPS = 5;

function modeChips(mode) {
  const ruleset = (mode.rulesets && typeof mode.rulesets.en === 'string') ? mode.rulesets.en : '';
  const chips = [];
  for (const [pattern, label] of RULE_CHIPS) {
    const found = ruleset.match(pattern);
    if (found) chips.push(label(found));
  }
  // The description carries its own shouty bracketed asides — "[4X DRONE COOLDOWN]",
  // "[NO SPECTATOR DRONES]". They are facts about the mode, so they become chips like the rest
  // instead of sitting in the middle of a sentence in capitals.
  for (const bracket of String(mode.description || '').match(/\[([^\]]+)\]/g) || []) {
    const text = bracket.slice(1, -1).trim();
    if (text) chips.push(text.charAt(0) + text.slice(1).toLowerCase());
  }
  return chips.slice(0, MAX_CHIPS);
}

// "Capture the Flag 10v10" -> badge "10v10", name "Capture the Flag". The format is the first thing
// a player wants from a mode card, and it reads better as a badge than as a suffix on the name. A
// title with no NvN in it keeps its full name and gets no badge.
function splitModeTitle(title) {
  const found = title.match(/\s*(\d+\s*v\s*\d+)\s*$/i);
  if (!found) return { name: title, format: '' };
  return {
    name: title.slice(0, found.index).trim() || title,
    format: found[1].replace(/\s+/g, '').toLowerCase(),
  };
}

// The visible ladder as chips, bottom rung first. The capstone gets `.top` because it is the
// one rank with no divisions; everything else about the row is the same.
function renderRankLadder() {
  const names = ladder.NAMES.map((name) => `            <li>${escapeHtml(name)}</li>`);
  if (ladder.TOP) names.push(`            <li class="top">${escapeHtml(ladder.TOP)}</li>`);
  return names.join('\n');
}

function renderGamemodeRows(catalogue) {
  const modes = (catalogue && Array.isArray(catalogue.gamemodes)) ? catalogue.gamemodes : [];
  if (!modes.length) return '';
  return modes
    .map((mode) => {
      const { name, format } = splitModeTitle(String(mode.title || mode.id || 'Gamemode'));
      const version = escapeHtml(mode.version || '');
      // Without the bracketed asides, which are chips now.
      const description = escapeHtml(
        String(mode.description || '').replace(/\[[^\]]*\]/g, '').replace(/\s+/g, ' ').trim(),
      );
      const chips = modeChips(mode)
        .map((chip) => `<li>${escapeHtml(chip)}</li>`)
        .join('');
      // The one mode ranked runs on wears the same tag the app's gamemode screen puts on it
      // (hub/webui/static/screens/gamemodes.js: `.gm-tag` beside the name, accent border on the
      // row), and for the same reason: nothing else on a card says which one the ladder uses.
      // It is read from live.cjs rather than typed, so the site and the queue gate can never
      // disagree about which mode that is.
      const ranked = String(mode.id || '') === liveModule.GATED_MODE_ID;
      return [
        `          <li class="mode${ranked ? ' ranked' : ''}">`,
        '            <div class="mode-top">',
        format ? `              <span class="mode-format">${escapeHtml(format)}</span>` : '',
        `              <h3 class="mode-name">${escapeHtml(name)}</h3>`,
        ranked ? '              <span class="mode-tag">Ranked</span>' : '',
        version ? `              <span class="mode-ver">v${version}</span>` : '',
        '            </div>',
        description ? `            <p class="mode-desc">${description}</p>` : '',
        chips ? `            <ul class="mode-chips">${chips}</ul>` : '',
        '          </li>',
      ].filter(Boolean).join('\n');
    })
    .join('\n');
}

// Renders a page from public/, substituting the placeholders the two pages share. Every value comes
// from the catalogue, so the site can never advertise a version or a mode list the hub does not
// actually serve.
function handleHtmlPage(req, res, htmlPath) {
  const html = fs.readFileSync(htmlPath, 'utf8');
  const catalogue = readCatalogue();
  const hub = (catalogue && catalogue.hub) || {};
  const modes = (catalogue && Array.isArray(catalogue.gamemodes)) ? catalogue.gamemodes : [];
  const replacements = {
    '{{HUB_VERSION}}': hub.version || 'unknown',
    '{{HUB_SIZE}}': formatBytes(hub.size) || 'Windows',
    '{{GAMEMODE_COUNT}}': String(modes.length),
    '{{GAMEMODES}}': renderGamemodeRows(catalogue),
    '{{RANK_COUNT}}': String(ladder.RANKS),
    '{{RANK_RUNGS}}': String(ladder.BANDS + (ladder.TOP ? 1 : 0)),
    '{{RANK_DIVISIONS}}': String(ladder.DIVISIONS),
    '{{RANK_LADDER}}': renderRankLadder(),
  };
  let rendered = html;
  for (const [token, value] of Object.entries(replacements)) {
    rendered = rendered.split(token).join(value);
  }
  const body = Buffer.from(rendered, 'utf8');
  res.writeHead(200, {
    'content-type': 'text/html; charset=utf-8',
    'content-length': body.length,
  });
  if (req.method === 'HEAD') {
    res.end();
    return;
  }
  res.end(body);
}

/**
 * COMP_GAME_RULES_OVERRIDE - the in-game rules, set from Railway instead of from a pack.
 *
 * "GAME" because this is what the GAME plays to, as against COMP_LO_SCORE_LIMIT, which is what
 * Lights Out decides a win at (live.cjs). Two numbers, two machines, two names.
 *
 * A gamemode's round count is not in this service at all: it is `rules` in the pack's
 * manifest.json, which the hub bakes into the cooked DataAsset when it builds the pak on the
 * player's machine. So the numbers CAN be changed without cooking anything new - the pak is
 * rebuilt on every install anyway - but until now only by hand-editing a manifest on each test
 * machine, which is exactly the thing that made a two-round test match a chore.
 *
 * This puts that number on the wire. Set, for example:
 *
 *   COMP_GAME_RULES_OVERRIDE={"BB5":{"score_limit":2}}
 *
 * and every catalogue this service serves carries `rules_override` on the BB5 entry; the hub
 * merges it over the pack's manifest before building (hub/catalogue.py -> hub/ops.py ->
 * tools/pak/build_gamemode.py). Unset - production - and the catalogue goes out byte for byte as
 * it sits on disk, which is why the parsed copy is only built when there is an override.
 *
 * TESTING ONLY. It changes what the players actually play, and it does NOT change what the server
 * decides a win is: the gamemode reports its own limit and that is what live.cjs uses unless
 * COMP_LO_SCORE_LIMIT_FORCE is set too. Set both, to the same number, or the match the players
 * finish and the match the server rates will be different matches.
 *
 * A garbled value is IGNORED with a warning rather than fatal: a typo in an environment variable
 * must not take the catalogue - and with it the queue's version gate, the download page and every
 * install - off the air.
 */
const RULES_OVERRIDE_KEYS = ['score_limit', 'max_rounds', 'team_switch_interval',
                             'time_limit', 'team_size', 'max_players'];

/**
 * Fill in the two numbers that have to agree with a changed score limit, unless they were given.
 *
 * `score_limit` is first-to-N. The other two are not targets, they are shapes:
 *   max_rounds           FScoringConfig.MaxPhases - the HARD CAP on rounds played. The match stops
 *                        here whatever the score, so it only ever bites when it is lower than the
 *                        rounds first-to-N needs. Shipped BB5 is 7 and 12: twelve is 6-6, a DRAW,
 *                        because a stock Bodybomb match can end level.
 *   team_switch_interval FTeamConfig.TeamSwitchInterval - sides swap every N rounds (BB5: 6, so
 *                        once, at half time).
 *
 * A test does not want a draw: a match that ends level never reaches the limit, so the server is
 * never told anybody won and it sits in the live list until it times out. So the derived cap is
 * `2 * score_limit - 1` - the longest a first-to-N can run (first to 2 -> 3 rounds: 2-0, or 2-1) -
 * which GUARANTEES a winner, one round past the 2 * (limit - 1) where BB5 accepts a draw.
 *
 * The swap is `score_limit - 1`, which is what BB5 itself ships (7 -> 6) and what every other
 * first-to-N shooter does (CS2 and Valorant: first to 13, swap after 12). Floored at 1, since a
 * swap of 0 is not a swap.
 *
 * Derived only. Name either key in COMP_GAME_RULES_OVERRIDE and that value is used untouched,
 * which is how you would ask for a drawable 12-round match or a swap every round.
 */
function deriveRounds(rules) {
  const limit = Number(rules.score_limit);
  if (!Number.isFinite(limit) || limit < 1) return rules;
  const out = { ...rules };
  if (out.max_rounds === undefined) out.max_rounds = 2 * limit - 1;
  if (out.team_switch_interval === undefined) out.team_switch_interval = Math.max(1, limit - 1);
  return out;
}

let rulesOverrideCache = { raw: null, value: null };
function rulesOverride() {
  const raw = String(process.env.COMP_GAME_RULES_OVERRIDE || '').trim();
  if (rulesOverrideCache.raw === raw) return rulesOverrideCache.value;
  let value = null;
  if (raw) {
    try {
      const parsed = JSON.parse(raw);
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        throw new Error('not a JSON object of {mode_id: {rule: number}}');
      }
      const out = {};
      for (const [id, rules] of Object.entries(parsed)) {
        if (!rules || typeof rules !== 'object' || Array.isArray(rules)) continue;
        const clean = {};
        for (const key of RULES_OVERRIDE_KEYS) {
          const n = Number(rules[key]);
          if (rules[key] !== undefined && Number.isFinite(n)) clean[key] = n;
        }
        if (Object.keys(clean).length) out[String(id)] = deriveRounds(clean);
      }
      value = Object.keys(out).length ? out : null;
      if (value) console.log('COMP_GAME_RULES_OVERRIDE active:', JSON.stringify(value));
      else console.warn('COMP_GAME_RULES_OVERRIDE names no rule this server knows; ignored.');
    } catch (e) {
      console.warn('COMP_GAME_RULES_OVERRIDE is not usable and is ignored:', e.message);
      value = null;
    }
  }
  rulesOverrideCache = { raw, value };
  return value;
}

/**
 * EVERY RULES CHANGE GETS ITS OWN VERSION NUMBER (Sam, 2026-09-17: "lets just make it create a new
 * gamemode version for every update so its not confusing").
 *
 * The counting, and why it counts rather than hashes, is server/rulesbuild.cjs - it is in a module
 * of its own because the only way to test a counter is to move it, and server.cjs listens on
 * import. Here it is wired to the store and to the two places a version is published.
 */
const RULES_BUILD_KEY = recording?`${STORE_PREFIX}comp:gamemode-build`:'comp:gamemode-build';
const rulesBuilds = rulesBuildModule.createRulesBuilds(
  (id, value) => upstashCmd(['HSET', RULES_BUILD_KEY, id, value]));

/** Read the counters back at boot, so a redeploy does not re-issue a number it has already used. */
async function loadRulesBuilds() {
  let rows = null;
  try {
    rows = await upstashCmd(['HGETALL', RULES_BUILD_KEY]);
  } catch {
    rows = null;                    // the store is optional; the counters just start again
  }
  const map = rulesBuilds.restore(rows);
  if (map.size) console.log('gamemode build numbers restored:', JSON.stringify([...map]));
}

/**
 * What this entry's version is AS SERVED - with the rules build number, when it has an override.
 *
 * Used by the catalogue and by requiredVersions, so the queue gate and the hub compare the same
 * string. They did not, before: the gate read the version straight off the disk file, which is the
 * one thing on this path that never carries the override.
 */
function overriddenVersion(entry) {
  const override = rulesOverride();
  return rulesBuilds.versionFor(entry, (override && entry && override[entry.id]) || null);
}

/** The catalogue text, with `rules_override` attached to the entries COMP_GAME_RULES_OVERRIDE
 *  names, and their version numbered accordingly.
 *  Returns the input untouched when there is no override or the catalogue will not parse. */
function applyRulesOverride(raw) {
  const override = rulesOverride();
  if (!override) return raw;
  let catalogue;
  try {
    catalogue = JSON.parse(raw);
  } catch {
    return raw;                     // serve what is on disk; the hub reports the parse failure
  }
  const modes = Array.isArray(catalogue.gamemodes) ? catalogue.gamemodes : [];
  for (const m of modes) {
    if (m && override[m.id]) {
      m.rules_override = { ...override[m.id] };
      m.version = overriddenVersion(m);
    }
  }
  return JSON.stringify(catalogue);
}

function handleCatalogueJson(req, res) {
  const source=fs.readFileSync(CATALOGUE_JSON_PATH,'utf8');
  const raw = applyRulesOverride(recording?recording.catalogue(source):source);
  const body = Buffer.from(raw, 'utf8');
  res.writeHead(200, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': body.length,
    'cache-control': 'no-store',
  });
  if (req.method === 'HEAD') {
    res.end();
    return;
  }
  res.end(body);
}

function handleHubDownload(req, res) {
  const catalogue = readCatalogue();
  const hub = (catalogue && catalogue.hub) || {};
  // Serve an optional zip when the catalogue provides one; otherwise serve the installer.
  // hub.download_url stays executable because hub/update.py runs it for self-updates.
  const downloadUrl = hub.zip_url || hub.download_url;
  if (!downloadUrl) {
    return sendJson(res, 500, { error: 'Server error.' });
  }
  res.writeHead(302, {
    location: downloadUrl,
    'cache-control': 'no-store',
    'content-length': 0,
  });
  res.end();
}

function handleHealth(req, res) {
  const catalogue = readCatalogue();
  const gamemodes = Array.isArray(catalogue.gamemodes) ? catalogue.gamemodes.length : 0;
  const hubVersion = (catalogue.hub && catalogue.hub.version) || 'unknown';
  sendJson(res, 200, {
    ok: true,
    build: PKG.version,
    gamemodes,
    hub: hubVersion,
    // Present only while COMP_GAME_RULES_OVERRIDE is set, so "is the test override still on?" is
    // one request rather than a Railway dashboard trip.
    ...(rulesOverride() ? { rules_override: rulesOverride() } : {}),
  });
}

/**
 * Run one Upstash REST command, e.g. ['INCR', key] or ['LRANGE', key, '0', '9'].
 * Returns the command's result, or null when Upstash is not configured or the call fails
 * — callers treat Upstash as optional unless strict is requested. Rank reads need to
 * distinguish a missing key from an unavailable store before trusting a default rating.
 */
async function upstashCmd(args, { strict = false, timeout } = {}) {
  if(recording)recordingModule.assertStoreCommand(args,STORE_PREFIX);
  return rawUpstashCmd(args,{strict,timeout});
}
async function accountStore(args,options) {
  if(recording)recordingModule.assertAccountCommand(args,ACCOUNT_PREFIX+'accounts:');
  return rawUpstashCmd(args,options);
}
async function rawUpstashCmd(args, { strict = false, timeout } = {}) {
  const url = process.env.UPSTASH_REDIS_REST_URL;
  const token = process.env.UPSTASH_REDIS_REST_TOKEN;
  if (!url || !token) return null;

  try {
    const response = await fetch(url, {
      method: 'POST',
      headers: {
        authorization: `Bearer ${token}`,
        'content-type': 'application/json',
      },
      body: JSON.stringify(args),
      ...(timeout ? {signal:AbortSignal.timeout(timeout)} : {}),
    });
    if (!response.ok) throw new Error('Storage request failed');
    const parsed = await response.json();
    if (!parsed || parsed.error || !Object.prototype.hasOwnProperty.call(parsed, 'result')) {
      throw new Error('Invalid storage response');
    }
    return parsed.result;
  } catch (err) {
    // Never log the token or the raw error object (which may embed request
    // details); a short message is enough to know the hook failed.
    console.error('[server] upstash command failed');
    if (strict) throw new Error('Storage unavailable');
    return null;
  }
}

async function upstashIncr(key) {
  await upstashCmd(['INCR', key]);
}

async function handleApiEvent(req, res) {
  let raw;
  try {
    raw = await readBody(req, BODY_LIMIT_BYTES);
  } catch (err) {
    if (err && err.tooLarge) {
      // The client may still be sending body bytes we never read; ask it to
      // close the connection rather than risk those bytes being parsed as
      // the start of a new pipelined request.
      return badRequest(res, 'Request body too large.', { connection: 'close' });
    }
    return badRequest(res);
  }

  let payload;
  try {
    payload = JSON.parse(raw.toString('utf8'));
  } catch {
    return badRequest(res, 'Invalid JSON.');
  }

  if (
    !payload ||
    typeof payload !== 'object' ||
    typeof payload.gamemode !== 'string' ||
    !payload.gamemode ||
    (payload.action !== 'install' && payload.action !== 'uninstall')
  ) {
    return badRequest(res, 'Expected { gamemode: string, action: "install"|"uninstall" }.');
  }

  const key = `${STORE_PREFIX}events:${payload.action}:${payload.gamemode}`;
  await upstashIncr(key);

  res.writeHead(204, { 'cache-control': 'no-store' });
  res.end();
}

// ---------------------------------------------------------------------------
// Router
// ---------------------------------------------------------------------------

// --------------------------------------------------------------------- probe
function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[ch]);
}

// How long /api/probe/slow holds its reply open, in seconds. THE DELAY LIVES HERE, ON THE SERVER,
// on purpose: retuning it is a redeploy (seconds) instead of an editor build + cook + pak + install
// + Sam standing in a game doing nothing (many minutes). Clamped so a typo cannot wedge a request
// open past the client's own HTTP timeout, which we do not know and cannot measure from here.
// MEASURED (chlobby-9, 2026-09-14): at 12 s the game's response delegate NEVER fired, while the
// two ordinary probes in the same run answered in 2.06 s and did fire. So the game's HTTP client
// gives up somewhere in (2, 12] seconds. 4 s sits safely above the proven-working 2 s and well
// below the failure, and is still meaningfully 'later' than BeginPlay+epsilon for the readiness
// question. Raise it only with evidence; each change here is a redeploy, not a pak rebuild.
const PROBE_SLOW_SECONDS = Math.max(1, Math.min(25, Number(process.env.PROBE_SLOW_SECONDS) || 4));

// Travel requests carry the host's per-match reporting capability. Never infer
// identity from an address: players on one router must remain independent.
async function hostTravelIdentity(req) {
  const match = /^Bearer ([0-9a-f]{64})$/.exec(String(req.headers.authorization || ''));
  if (req.method !== 'POST' || !match) return '';
  try {
    const service = live();
    await service._internals.ready;
    await service._internals.ensureRecovery();
    const authorised = await (service.authoriseReportFresh||service.authoriseReport)(match[1]);
    return authorised ? authorised.steamId : '';
  } catch { return ''; }
}

// A request we deliberately never answer in time. The game's HTTP client abandons a request it
// cannot get a reply to and does NOT fire its response delegate for one it abandons (chlobby-9: a
// 12 s hold never fired, while 2.06 s and 4.09 s both did). That is what a player without a travel
// permit gets: the pak sits there and the game behaves as though it were not installed.
//
// WHY THIS NUMBER IS LARGE, AND IT IS NOT ARBITRARY. We cannot simply drop the socket, because the
// edge in front of this server turns a destroyed connection into a 502 - a real HTTP response.
// SendAttributionEvent's delegate fires on a FAILED status just as well as a successful one
// (ch_bit_404 came back), and chlobby-28's OnHostDelay handler IGNORES the bool entirely: it goes
// straight to the IsStandalone branch and travels. So a 502 that arrives while the game is still
// listening would travel the player anyway and defeat the whole gate.
//
// The defence is time, not status: hold far past the abandon window so that by the time any
// response - ours, or the edge's - can reach the client, the client has already given up and the
// delegate can no longer fire. 45 s against a measured-dead 12 s is ~4x margin.
//
// The durable fix is a pak whose host arm BRANCHES on bSuccess, so a 404 means "no" explicitly
// instead of being inferred from silence. That needs a cook; this does not.
const PROBE_IGNORE_SECONDS = 45;

/**
 * The team/roster ruling, answered as an HTTP STATUS because that is all the game can read.
 *
 * `bSuccess` on SendAttributionEvent's response delegate tracks the status (chlobby-3: 200 -> true,
 * chlobby-5: 404 -> false), so:
 *
 *   200  yes   - on the roster ('member'), or on team 1 ('side')
 *   404  no    - a stranger ('member'), or on team 2 ('side')
 *
 * A question we cannot answer - a caller who is not a host mid-match, a subject who is on no team,
 * a malformed id - is 409, NOT 404. Both are "false" to a pak that only reads the bool, but the
 * distinction is the difference between "team 2" and "we do not know", and the log has to keep it
 * or a bad deploy would look exactly like a match where everyone is on team 2. The caller is
 * expected to treat any non-200 to a 'member' ask as "do not admit", which is the safe direction.
 *
 * The probe is logged either way (handleProbe does it), so a run is reconstructable afterwards.
 */
function parseStartReady(body) {
  if (!body || typeof body !== 'object' || Array.isArray(body) || body.event_name !== 'ch_start_ready') throw Error('bad event');
  const token = /^chm-([0-9a-f]{16})$/.exec(String(body.storefront || ''));
  const raw = String(body.platform || ''), cut = raw.indexOf('|');
  if (!token || cut < 1 || raw.indexOf('|', cut + 1) !== -1) throw Error('bad snapshot');
  const n = Number(raw.slice(0, cut));
  if (!Number.isInteger(n) || n < 2 || n > 10 || String(n) !== raw.slice(0, cut)) throw Error('bad count');
  let tail = raw.slice(cut + 1); if (tail.endsWith(';')) tail = tail.slice(0, -1);
  const encoded = tail ? tail.split(';') : [];
  if (encoded.length !== n) throw Error('wrong count');
  const seen = new Set(), rows = encoded.map(text => {
    const m = (privateAccounts?/^(\d{17}|):([01]):([01])$/:/^(\d{17}):([01]):([01])$/).exec(text);
    if (!m || (m[1]&&seen.has(m[1]))) throw Error('bad row');
    if(m[1])seen.add(m[1]); return { steam_id: m[1], team: Number(m[2]), active: Number(m[3]) };
  });
  return { host: String(body.user_id || ''), matchId: token[1], rows };
}

async function handleStartReady(req, res) {
  const cap = await authoriseRankedReport(req, res);
  if (!cap) return;
  let ruling;
  try {
    if (req.method !== 'POST') throw Error('POST required');
    const body = JSON.parse((await readBody(req, PROBE_BODY_LIMIT_BYTES)).toString('utf8'));
    const presence = body?.event_name === 'ch_match_presence';
    let data;
    if(presence) {
      const key=/^chm-([0-9a-f]{16})$/.exec(String(body.storefront||''));
      const roster=/^([1-9]|10)\|((?:\d{17};)+)$/.exec(String(body.platform||''));
      const ids=roster?roster[2].slice(0,-1).split(';'):[];
      if(!key||!roster||body.first_session_timestamp!=='chpresence-1'||ids.length!==Number(roster[1])||new Set(ids).size!==ids.length)throw Error('invalid presence');
      data={matchId:key[1],rows:ids.map(steam_id=>({steam_id,active:1}))};
    } else data=parseStartReady(body);
    if (data.matchId !== cap.authorised.matchId) throw Error('wrong match');
    ruling = await runAuthorised(cap,()=>presence ? cap.service.matchPresence(cap.authorised.steamId, data.matchId, data.rows)
      : cap.service.startReady(cap.authorised.steamId, data.matchId, data.rows));
    if (ruling.started) console.log('[match] complete roster approved %s', data.matchId);
  } catch { ruling = { ok: false, error: 'invalid start snapshot' }; }
  return sendJson(res, ruling.ok ? 200 : 409, ruling);
}

async function handleFinalSnapshot(req, res) {
  const incoming = await readRankedReport(req, res);
  if (!incoming) return;
  const key = /^chm-([0-9a-f]{16})$/.exec(String(incoming.body.storefront || ''));
  if (req.method !== 'POST' || !key || incoming.body.event_name !== 'ch_final_snapshot' || incoming.body.first_session_timestamp !== 'chfinal-1')
    return sendJson(res,409,{ok:false,error:'invalid final snapshot',data_collected:false});
  const cap = await authoriseRankedReport(req, res, key[1]);
  if (!cap) return;
  let result, raw = incoming.raw;
  try {
    const body = incoming.body;
    if (key[1] !== cap.authorised.matchId) throw Error('wrong match');
    result = await runAuthorised(cap,()=>cap.service.finalSnapshot(cap.authorised.steamId, {
      match_id: key[1], rows: body.platform, meta: body.timestamp, combat_end: body.ip }));
  } catch { result = { ok: false, error: 'invalid final snapshot', data_collected: false }; }
  await recordReport(req, raw, { report: { event: 'ch_final_snapshot', match: cap.authorised.matchId,
    status: result.ok ? 200 : 409, error: result.error || '', data_collected: result.data_collected === true } });
  return sendJson(res, result.ok ? 200 : 409, result);
}

async function handleProbeSlow(req, res) {
  // WHY THIS EXISTS (chlobby-8, 2026-09-14). K2_SetTimer does not fire on the lobby GameMode. The
  // cooked bytecode is provably correct - CallMath K2_SetTimer(Self, String 'LateFind', Float 25.0,
  // False, ...) at 0x05f2, reached via PushExecutionFlow -> 05e0 from the BeginPlay Sequence, with
  // LateFind a real function entering the ubergraph at offset 2480 - and ch_timer_fired still never
  // arrived after three minutes. So the timer is out.
  //
  // What DOES fire in the lobby, in every build we have run, is SendAttributionEvent's response
  // delegate: ch_bit_ok has come back 250-400 ms after its call every single time, and bSuccess is
  // proven to track the HTTP STATUS rather than mere transport. So the delay becomes the server's
  // job: record the event immediately (so the log shows the ask arrived), then hold the reply open.
  // The game's response delegate fires when we finally answer, and THAT is what runs the late
  // search - no timer, no variable, no class-default change, only machinery already proven here.
  // THE GATE. A permit is granted only when a competitive match opens its connect window, to that
  // match's host, and is revoked when that host arrives in the match world (live.cjs). Requests
  // may retry until arrival. No permit -> no reply -> no travel, and the player keeps a stock game: they can
  // launch normally, and they can LEAVE a match without being hosted straight back into it.
  const asker = await hostTravelIdentity(req);
  let permitted = false;
  try {
    permitted = Boolean(asker) && live().takeHostPermit(asker);
  } catch { permitted = false; }          // live not up yet: fail closed, never auto-host

  if (!permitted) {
    console.log('[probe] travel clock NOT granted for %s - no permit', asker || 'unknown caller');
    await handleProbe(req, res, { delaySeconds: PROBE_IGNORE_SECONDS, silent: true });
    return;
  }
  console.log('[probe] travel clock granted to %s', asker);
  // THE HOST'S GAME IS UP. This ask comes from GM_CHLobby's BeginPlay, so the host has finished
  // booting and is about to travel - which is the moment to stop the connect clock expiring
  // underneath them. It is not an arrival and marks nobody connected; see live.noteHostLaunching.
  try { live().noteHostLaunching(asker); } catch { /* live not up yet: the clock simply stands */ }
  await handleProbe(req, res, { delaySeconds: PROBE_SLOW_SECONDS });
}

// Which GM_BB5 events prove the host is standing in the match world. ch_lobby_read is the earliest
// (T_READ = 12 s after the match world's BeginPlay, bb5_graphs.py:71) and is the one that now
// RELEASES the joiners - the lobby they search for carries its name from the moment the game opens
// it, so there is nothing left to wait for. ch_lobby_write follows at 30 s and is kept as a second
// chance in case the first is lost in transit. ch_lobby_verify is NOT here: by the time it fires
// the joiners have been released long ago.
const ARRIVAL_EVENTS = new Set(['ch_lobby_read', 'ch_lobby_write']);

/** Best effort, and deliberately silent: a probe must never be able to fail a telemetry write. */
function noteMatchArrival(entry, service = live()) {
  try {
    const body = JSON.parse(entry.body || '');
    if (!ARRIVAL_EVENTS.has(body.event_name)) return;
    const result = service.gameReportedIn(body.user_id, body.event_name);
    if (result && result.ok) {
      console.log('[probe] host %s reported in from the match (%s) - %d/%d connected',
                  result.host, result.event, result.connected, result.total);
    }
  } catch {
    /* not our JSON, or live is not up yet */
  }
}

// THE SCOREBOARD (Sam, 2026-09-15). GM_BB5 reads ABodycamGameState::GetTeams() and
// GetScoreLimit() on a timer and packs the result into one SendAttributionEvent, which lands
// here like every other probe. The fields are cramped on purpose - SendAttributionEvent has a
// fixed signature, so the payload rides inside the strings it already has:
//
//   event_name              "ch_bb5_score"
//   user_id                 the reporting host's SteamID64
//   storefront              the CH_MATCH id stamped on the lobby, so a late report cannot be
//                           applied to the wrong match
//   platform                "<hostTeamId>|<teamId>:<score>|<teamId>:<score>"
//   timestamp               GetScoreLimit(), as a string
//
// The host's own in-game team is in there because we never call SetTeamId, so the game's team
// ids mean nothing to us on their own. See live.cjs gameReportedScore for the mapping.
const SCORE_EVENT = 'ch_bb5_score';

// ...AND THE SAME SCOREBOARD OFF THE HEARTBEAT (2026-09-16). ch_bb5_score has one branch in front
// of it in the pak - GetTeams() must list two teams - and on 2026-09-16 that branch was false for
// a whole 1v1: 270 ch_bb5_stats and 53 ch_bb5_state landed, and not one ch_bb5_score, so the match
// was never settled and the hub never closed anybody's game. The heartbeat has no branch in front
// of it, so it now carries the scoreline too, in the one string field it was not using:
//
//   first_session_timestamp  "<hostTeamId>|0:<score>|1:<score>;lim=<scoreLimit>"
//
// It is fed to the SAME gameReportedScore, which is what keeps one settle path rather than two:
// the stale-report guard, the round timeline and the win rule are all still in that one function.
// A report from either source is indistinguishable once it gets there.
const SCORE_NONE_EVENT = 'ch_bb5_score_none';   // the false arm, carrying the length it saw

// PER-PLAYER STATS (tier 1 - docs/match-data.md). Same channel, different event name:
//   event_name  "ch_bb5_stats"
//   user_id     the reporting host
//   storefront  the CH_MATCH id
//   platform    one or more "<steamId>|<kills>:<deaths>:<rounds>:<roundsWon>:<clutches>",
//               comma-separated if the field turns out to be long enough to hold the roster
//   timestamp   rounds played so far
const STATS_EVENT = 'ch_bb5_stats';
const ROUND_EVENT = 'ch_bb5_round';
const STATE_EVENT = 'ch_bb5_state';   // the same payload from a plain timer - see gm_heartbeat
const KILL_EVENT = 'ch_bb5_kill';

function noteMatchStats(entry, service = live()) {
  try {
    const body = JSON.parse(entry.body || '');
    if (body.event_name !== STATS_EVENT) return;
    const result = service.gameReportedStats(body.user_id, {
      match: body.storefront, rows: body.platform, rounds: body.timestamp,
    });
    if (result && result.ok) {
      console.log('[probe] stats: %d row(s), %d player(s) known', result.players, result.known);
    } else if (result && result.error) {
      console.log('[probe] stats from %s ignored: %s', body.user_id, result.error);
    }
  } catch {
    /* not our JSON, or live is not up yet */
  }
}

function noteMatchKill(entry, service = live()) {
  try {
    const body = JSON.parse(entry.body || '');
    if (body.event_name !== KILL_EVENT) return;
    const result = service.gameReportedKill(body.user_id, {
      match: body.storefront, row: body.platform,
    });
    if (result && result.ok) {
      const k = result.kill;
      console.log('[probe] kill %d: %s -> %s%s', result.kills,
                  k.killer || '(world)', k.victim, k.teamKill ? '  TEAM KILL' : '');
    } else if (result && result.error) {
      console.log('[probe] kill from %s ignored: %s', body.user_id, result.error);
    }
  } catch {
    /* not our JSON, or live is not up yet */
  }
}

function noteMatchRound(entry, service = live()) {
  try {
    const body = JSON.parse(entry.body || '');
    const beat = body.event_name === STATE_EVENT;
    if (body.event_name !== ROUND_EVENT && !beat) return;
    const result = service.gameReportedRound(body.user_id, {
      match: body.storefront, row: body.platform, source: beat ? 'heartbeat' : 'delegate',
    });
    if (result && result.ok) {
      console.log('[probe] round %d of %d known (%s): %j',
                  result.round, result.known, beat ? 'heartbeat' : 'OnRoundEnded', result.row);
    } else if (result && result.error) {
      console.log('[probe] round from %s ignored: %s', body.user_id, result.error);
    }
  } catch {
    /* not our JSON, or live is not up yet */
  }
}

// The heartbeat repeats every 5 s and an unchanged score is still a report, so the same line would
// otherwise be printed twelve times a minute and bury everything else in the deploy log. A DECIDED
// match is never squashed; only the unchanged chatter in front of it is.
let lastScoreLine = '';
function sayScore(line) {
  if (line === lastScoreLine) return;
  lastScoreLine = line;
  console.log(line);
}

function noteMatchScore(entry, service = live()) {
  try {
    const body = JSON.parse(entry.body || '');

    // The gate's false arm. Nothing to settle - it is a measurement, and the one line it prints is
    // the difference between "the scoreline graph is not in this build" and "it is, and GetTeams()
    // is short". Both looked identical until this event existed.
    if (body.event_name === SCORE_NONE_EVENT) {
      sayScore(`[probe] no scoreline from ${body.user_id}: ${body.platform} (GetTeams is short)`);
      return;
    }

    // The heartbeat's copy. Same settle path, different field - and only when the tail parses, so
    // a build that predates the scoreline (first_session_timestamp '') is simply not a score
    // report, which is what it was before.
    let fields = null;
    if (body.event_name === STATE_EVENT) {
      const parsed = liveModule.parseHeartbeatScore(body.first_session_timestamp);
      if (!parsed) return;
      fields = { match: body.storefront, ...parsed };
    } else if (body.event_name === SCORE_EVENT) {
      fields = { match: body.storefront, scores: body.platform, limit: body.timestamp };
    } else {
      return;
    }
    const result = service.gameReportedScore(body.user_id, fields);
    if (result && result.ok && result.finished) {
      console.log('[probe] match %s decided: team %d wins %d-%d',
                  result.match_id, result.winner, result.score[1], result.score[2]);
    } else if (result && result.ok) {
      sayScore(`[probe] score ${result.score[1]}-${result.score[2]} (first to ${result.limit})`);
    } else if (result && result.error) {
      // Worth a line: a score report being REJECTED is the failure mode that would otherwise be
      // silent, and it is the first thing to look at when a match does not settle.
      sayScore(`[probe] score report from ${body.user_id} ignored: ${result.error}`);
    }
  } catch {
    /* not our JSON, or live is not up yet */
  }
}

// THE IN-GAME TEAMS (Sam, 2026-09-15). GM_BB5's team sweep walks its roster one player per tick
// and reports what team the game has that player on. The rows are what lets the server hold the
// start until the match agrees with the lobby - see live.cjs teamsAgree and goLiveIfReady.
//
//   event_name   "ch_team_verified" (legacy ch_team_write is diagnostics only)
//   user_id      the reporting host's SteamID64        <- who is reporting
//   storefront   the SUBJECT's SteamID64               <- who the row is about
//   platform     "idx:roster:want:after:lib"
//   timestamp    the world clock, truncated, as a string
//
// READ THE PAYLOAD BY INDEX, NOT BY REGEX. Every one of those fields can be negative - TeamID is
// -1 until the game assigns one, and so is GetPlayerTeamID - and an all-optional positional tail
// matched with one pattern is what silently binned 100% of the stats rows on 2026-09-15: a "-1"
// slid into the next field's slot and the whole row still "matched".
const TEAM_EVENT = 'ch_team_verified';
const TEAM_FIELDS = 5;

function noteMatchTeams(entry, service = live()) {
  try {
    const body = JSON.parse(entry.body || '');
    if (body.event_name !== TEAM_EVENT) return;
    const f = String(body.platform || '').split(':');
    if (f.length < TEAM_FIELDS) return;
    // Both the property and the game's reader must reflect the confirmed side. A failed
    // readback invalidates any previous observation instead of leaving stale agreement.
    const team = ['0', '1'].includes(f[2]) && f[2] === f[3] && f[3] === f[4] ? f[3] : -1;
    const result = service.gameReportedTeam(body.user_id, { subject: body.storefront, team, verified: true });
    if (!result || !result.ok) return;
    if (result.started) {
      console.log('[probe] teams agree - match %s is live (host on in-game team %s)',
                  result.match_id, body.platform.split(':')[3]);
    } else if (result.ready && !result.agree) {
      // The line that matters when a start is being held: WHY, in the words the gate used.
      console.log('[probe] team %s/%s reported (%s on %s) - not starting: %s',
                  result.reported, result.total, result.subject, result.team, result.reason);
    }
  } catch {
    /* not our JSON, or live is not up yet */
  }
}

// TEAM KILLS. GM_BB5 binds ABodycamGameMode::OnPlayerKilled and reports the ones where both
// players are on the same team - never a suicide, never an unassigned player, never an ordinary
// enemy kill (see bb5_graphs.py gm_teamkill for why each of those is refused at the source).
//
//   event_name   "ch_team_kill"
//   user_id      the reporting host's SteamID64
//   storefront   the KILLER's SteamID64
//   timestamp    the VICTIM's SteamID64      (repeat targeting is a signal on its own)
//   platform     "team:elapsed:round:alive0:alive1"
//
// Read by index after splitting, like every other packed payload here: these fields can be
// negative, and a regex with an optional tail is what silently binned 100% of the stats rows.
const TEAMKILL_EVENT = 'ch_team_kill';
const TEAMKILL_FIELDS = 5;

function noteTeamKill(entry, service = live()) {
  try {
    const body = JSON.parse(entry.body || '');
    if (body.event_name !== TEAMKILL_EVENT) return;
    const f = String(body.platform || '').split(':');
    if (f.length < TEAMKILL_FIELDS) return;
    const result = service.teamKillReported(body.user_id, {
      killer: body.storefront, victim: body.timestamp,
      team: f[0], elapsed: f[1], round: f[2], alive0: f[3], alive1: f[4],
    });
    if (!result || !result.ok) return;
    console.log('[probe] team kill %s -> %s (round %s, %ss in) flags=[%s]%s',
                result.killer, result.victim, f[2], f[1], (result.flags || []).join(','),
                result.enforced ? ' PENALISED' : (result.malicious ? ' (not enforced)' : ''));
  } catch {
    /* not our JSON, or live is not up yet */
  }
}

/**
 * Apply the old unauthenticated wire only to a server-marked match restored from before report
 * credentials shipped. The live service binds reporter, host, match and lifecycle before any
 * legacy parser runs. Combat evidence is intentionally absent: this bridge may finish an
 * interrupted match, but cannot create new sanction or rating evidence.
 */
function applyRestoredLegacyReport(entry, service = live()) {
  let body;
  try { body = JSON.parse(entry.body || ''); } catch { return false; }
  const event = String(body && body.event_name || '');
  const matchClaim = new Set([SCORE_EVENT, STATS_EVENT, ROUND_EVENT, STATE_EVENT, KILL_EVENT]).has(event)
    ? String(body.storefront || '') : '';
  if (!service.authoriseLegacyReport(String(body.user_id || ''), event, matchClaim)) return false;
  noteMatchArrival(entry, service);
  noteMatchScore(entry, service);
  noteMatchStats(entry, service);
  noteMatchRound(entry, service);
  noteMatchKill(entry, service);
  noteMatchTeams(entry, service);
  noteTeamKill(entry, service); // diagnostic only; legacy team-kill reports never classify or enforce
  return true;
}

const RESTORED_LEGACY_EVENTS = new Set([
  ...ARRIVAL_EVENTS, SCORE_EVENT, STATS_EVENT, ROUND_EVENT, STATE_EVENT, KILL_EVENT,
  TEAM_EVENT, TEAMKILL_EVENT,
]);

function restoredLegacyCandidate(entry) {
  try {
    const body = JSON.parse(entry.body || '');
    return RESTORED_LEGACY_EVENTS.has(String(body && body.event_name || ''));
  } catch { return false; }
}

/** The ranked mode rules the hub actually bakes into its pak, with the served override merged in. */
function effectiveRankedRules() {
  // Railway deploys from server/, so the source manifest may not be present.
  // This is also the catalogue that clients use to build the actual game pak.
  const served = expectedCompetitiveRules();
  if (served) return served;
  let base = {};
  try {
    const id = String(liveModule.GATED_MODE_ID || 'BB5');
    const manifestPath = path.join(__dirname, '..', 'gamemodes', id.toLowerCase(), 'manifest.json');
    const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
    if (manifest && manifest.rules && typeof manifest.rules === 'object' &&
        !Array.isArray(manifest.rules)) base = { ...manifest.rules };
    const override = rulesOverride();
    return { ...base, ...((override && override[id]) || {}) };
  } catch {
    const override = rulesOverride();
    return { ...((override && override[liveModule.GATED_MODE_ID]) || {}) };
  }
}

const MATCH_ID_RE = /^[0-9a-f]{16}$/;

/**
 * Apply one authenticated match-world report. The capability supplies every authority-bearing
 * field; payload identity and match fields are telemetry only. Subject fields stay payload-owned
 * for team verification and team-kill evidence because they name somebody other than the host.
 */
function dispatchRankedReport(body, reportAuth, service = live()) {
  body = body && typeof body === 'object' ? body : {};
  const event = String(body.event_name || '');
  const host = String(reportAuth && reportAuth.steamId || '');
  const match = String(reportAuth && reportAuth.matchId || '');
  const scoreLimit = Number(reportAuth && reportAuth.scoreLimit);
  const matchEvents = new Set([SCORE_EVENT, STATS_EVENT, ROUND_EVENT, STATE_EVENT, KILL_EVENT, 'ch_combat_v1']);
  const claimed = String(body.storefront || '');
  if (matchEvents.has(event) && MATCH_ID_RE.test(claimed) && claimed !== match) {
    return { status: 409, error: 'Report belongs to a different match.' };
  }

  let result = null;
  if (ARRIVAL_EVENTS.has(event)) {
    result = service.gameReportedIn(host, event);
  } else if (event === STATS_EVENT) {
    result = service.gameReportedStats(host, {
      match, rows: body.platform, rounds: body.timestamp,
    });
  } else if (event === 'ch_combat_v1') {
    result = service.gameReportedCombat(host, { match, row: body.platform }, { authenticated: true });
  } else if (event === KILL_EVENT) {
    result = service.gameReportedKill(host, { match, row: body.platform });
  } else if (event === ROUND_EVENT || event === STATE_EVENT) {
    result = service.gameReportedRound(host, {
      match, row: body.platform, source: event === STATE_EVENT ? 'heartbeat' : 'delegate',
    });
    if (event === STATE_EVENT) {
      const parsed = liveModule.parseHeartbeatScore(body.first_session_timestamp);
      if (parsed) result = service.gameReportedScore(host, {
        match, scores: parsed.scores, limit: String(scoreLimit),
      });
    }
  } else if (event === SCORE_EVENT) {
    result = service.gameReportedScore(host, {
      match, scores: body.platform, limit: String(scoreLimit),
    });
  } else if (event === TEAM_EVENT) {
    const fields = String(body.platform || '').split(':');
    if (fields.length >= TEAM_FIELDS) {
      const team = ['0', '1'].includes(fields[2]) && fields[2] === fields[3] &&
        fields[3] === fields[4] ? fields[3] : -1;
      result = service.gameReportedTeam(host, {
        subject: body.storefront, team, verified: true,
      });
    }
  } else if (event === TEAMKILL_EVENT) {
    const fields = String(body.platform || '').split(':');
    if (fields.length >= TEAMKILL_FIELDS) {
      result = service.teamKillReported(host, {
        killer: body.storefront, victim: body.timestamp,
        team: fields[0], elapsed: fields[1], round: fields[2],
        alive0: fields[3], alive1: fields[4],
      });
    }
  }
  return { status: 200, result };
}

async function authoriseRankedReport(req, res, finalMatchId) {
  const service = live();
  try {
    await service._internals.ready;
    await service._internals.ensureRecovery();
  } catch {
    sendJson(res, 503, { ok: false, error: 'Ranked state is recovering.' });
    return null;
  }
  const match = /^Bearer ([0-9a-f]{64})(?:\.(0|[1-9]\d{0,5}))?$/.exec(String(req.headers.authorization || ''));
  let authorised;
  try {
    authorised = match ? (finalMatchId && service.authoriseFinalReport
      ? await service.authoriseFinalReport(match[1],finalMatchId) : await (service.authoriseReportFresh||service.authoriseReport)(match[1])) : null;
  } catch { sendJson(res,503,{ok:false,error:'Match receipt is temporarily unavailable.'}); return null; }
  if (!authorised) {
    sendJson(res, 401, { ok: false, error: 'Invalid match report credential.' });
    return null;
  }
  if((authorised.epoch||0)!==Number(match[2]||0)) {
    sendJson(res,401,{ok:false,error:'Superseded host authority.'});return null;
  }
  return { service, authorised };
}

async function handleHostMigration(req,res) {
  const cap=/^Bearer ([a-f0-9]{64})\.(0|[1-9]\d{0,5})$/.exec(String(req.headers.authorization||''));
  if(req.method!=='POST'||!cap)return sendJson(res,401,{ok:false});
  const parsed=await readRankedReport(req,res);if(!parsed)return;
  const b=parsed.body,key=/^chm-([a-f0-9]{16})$/.exec(String(b.storefront||''));
  const operation=b.event_name==='ch_host_activate'?'activate':b.event_name==='ch_host_endorse'?'endorse':'';
  if(!key||!operation||b.first_session_timestamp!=='chmigration-1' ||
     (b.platform!==''&&!/^\d{17}$/.test(String(b.platform||''))))return sendJson(res,409,{ok:false});
  try {
    const service=live();await service._internals.ready;await service._internals.ensureRecovery();
    const result=await service.migrationReport(cap[1],{match_id:key[1],operation,epoch:Number(cap[2]),candidate:b.platform});
    return sendJson(res,result.ok?200:409,result);
  } catch {return sendJson(res,503,{ok:false,error:'Host handoff could not be saved.'});}
}

function runAuthorised(cap,operation) {
  return cap.service.withReportAuthority ? cap.service.withReportAuthority(cap.authorised,operation) : operation();
}

async function readRankedReport(req, res) {
  let raw;
  try {
    raw = await readBody(req, PROBE_BODY_LIMIT_BYTES);
  } catch (err) {
    sendJson(res, err && err.tooLarge ? 413 : 400, { ok: false, error: 'Invalid report body.' });
    return null;
  }
  try {
    const body = JSON.parse(raw.toString('utf8') || '{}');
    if (!body || typeof body !== 'object' || Array.isArray(body)) throw new Error('object required');
    return { raw, body };
  } catch {
    sendJson(res, 400, { ok: false, error: 'Invalid report JSON.' });
    return null;
  }
}

async function recordReport(req, raw, extra = {}) {
  const entry = {
    at: new Date().toISOString(), method: req.method || '', url: new URL(req.url || '/', 'http://local').pathname,
    headers: describeHeaders(req.headers), bodyBytes: raw.length, bodyTruncated: false,
    body: describeBody(raw.toString('utf8')), ...extra,
  };
  probeLog.unshift(entry);
  if (probeLog.length > PROBE_KEEP) probeLog.length = PROBE_KEEP;
  await upstashCmd(['LPUSH', PROBE_KEY, JSON.stringify(entry)]);
  await upstashCmd(['LTRIM', PROBE_KEY, '0', String(PROBE_KEEP - 1)]);
}

async function handleMatchReport(req, res) {
  const cap = await authoriseRankedReport(req, res);
  if (!cap) return;
  const parsed = await readRankedReport(req, res);
  if (!parsed) return;
  let applied;
  try {
    applied = await runAuthorised(cap,async()=>{
      const out=dispatchRankedReport(parsed.body,cap.authorised,cap.service);
      if(out.result?.then)out.result=await out.result;
      return out;
    });
    if (applied.result && applied.result.ok === false) { applied.status = 409; applied.error = applied.result.error; }
  } catch {
    sendJson(res, 500, { ok: false, error: 'Report processing failed.' });
    return;
  }
  await recordReport(req, parsed.raw, { report: { event: String(parsed.body.event_name || ''),
    match: cap.authorised.matchId, host: cap.authorised.steamId, status: applied.status } });
  if (applied.status !== 200) {
    sendJson(res, applied.status, { ok: false, error: applied.error });
    return;
  }
  sendJson(res, 200, { ok: true });
}

async function handleMatchReportTeam(req, res) {
  const cap = await authoriseRankedReport(req, res);
  if (!cap) return;
  const parsed = await readRankedReport(req, res);
  if (!parsed) return;
  const subject = String(parsed.body.storefront || '');
  const ask = String(parsed.body.platform || 'member');
  let ruling = { ok: false, error: 'live not up' };
  try { ruling = await runAuthorised(cap,()=>cap.service.teamRuling(cap.authorised.steamId, subject, ask)); } catch { /* fail closed */ }
  const status = ruling && ruling.ok ? (ruling.yes ? 200 : 404) : 409;
  await recordReport(req, parsed.raw, { ruling: {
    ask, host: cap.authorised.steamId, subject, status, ...(ruling || {}),
  } });
  sendJson(res, status, { ok: status === 200, ask, team: (ruling && ruling.team) || 0 });
}

async function handleCombatBatch(req, res) {
  const cap = await authoriseRankedReport(req, res);
  if (!cap) return;
  const parsed = await readRankedReport(req, res);
  if (!parsed) return;
  let result, status;
  try {
    if (parsed.body.event_name !== 'ch_combat_batch') throw Error('invalid batch event');
    result = await runAuthorised(cap,()=>cap.service.combatBatch(cap.authorised.steamId, cap.authorised.matchId, parsed.body.platform));
    status = result.ok ? 200 : 409;
  } catch { status = 503; result = { ok: false, error: 'Combat data is not saved yet.' }; }
  // Ordinary batches are frequent. Keep only errors, newly quarantined samples,
  // and the terminal batch so the probe retains useful completion evidence.
  if (!result.ok || result.new_rejected || String(parsed.body.platform || '').includes(';terminal=1;'))
    await recordReport(req, parsed.raw, { report: { event: 'ch_combat_batch', match: cap.authorised.matchId,
      status, error: result.error || '', rejected: result.rejected || 0 } });
  return sendJson(res, status, result);
}

async function handleProbe(req, res, opts) {
  let raw = Buffer.alloc(0);
  let tooLarge = false;
  try {
    raw = await readBody(req, PROBE_BODY_LIMIT_BYTES);
  } catch (err) {
    tooLarge = Boolean(err && err.tooLarge);
  }

  const entry = {
    at: new Date().toISOString(),
    method: req.method || '',
    url: new URL(req.url || '/', 'http://local').pathname,                 // never retain query parameters
    headers: describeHeaders(req.headers),
    bodyBytes: raw.length,
    bodyTruncated: tooLarge,
    body: describeBody(raw.toString('utf8')),
  };

  probeLog.unshift(entry);
  if (probeLog.length > PROBE_KEEP) probeLog.length = PROBE_KEEP;

  // THE HOST'S GAME REPORTING IN. GM_BB5's timers only run once the match world exists, so one of
  // these arriving IS the host's arrival - see live.cjs gameReportedIn for why that beats the hub
  // asserting it, and for the trust trade. Anything that is not a host mid-connect is ignored
  // quietly: the overwhelming majority of probes are ordinary telemetry.
  let legacyService = null;
  let legacyRecoveryFailed = false;
  if (restoredLegacyCandidate(entry)) {
    legacyService = live();
    try {
      await legacyService._internals.ready;
      await legacyService._internals.ensureRecovery();
    } catch {
      legacyRecoveryFailed = true;
    }
    if (!legacyRecoveryFailed) applyRestoredLegacyReport(entry, legacyService);
  }

  // Best effort: an in-memory log is lost on every redeploy, and the whole point is to
  // still have the evidence when Sam gets round to looking at it.
  await upstashCmd(['LPUSH', PROBE_KEY, JSON.stringify(entry)]);
  await upstashCmd(['LTRIM', PROBE_KEY, '0', String(PROBE_KEEP - 1)]);

  if (legacyRecoveryFailed) {
    sendJson(res, 503, { ok: false, error: 'Ranked state is recovering.' });
    return;
  }

  const delay = opts && Number(opts.delaySeconds) > 0 ? Number(opts.delaySeconds) : 0;
  if (!delay) {
    // `status` lets a caller answer NO with a real status code rather than by silence. The game
    // reads exactly one bit and it tracks the HTTP status, so 404 is a usable "no" - see
    // handleProbeJoin. Defaults to 200, which is every other probe's answer.
    const status = Number(opts && opts.status) || 200;
    sendJson(res, status, { ok: status === 200 });
    return;
  }
  // Hold the reply open. The entry is ALREADY logged above, so the probe log shows the ask landing
  // at once and the answer arriving `delay` seconds later - which is itself the measurement of
  // whether the game will wait this long at all. If the client gives up first we will see the ask
  // with no matching done event, and that tells us the HTTP timeout is shorter than `delay`.
  const silent = Boolean(opts && opts.silent);
  const timer = setTimeout(() => {
    if (res.writableEnded) return;
    // A silent hold is the "no" answer: destroying the socket without a response means the game's
    // response delegate never fires, so the pak never travels. Answering 404 would ALSO be a no -
    // bSuccess tracks HTTP status - but only for a pak that branches on it, and chlobby-28 ignores
    // the bool entirely. Not replying is the one signal the CURRENT pak already obeys.
    if (silent) { res.destroy(); return; }
    sendJson(res, 200, { ok: true, delayed: delay });
  }, delay * 1000);
  // Do not keep the process alive for this, and do not leak the timer if the client hangs up.
  if (typeof timer.unref === 'function') timer.unref();
  res.on('close', () => clearTimeout(timer));
}

// Steam sign-in (Sam's decision C1). Built once, lazily, because it closes over
// upstashCmd/sendJson which are defined above but only exist at call time.
let authRouter = null;
const accountActivity = require('./account-activity.cjs').create();
let registrationIndexService;
function registrationIndex() {
  // Private test services may share account storage under a restricted command
  // allowlist. They must not migrate or expose the public population.
  if(recording || !process.env.UPSTASH_REDIS_REST_URL || !process.env.UPSTASH_REDIS_REST_TOKEN)return null;
  if(!registrationIndexService)registrationIndexService=require('./registered-players.cjs').create({
    store:upstashCmd,prefix:ACCOUNT_PREFIX,authPrefix:STORE_PREFIX,rosterPrefix:STORE_PREFIX});
  return registrationIndexService;
}
function auth() {
  if (!authRouter) {
    authRouter = authModule.create({
      upstashCmd,
      prefix: STORE_PREFIX,
      sendJson,
      badRequest,
      accountPrefix:ACCOUNT_PREFIX,
      accountStore:privateAccounts?accountStore:upstashCmd,
      allowSteamId:recording?.allowed,
      privateGameplay:privateAccounts,
      recordSteamSignIn:async id=>{
        const index=registrationIndex();
        if(!index)return;
        try {if(await index.record(id))accountDirectory().invalidate();}
        catch(error){accountDirectory().invalidate();throw error;}
      },
      onAccountsChanged:()=>accountDirectory().invalidate(),
      accounts:privateAccounts?{...require('./accounts.cjs').configuration(),allowEmail:recording.allowEmail,allowGame:recording.allowed}:undefined,
      ownershipTransaction: ({account,pending},finish) => accountActivity.write(async()=>{
        const ledger=await require('./account-ownership.cjs').readSteam({upstashCmd,prefix:ACCOUNT_PREFIX},pending.steam_id);
        const ids=[...new Set([account.player_id||account.id,ledger?.player_id||pending.steam_id])];
        await live().prepareOwnership(ids);
        accountDirectory().invalidate();
        const result=await finish();
        if(result===1)live().ownershipChanged({ids,accountId:account.id,steamId:pending.steam_id});
        return result;
      }),
    });
  }
  return authRouter;
}

// The admin console: a WEB PAGE you sign into with Steam (server/admin.cjs), on this service
// rather than a second one - a separate deploy would run the same code against the same database,
// and what protects the data is the sign-in, not the hostname.
let adminRouter = null;
let accountDirectoryService;
function accountDirectory() {
  if (!accountDirectoryService) accountDirectoryService = require('./admin-accounts.cjs').create({
    store:process.env.UPSTASH_REDIS_REST_URL && process.env.UPSTASH_REDIS_REST_TOKEN
      ? (privateAccounts ? accountStore : upstashCmd) : null,
    prefix:ACCOUNT_PREFIX,
    registrations:registrationIndex(),
    onUpdate:()=>liveRouter?.broadcastStats(),
  });
  return accountDirectoryService;
}
function admin() {
  if (!adminRouter) {
    adminRouter = adminModule.create({
      upstashCmd,
      analytics: analytics(),
      prefix: STORE_PREFIX,
      live,
      // Reused, never reimplemented: posting the assertion back to Steam is the single step that
      // makes sign-in safe, and a second copy of it is one too many.
      verifyWithSteam: auth()._internals.verifyWithSteam,
    });
  }
  return adminRouter;
}

// The live competitive service (presence, queue, match formation). Built lazily for the
// same reason as auth(): it closes over helpers defined above it.
let liveRouter = null;
function expectedCompetitiveRules() {
  try {
    const catalogue = JSON.parse(fs.readFileSync(CATALOGUE_JSON_PATH, 'utf8'));
    const entry = catalogue.gamemodes.find(e => e.id === liveModule.GATED_MODE_ID);
    const rules = { ...entry.rules, ...(rulesOverride()?.[entry.id] || {}) };
    if (![rules.score_limit, rules.max_rounds].every(n => Number.isInteger(n) && n > 0)) return null;
    return rules;
  } catch { return null; }
}
function live() {
  if (!liveRouter) {
    const persistentStore = process.env.UPSTASH_REDIS_REST_URL &&
      process.env.UPSTASH_REDIS_REST_TOKEN ? upstashCmd : null;
    liveRouter = liveModule.create({
      analytics: analytics(),
      accountDirectory: accountDirectory(),
      whoami: (token) => auth().whoami(token),
      admitGameplay: (token,identity) => auth().admitGameplay(token,identity),
      activityGate: accountActivity,
      bearer: (req) => auth().bearer(req),
      sendJson,
      badRequest,
      readBody,
      // Queue bans have to outlive a redeploy or they are not bans at all, so the penalty
      // store writes through to Upstash. Everything else in live.cjs stays in memory.
      upstashCmd: persistentStore,
      prefix: STORE_PREFIX,
      // Snapshot the effective match rule when the host receives its report capability. Live's
      // built-in default remains authoritative when no deployment override exists.
      rankedRules: effectiveRankedRules,
      // The queue is shut to anybody behind the release we are publishing (live.cjs, the
      // version gate). A match already running is not.
      requiredVersions,
      expectedRules: expectedCompetitiveRules,
      privateSoloSteam:privateAccounts?process.env.COMP_PRIVATE_STEAM_IDS:'',
      // A name for somebody who is not signed in. Reused, never reimplemented, for the same
      // reason verifyWithSteam is: this is auth's day-cached Steam profile lookup, and a second
      // copy of it would be a second cache to keep warm.
      profileOf: (steamId) => auth().profileFor(steamId),
    });
  }
  return liveRouter;
}

async function router(req, res) {
  const method = req.method || 'GET';
  let url;
  try {
    url = new URL(req.url, 'http://internal');
  } catch {
    return badRequest(res);
  }
  let pathname = url.pathname;
  if(recording) {
    res.setHeader('X-Robots-Tag','noindex, nofollow, noarchive');
    res.setHeader('Referrer-Policy','no-referrer');
    const privatePath=recording.publicPath(pathname);
    if(privatePath){pathname=privatePath;url.pathname=privatePath;}
    else if(!pathname.startsWith('/api/')&&!pathname.startsWith('/auth/')&&!pathname.startsWith('/admin')&&!pathname.startsWith('/assets/'))
      return sendJson(res,404,{error:'Not found.'});
  }

  if ((method === 'GET' || method === 'HEAD') && (pathname === '/account' || pathname === '/account.html')) {
    // Browser account POSTs must originate on the one configured HTTPS origin.
    // Redirect website aliases before a user enters any credentials.
    try {
      const canonical = new URL(process.env.HUB_ACCOUNT_ORIGIN);
      if (canonical.protocol === 'https:' && canonical.origin === process.env.HUB_ACCOUNT_ORIGIN &&
          String(req.headers.host || '').toLowerCase() !== canonical.host.toLowerCase()) {
        res.writeHead(302, {location:canonical.origin + '/account', 'cache-control':'no-store', 'referrer-policy':'no-referrer'});
        return res.end();
      }
    } catch {} // Unconfigured accounts still render the page and fail closed at the API.
    res.setHeader('cache-control', 'no-store');
    res.setHeader('referrer-policy', 'no-referrer');
    res.setHeader('x-content-type-options', 'nosniff');
    res.setHeader('content-security-policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'");
    return handleHtmlPage(req, res, path.join(PUBLIC_DIR, 'account.html'));
  }

  if (method === 'POST' && pathname === '/api/telemetry') return handleTelemetry(req,res);

  // Explicit allowlist: legal pages use the same rendering and HEAD behavior as the site.
  const legalPage = /^\/(privacy|terms|cookies|notices)(?:\.html)?$/.exec(pathname);
  if ((method === 'GET' || method === 'HEAD') && legalPage) {
    return handleHtmlPage(req, res, path.join(PUBLIC_DIR, `${legalPage[1]}.html`));
  }

  if ((method === 'GET' || method === 'HEAD') && (pathname === '/' || pathname === '/index.html')) {
    return handleIndex(req, res);
  }

  if ((method === 'GET' || method === 'HEAD') && (pathname === '/about' || pathname === '/about.html')) {
    return handleAbout(req, res);
  }

  if ((method === 'GET' || method === 'HEAD') && (pathname === '/ranked' || pathname === '/ranked.html')) {
    return handleRanked(req, res);
  }

  if ((method === 'GET' || method === 'HEAD') && (pathname === '/how' || pathname === '/how.html')) {
    return handleHow(req, res);
  }

  if ((method === 'GET' || method === 'HEAD') && (pathname === '/faq' || pathname === '/faq.html')) {
    return handleFaq(req, res);
  }

  if ((method === 'GET' || method === 'HEAD') && pathname === '/catalogue.json') {
    return handleCatalogueJson(req, res);
  }

  if ((method === 'GET' || method === 'HEAD') && pathname === '/hub/download') {
    return handleHubDownload(req, res);
  }

  if ((method === 'GET' || method === 'HEAD') && pathname.startsWith('/hub/')) {
    const rawSegment = pathname.slice('/hub/'.length);
    return serveStaticFile(req, res, HUB_DIR, rawSegment);
  }

  if ((method === 'GET' || method === 'HEAD') && pathname.startsWith('/packs/')) {
    const rawSegment = pathname.slice('/packs/'.length);
    return serveStaticFile(req, res, PACKS_DIR, rawSegment);
  }

  if ((method === 'GET' || method === 'HEAD') && pathname.startsWith('/assets/')) {
    const rawSegment = pathname.slice('/assets/'.length);
    return serveStaticFile(req, res, ASSETS_DIR, rawSegment);
  }

  if ((method === 'GET' || method === 'HEAD') && pathname === '/api/health') {
    res.setHeader('cache-control', 'no-store');
    return handleHealth(req, res);
  }

  if (method === 'POST' && pathname === '/api/event') {
    res.setHeader('cache-control', 'no-store');
    return handleApiEvent(req, res);
  }

  // Match-world evidence is accepted only with the host's per-match capability. Check the team
  // ruling suffix first so the plain report route cannot swallow it.
  if (method === 'POST' && pathname === '/api/match-report/team') {
    res.setHeader('cache-control', 'no-store');
    return handleMatchReportTeam(req, res);
  }
  if (method === 'POST' && pathname === '/api/match-report/batch') {
    res.setHeader('cache-control', 'no-store');
    return handleCombatBatch(req, res);
  }
  if (method === 'POST' && pathname === '/api/match-report') {
    res.setHeader('cache-control', 'no-store');
    return handleMatchReport(req, res);
  }

  // Native host travel still uses this clock. Retired diagnostic routes return 404.
  if (pathname === '/api/probe/slow') {
    res.setHeader('cache-control', 'no-store');
    return handleProbeSlow(req, res);
  }

  if (pathname === '/api/match-report/start-ready') {
    res.setHeader('cache-control', 'no-store');
    return handleStartReady(req, res);
  }

  if (pathname === '/api/match-report/final') {
    res.setHeader('cache-control', 'no-store');
    return handleFinalSnapshot(req, res);
  }

  if (pathname === '/api/probe') {
    res.setHeader('cache-control', 'no-store');
    return handleProbe(req, res);
  }

  // The admin console (a web page, not an API): /admin*
  if (pathname === '/admin' || pathname.startsWith('/admin/')) {
    if (await admin().route(req, res, method, pathname, url)) return;
  }

  // Steam sign-in and independent Lights Out accounts: /api/auth/*, /auth/steam/*
  if (pathname.startsWith('/api/auth/') || pathname.startsWith('/auth/steam/')) {
    res.setHeader('cache-control', 'no-store');
    if (await auth().route(req, res, method, pathname, url)) return;
  }

  // Live competitive: the SSE stream, the queue, match accept, parties, friends, the leaderboard
  // and reports.
  //
  // ASK THE MODULE, never a copy of its route list. This used to be a hand-written prefix test
  // (/api/live*, /api/queue/*, /api/match/*, /api/party/*) and every route added to live.cjs
  // outside those four prefixes was unreachable: /api/friends/* answered 404 "Not found." for a
  // fully built friends tab, and so did /api/leaderboard and /api/report. live.cjs owns the list
  // (its NEEDS_AUTH), so a new route there is routable here the moment it is added.
  if (pathname.startsWith('/api/') && liveModule.owns(pathname)) {
    if (await live().route(req, res, method, pathname, url)) return;
  }

  if (pathname.startsWith('/api/')) {
    res.setHeader('cache-control', 'no-store');
  }
  if (pathname === '/api/match-report/migration') {
    res.setHeader('cache-control','no-store');return handleHostMigration(req,res);
  }

  return notFound(res);
}

function requestListener(req, res) {
  const started=performance.now(), requestId=require('node:crypto').randomUUID();
  res.setHeader('x-request-id',requestId);
  res.on('finish',()=>{
    const route=String(req.url||'').split('?')[0];
    if (route==='/api/telemetry'||!route.startsWith('/api/')||/probe|live|network/.test(route)&&res.statusCode<400)return;
    const duration=performance.now()-started;
    const action=/^\/api\/[a-z_]+(?:\/[a-z_]+){0,2}$/.test(route)?route.replaceAll('/','.').slice(1):'api.other';
    analytics().emit('request.outcome',{action,status:res.statusCode,duration_ms:Math.round(duration),method:req.method},{actor_id:req.analyticsActor,match_id:req.analyticsMatch,request_id:requestId,severity:res.statusCode>=500?'error':res.statusCode>=400?'warn':'info'});
  });
  Promise.resolve()
    .then(() => router(req, res))
    .catch((err) => {
      console.error('[server] unhandled:', err);
      if (!res.headersSent) {
        sendJson(res, 500, { error: 'Server error.' });
      } else {
        res.destroy();
      }
    });
}

// ---------------------------------------------------------------------------
// Server bootstrap
// ---------------------------------------------------------------------------

function createServer(options = {}) {
  // Isolated HTTP contract tests inject a minimal live service. Production calls createServer()
  // without options; keeping the seam here exercises the real router/body/auth code over TCP.
  if (options.liveService) liveRouter = options.liveService;
  if (options.analyticsService) analyticsService = options.analyticsService;
  const server = http.createServer(requestListener);
  server.keepAliveTimeout = 120_000;
  server.headersTimeout = 125_000;
  server.requestTimeout = 300_000;
  return server;
}

function start() {
  if(recording)recordingModule.validateDeployment();
  if (process.env.HUB_TEST_TOKENS) {
    // Loud on purpose: this variable lets anyone who can set it mint an identity. It exists
    // for the test suite and must never be set on Railway.
    console.error('[server] WARNING: HUB_TEST_TOKENS is set — enabled only under NODE_ENV=test. '
                  + 'Unset it unless this is a test run.');
  }
  const server = createServer();
  analytics().emit('service.start',{code:'server_started'});
  void analytics().maintenance();
  accountDirectory();
  const port = process.env.PORT ?? 8081;
  // The rules build numbers, before the first catalogue goes out if we can manage it. Nothing
  // waits on it: until it lands, versions are served plain, which is always safe.
  loadRulesBuilds().catch(() => { rulesBuilds = rulesBuilds || new Map(); });
  server.listen(port, () => {
    const address = server.address();
    const actualPort = typeof address === 'object' && address ? address.port : port;
    console.log(`[server] listening on port ${actualPort}`);
  });
  installShutdown(server);
  return server;
}

/**
 * LEAVE TIDILY, BECAUSE A REDEPLOY IS NOT A CRASH.
 *
 * Railway sends SIGTERM when a push takes over, and until now nothing listened: the process was
 * simply killed, taking every live match in its memory with it. Two things have to happen in that
 * window, and they have to happen in this order:
 *
 *   1. live.shutdown() writes the last few seconds of every live match through to Upstash. The
 *      container that replaces this one reads them back and re-arms their clocks, so from a
 *      player's side a redeploy is a slightly long reconnect rather than a match that vanished.
 *   2. ...and only then does it stop this process's own timers and drop its streams. Stopping
 *      them first would be worse than not stopping them at all: both containers would be running
 *      the same match's clocks, and a no-show could be judged - and paid for - twice.
 *
 * Then the listener closes and we exit. The hard deadline is the belt to that: whatever goes
 * wrong, this process must not be the reason the deploy hangs.
 */
function installShutdown(server) {
  let going = false;
  const bye = (signal) => {
    if (going) return;
    going = true;
    console.log(`[server] ${signal}: saving live matches and shutting down`);
    // Nothing may outlive this, however badly the save goes.
    const hard = setTimeout(() => process.exit(0), 8000);
    if (typeof hard.unref === 'function') hard.unref();
    Promise.resolve()
      .then(() => live().shutdown())
      .then(() => analytics().close())
      .then(() => accountDirectoryService?.close())
      .catch((err) => console.error('[server] shutdown:', err && err.message))
      .then(() => new Promise((resolve) => server.close(resolve)))
      .then(() => { clearTimeout(hard); process.exit(0); })
      .catch(() => process.exit(0));
  };
  process.on('SIGTERM', () => bye('SIGTERM'));
  process.on('SIGINT', () => bye('SIGINT'));
}

if (require.main === module) {
  start();
}

module.exports = { createServer, start,
  _internals: { describeHeaders, dispatchRankedReport, applyRestoredLegacyReport, effectiveRankedRules } };
