/**
 * Steam sign-in for Lights Out (2026-09-14).
 *
 * Sam's decision C1: Competitive identity is the Steam account, because the rank should
 * belong to the account that actually plays. Steam OpenID 2.0 gives us one thing — a
 * verified SteamID64 — and nothing else: no email, no password, no friends list.
 *
 * The hub is a desktop program, so it cannot receive a browser redirect. Rather than open
 * a local HTTP listener requiring firewall configuration, sign-in is a LINK CODE handshake:
 *
 *   1. hub  -> GET  /api/auth/start            -> { code, url, expires_in }
 *   2. hub opens `url` in the player's browser -> Steam login -> /auth/steam/return
 *   3. server verifies the assertion WITH STEAM, binds the SteamID64 to the code
 *   4. hub  -> GET  /api/auth/poll?code=...    -> { status: "pending" | "ready", ... }
 *   5. hub stores the token; /api/auth/me exchanges it for the profile
 *
 * Security notes, because this is the front door:
 *   - An OpenID response is NEVER trusted as it arrives. Every parameter is posted back to
 *     Steam with openid.mode=check_authentication and we look for `is_valid:true`. Without
 *     that step anyone could hand us any SteamID64 they liked.
 *   - `openid.claimed_id` must match Steam's own identity URL shape exactly.
 *   - return_to must match what we sent, or Steam's own check fails.
 *   - Link codes are single use and expire; tokens are opaque random bytes.
 *   - The persona name and avatar are a nice-to-have from the Steam Web API and need
 *     STEAM_WEB_API_KEY. Everything works without it; the player is just shown their ID.
 */
const crypto = require('crypto');
const accountsModule = require('./accounts.cjs');
const ownership = require('./account-ownership.cjs');
const { createOriginResolver } = require('./public-origin.cjs');

const STEAM_OPENID = 'https://steamcommunity.com/openid/login';
const CLAIMED_ID_RE = /^https?:\/\/steamcommunity\.com\/openid\/id\/(\d{17})$/;

const LINK_TTL_SECONDS = 15 * 60;          // long enough to find your Steam password
const TOKEN_TTL_SECONDS = 90 * 24 * 3600;  // 90 days, refreshed on every /api/auth/me
const PROFILE_TTL_SECONDS = 24 * 3600;     // persona names change; re-fetch daily

/** Fallback store so the whole flow works locally with no Upstash (and in tests). */
const memory = new Map();

function makeStore(upstashCmd, prefix) {
  const key = (name) => `${prefix}${name}`;

  return {
    async set(name, value, ttlSeconds) {
      const raw = JSON.stringify(value);
      const stored = await upstashCmd(['SET', key(name), raw, 'EX', String(ttlSeconds)]);
      if (stored === null) {
        memory.set(key(name), { raw, expires: Date.now() + ttlSeconds * 1000 });
      }
    },
    async get(name) {
      const raw = await upstashCmd(['GET', key(name)]);
      if (raw !== null && raw !== undefined) {
        try { return JSON.parse(raw); } catch { return null; }
      }
      const hit = memory.get(key(name));
      if (!hit) return null;
      if (hit.expires < Date.now()) { memory.delete(key(name)); return null; }
      try { return JSON.parse(hit.raw); } catch { return null; }
    },
    async del(name) {
      await upstashCmd(['DEL', key(name)]);
      memory.delete(key(name));
    },
  };
}

function randomToken(bytes = 32) {
  return crypto.randomBytes(bytes).toString('base64url');
}

/** Short, unambiguous, and safe in a URL. Not a secret on its own — it only ever names a
 *  pending sign-in, and the token that comes back is what authenticates. */
function randomCode() {
  const alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';
  let out = '';
  const bytes = crypto.randomBytes(12);
  for (let i = 0; i < 12; i += 1) out += alphabet[bytes[i] % alphabet.length];
  return out;
}

/**
 * Ask Steam whether the assertion it just sent us is genuine. This is the step that makes
 * the whole thing safe; skipping it would let anyone forge a sign-in.
 */
async function verifyWithSteam(params) {
  const body = new URLSearchParams();
  for (const [name, value] of params) {
    if (name.startsWith('openid.')) body.append(name, value);
  }
  body.set('openid.mode', 'check_authentication');

  const response = await fetch(STEAM_OPENID, {
    method: 'POST',
    headers: { 'content-type': 'application/x-www-form-urlencoded' },
    body: body.toString(),
  });
  if (!response.ok) return false;
  const text = await response.text();
  return /^is_valid:true$/m.test(text.trim());
}

async function fetchProfile(steamId) {
  const key = process.env.STEAM_WEB_API_KEY;
  if (!key) return null;                  // optional: the ID alone is enough to sign in
  try {
    const url = `https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/?key=${encodeURIComponent(key)}&steamids=${encodeURIComponent(steamId)}`;
    const response = await fetch(url);
    if (!response.ok) return null;
    const parsed = await response.json();
    const player = parsed && parsed.response && Array.isArray(parsed.response.players)
      ? parsed.response.players[0] : null;
    if (!player) return null;
    return {
      persona: String(player.personaname || ''),
      avatar: String(player.avatarmedium || player.avatar || ''),
      profile_url: String(player.profileurl || ''),
    };
  } catch {
    return null;                          // never let a Steam hiccup break sign-in
  }
}

function create({ upstashCmd, prefix = 'hub:', accountPrefix=prefix, accountStore=upstashCmd,
                  allowSteamId=()=>true, privateGameplay=false, sendJson, badRequest, verify, accounts, ownershipTransaction,
                  recordSteamSignIn=async()=>{}, onAccountsChanged=()=>{} }) {
  const baseUrlOf = createOriginResolver();
  const store = makeStore(upstashCmd, prefix + 'auth:');
  const accountService = accountsModule.create({upstashCmd:accountStore,prefix:accountPrefix,authPrefix:prefix,sendJson,
    steamIdentity,ownershipTransaction,onAccountsChanged,options:accounts,revokeSteam:token=>store.del(`token:${token}`)});
  // `verify` exists so tests can exercise the handshake without talking to Steam. It is
  // NEVER set in production: server.cjs does not pass it, so the real check always runs.
  const checkAssertion = verify || verifyWithSteam;

  // Ownership is durable authorization state, not optional account enrichment.
  // Read it even when new Lights Out registrations are disabled. An unavailable
  // ledger must never resurrect a disconnected Steam login through legacy data.
  async function steamLedger(steamId) {
    return ownership.readSteam({upstashCmd:accountStore,prefix:accountPrefix},steamId);
  }
  async function resolveSteam(session) {
    if(!allowSteamId(session.steam_id))return null;
    const ledger=await steamLedger(session.steam_id);
    if(privateGameplay&&ledger?.account_id&&!await accountService.bySteam(session.steam_id))return null;
    if((session.steam_generation||0)!==(ledger?.generation||0))return null;
    return {player_id:ledger?.player_id||session.steam_id,steam_generation:ledger?.generation||0,
      account_id:ledger?.account_id||null,auth_method:'steam'};
  }

  async function profileFor(steamId) {
    const cached = await store.get(`user:${steamId}`);
    if (cached) return cached;
    const fetched = await fetchProfile(steamId);
    if (!fetched) {
      // Do NOT cache a blank profile. Without STEAM_WEB_API_KEY (or during a Steam
      // outage) fetchProfile returns null, and caching that for a day would mean adding
      // the key later changed nothing until the TTL expired. Cost of not caching: one
      // failed lookup per sign-in, which is nothing.
      return { persona: '', avatar: '', profile_url: '' };
    }
    await store.set(`user:${steamId}`, fetched, PROFILE_TTL_SECONDS);
    return fetched;
  }

  /** 1. The hub asks for a link code and the URL to open. */
  async function handleStart(req, res) {
    const code = randomCode();
    await store.set(`link:${code}`, { status: 'pending', created: Date.now() }, LINK_TTL_SECONDS);
    sendJson(res, 200, {
      ok: true,
      code,
      url: `${baseUrlOf(req)}/auth/steam/start?code=${code}`,
      expires_in: LINK_TTL_SECONDS,
    });
  }

  /** 2. The browser lands here and gets bounced to Steam. */
  async function handleSteamStart(req, res, url) {
    const code = String(url.searchParams.get('code') || '');
    const pending = code ? await store.get(`link:${code}`) : null;
    if (!pending || pending.status !== 'pending') {
      return sendPage(res, 400, 'Sign-in link expired',
        'Go back to Lights Out and press Sign in with Steam again.');
    }

    const base = baseUrlOf(req);
    const params = new URLSearchParams({
      'openid.ns': 'http://specs.openid.net/auth/2.0',
      'openid.mode': 'checkid_setup',
      'openid.return_to': `${base}/auth/steam/return?code=${encodeURIComponent(code)}`,
      'openid.realm': base,
      'openid.identity': 'http://specs.openid.net/auth/2.0/identifier_select',
      'openid.claimed_id': 'http://specs.openid.net/auth/2.0/identifier_select',
    });
    res.writeHead(302, { location: `${STEAM_OPENID}?${params.toString()}`, 'cache-control': 'no-store' });
    res.end();
  }

  /** 3. Steam sends the player back here. Verify, then bind the ID to the code. */
  async function handleSteamReturn(req, res, url) {
    const code = String(url.searchParams.get('code') || '');
    const pending = code ? await store.get(`link:${code}`) : null;
    if (!pending || pending.status !== 'pending') {
      return sendPage(res, 400, 'Sign-in link expired',
        'Go back to Lights Out and press Sign in with Steam again.');
    }

    const claimed = String(url.searchParams.get('openid.claimed_id') || '');
    const match = CLAIMED_ID_RE.exec(claimed);
    if (!match) {
      return sendPage(res, 400, 'Sign-in failed', 'Steam did not return a valid account id.');
    }

    const valid = await checkAssertion(url.searchParams);
    if (!valid) {
      // Either a forged assertion or a Steam outage; both mean we must not sign anyone in.
      return sendPage(res, 400, 'Sign-in could not be verified',
        'Steam did not confirm that sign-in. Please try again.');
    }

    const steamId = match[1];
    if(!allowSteamId(steamId)) {
      await store.set(`link:${code}`,{status:'denied'},LINK_TTL_SECONDS);
      return sendPage(res,403,'Private test','This account is not invited to the private test.');
    }
    const token = randomToken();
    const ledger=await steamLedger(steamId);
    await recordSteamSignIn(steamId);
    await store.set(`token:${token}`, { steam_id: steamId, created: Date.now(),steam_generation:ledger?.generation||0 }, TOKEN_TTL_SECONDS);
    await store.set(`link:${code}`, { status: 'ready', steam_id: steamId, token }, LINK_TTL_SECONDS);

    const profile = await profileFor(steamId);
    sendPage(res, 200, 'Signed in',
      `${profile.persona ? profile.persona + ', you' : 'You'} are signed in. You can close this tab and go back to Lights Out.`);
  }

  /** 4. The hub polls until the browser half is done. The code is single use. */
  async function handlePoll(req, res, url) {
    const code = String(url.searchParams.get('code') || '');
    if (!code) return badRequest(res, 'Missing code.');
    const pending = await store.get(`link:${code}`);
    if (!pending) return sendJson(res, 200, { ok: true, status: 'expired' });
    if(pending.status==='denied')return sendJson(res,200,{ok:false,status:'denied'});
    if (pending.status !== 'ready') return sendJson(res, 200, { ok: true, status: 'pending' });

    await store.del(`link:${code}`);       // single use: the token has taken over
    const identity=await steamIdentity(pending.token);
    if(!identity)return sendJson(res,401,{ok:false,error:'Sign in again. This account association has changed.'});
    const profile = await profileFor(pending.steam_id);
    let linked=null;
    if(accountService.ready && identity.account_id) {
      try {
        const candidate=await accountService.bySteam(pending.steam_id);
        if(candidate && candidate.id===identity.account_id && candidate.player_id===identity.player_id &&
           candidate.steam_id===pending.steam_id)linked=accountService.summary(candidate);
      } catch { /* Optional display enrichment does not change authenticated ownership. */ }
    }
    sendJson(res, 200, {
      ok: true, status: 'ready', token: pending.token, steam_id: pending.steam_id,
      player_id:identity.player_id||pending.steam_id,game_steam_id:pending.steam_id,auth_method:'steam',...profile,
      ...(identity.account_id?{account_id:identity.account_id}:{}),...(linked?{account:linked}:{}),
    });
  }

  function bearer(req) {
    const header = String(req.headers.authorization || '');
    return header.startsWith('Bearer ') ? header.slice(7).trim() : '';
  }

  /** 5. Who is this token? Also refreshes the token's lifetime. */
  async function handleMe(req, res) {
    const token = bearer(req);
    if(accountsModule.isSession(token)) {
      return accountService.route(req,res,'GET','/api/auth/account/me');
    }
    if (!token) return sendJson(res, 401, { ok: false, error: 'Not signed in.' });
    const session = await store.get(`token:${token}`);
    if (!session) return sendJson(res, 401, { ok: false, error: 'Signed out or expired.' });
    const identity=await resolveSteam(session);
    if(!identity)return sendJson(res,401,{ok:false,error:'Sign in again. This account association has changed.'});
    await recordSteamSignIn(session.steam_id);
    await store.set(`token:${token}`, session, TOKEN_TTL_SECONDS);
    const profile = await profileFor(session.steam_id);
    let linked=null;
    if(accountService.ready) {
      try {
        if(identity.account_id)linked=await accountService.bySteam(session.steam_id);
        if(linked&&(linked.id!==identity.account_id||linked.player_id!==identity.player_id||linked.steam_id!==session.steam_id))
          throw Error('Ownership changed');
      }
      // Optional account enrichment must not take down independent Steam login.
      catch {linked=null;}
    }
    sendJson(res, 200, { ok: true, steam_id: session.steam_id, player_id:identity.player_id,...profile,
      ...(linked?{account_id:linked.id,account:accountService.summary(linked),auth_method:'steam'}:{}) });
  }

  async function handleSignOut(req, res) {
    const token = bearer(req);
    if(accountsModule.isSession(token)) {
      try { await accountService.signout(token);sendJson(res,200,{ok:true}); }
      catch { sendJson(res,503,{ok:false,error:'Account service is temporarily unavailable.'}); }
      return;
    }
    if (token) await store.del(`token:${token}`);
    sendJson(res, 200, { ok: true });
  }

  function sendPage(res, status, title, message) {
    const esc = (s) => String(s).replace(/[&<>"']/g, (c) => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    const html = `<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${esc(title)} — Lights Out</title>
<style>
 :root { color-scheme: light dark; }
 body { margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
        font:16px/1.6 system-ui,sans-serif; padding:24px; text-align:center; }
 main { max-width:420px; }
 h1 { font-size:20px; margin:0 0 8px; }
 p { color:#777; margin:0; }
</style></head>
<body><main><h1>${esc(title)}</h1><p>${esc(message)}</p></main></body></html>`;
    const body = Buffer.from(html, 'utf8');
    res.writeHead(status, {
      'content-type': 'text/html; charset=utf-8',
      'content-length': body.length,
      'cache-control': 'no-store',
    });
    res.end(body);
  }

  /** Returns true when it handled the request. */
  async function route(req, res, method, pathname, url) {
    if(await accountService.route(req,res,method,pathname))return true;
    if (method === 'GET' && pathname === '/api/auth/start') { await handleStart(req, res); return true; }
    if (method === 'GET' && pathname === '/auth/steam/start') { await handleSteamStart(req, res, url); return true; }
    if (method === 'GET' && pathname === '/auth/steam/return') {
      try {await handleSteamReturn(req, res, url);}catch {sendJson(res,503,{ok:false,error:'Sign-in is temporarily unavailable.'});}
      return true;
    }
    if (method === 'GET' && pathname === '/api/auth/poll') { await handlePoll(req, res, url); return true; }
    if (method === 'GET' && pathname === '/api/auth/me') {
      try {await handleMe(req, res);}catch {sendJson(res,503,{ok:false,error:'Sign-in is temporarily unavailable.'});}
      return true;
    }
    if (method === 'POST' && pathname === '/api/auth/signout') { await handleSignOut(req, res); return true; }
    return false;
  }

  /** Who is this bearer token? Used by the live service to authenticate an SSE stream or a
   *  POST. Returns {steam_id, persona, avatar} or null — never throws, never refreshes. */
  async function steamIdentity(token) {
    if (!token) return null;
    if(accountsModule.isSession(token))return null;
    // TEST SEAM. HUB_TEST_TOKENS="tok=steamid,tok2=steamid2" lets the test suite drive the
    // queue and match flow without a real Steam round trip. Disabled outside explicit tests.
    const seam = process.env.NODE_ENV === 'test' && process.env.HUB_TEST_TOKENS;
    if (seam) {
      for (const pair of seam.split(',')) {
        const [name, steamId] = pair.split('=');
        if (name && name.trim() === String(token) && steamId && allowSteamId(steamId.trim())) {
          return { steam_id: steamId.trim(),auth_method:'steam', persona: 'Test ' + steamId.trim().slice(-2), avatar: '' };
        }
      }
    }
    const session = await store.get(`token:${String(token)}`);
    if (!session || !session.steam_id) return null;
    const identity=await resolveSteam(session);
    if(!identity)return null;
    await recordSteamSignIn(session.steam_id);
    const profile = await profileFor(session.steam_id);
    return { steam_id: session.steam_id, ...identity,authenticated_at:session.created,...profile };
  }

  // Both credentials resolve to a stable owner plus a separately verified
  // game identity. A Lights Out parent login cannot authorize native gameplay.
  async function whoami(token) {
    try {
      if(accountsModule.isGameSession(token)) return await accountService.gameIdentity(token);
      if(accountsModule.isSession(token)) return null;
      const identity=await steamIdentity(token);
      return identity ? {...identity,player_id:identity.player_id||identity.steam_id,
        game_steam_id:identity.steam_id} : null;
    } catch {return null;}
  }

  // `profileFor` is exported because live.cjs needs a name for somebody who is not signed in
  // right now - a leaderboard row, an offline friend - and this is the lookup that already knows
  // one: Steam's GetPlayerSummaries behind a day-long cache. server.cjs has passed it in as
  // `profileOf` since the bug-report release; until it was exported here that was a seam wired to
  // `undefined`, and live.cjs quietly never called it.
  async function admitGameplay(token, expected) {
    if(privateGameplay) {
      if(accountsModule.isGameSession(token))return accountService.markUsed(token,expected,true);
      const current=await whoami(token);
      return Boolean(current&&current.player_id===expected.player_id&&current.game_steam_id===expected.game_steam_id);
    }
    if(accountsModule.isGameSession(token))return accountService.markUsed(token,expected);
    if(accountsModule.isSession(token))return false;
    if(process.env.NODE_ENV==='test'&&process.env.HUB_TEST_TOKENS)return true;
    return ownership.markSteamUsed({upstashCmd,prefix:accountPrefix,authPrefix:prefix},token,expected);
  }
  return { route, whoami, bearer, profileFor, admitGameplay,
           _internals: { verifyWithSteam, randomCode, randomToken, CLAIMED_ID_RE } };
}

module.exports = { create, CLAIMED_ID_RE, randomCode, randomToken };
