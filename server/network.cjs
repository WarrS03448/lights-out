/** Pre-match network policy. Values are relay RTT measurements, not game RTT promises. */
const crypto = require('node:crypto');
const TRANSPORT = 'webrtc-relay-v1';
const REGIONS = ['NA', 'SA', 'EU', 'AS', 'OC', 'AF', 'ME'];
const positive = (name, fallback) => {
  const n = Number(process.env[name]);
  return Number.isFinite(n) && n > 0 ? n : fallback;
};
const MAX_PING = Math.min(120, positive('COMP_MAX_PING_MS', 120));
const TARGET_PING = Math.min(80, MAX_PING, positive('COMP_TARGET_PING_MS', 80));
const PROFILE_TTL = 120000;
const PING_TTL = 60000;
const MAX_PEERS = 16;
const ATTEMPT_TTL = 45000;
const MAX_ATTEMPTS = 1024;
const enforced = () => !(process.env.NODE_ENV === 'test' && process.env.COMP_NETWORK_TEST_BYPASS === '1');
const fresh = (at, now, ttl) => Number.isFinite(at) && at <= now && now - at <= ttl;
const ready = (p, now) => Boolean(p && p.transport === TRANSPORT && REGIONS.includes(p.region)
  && typeof p.location === 'string' && /^[0-9a-f]{32}$/.test(p.location) && p.revision
  && fresh(p.sampled, now, PROFILE_TTL));
const compatible = (players) => new Set(players.map(p => p.region)).size <= 1
  || players.every(p => p.cross_region === true);

function pairPing(a, b, now) {
  const report = (from, to) => {
    const r = from.pings && from.pings[to.id];
    return r && fresh(r.at, now, PING_TTL) && r.localRevision === from.revision
      && r.remoteRevision === to.revision && Number.isFinite(r.ms) && r.ms >= 0 ? r.ms : null;
  };
  const ab = report(a,b), ba = report(b,a);
  return ab === null || ba === null ? null : Math.max(ab,ba);
}

function selectHost(players, now) {
  if (!players.length || !players.every(p => ready(p,now)) || !compatible(players)) return null;
  let best = null;
  for (const host of players) {
    const pings = {};
    let total = 0, worst = 0, valid = true;
    for (const peer of players) {
      const ping = peer.id === host.id ? 0 : pairPing(host,peer,now);
      if (ping === null || ping > MAX_PING) { valid = false; break; }
      pings[peer.id] = ping; total += ping; worst = Math.max(worst,ping);
    }
    if (!valid) continue;
    const average = total / Math.max(1,players.length - 1);
    if (!best || average < best.average || (average === best.average && (worst < best.worst
        || (worst === best.worst && host.id < best.host)))) {
      best = { host:host.id, average, worst, pings,
        cross_region:new Set(players.map(p => p.region)).size > 1,
        region:host.region, estimated:true };
    }
  }
  return best;
}

class Registry {
  constructor() { this.profiles = new Map(); this.preferences = new Map(); this.attempts = new Map(); }
  player(id) { return this.profiles.get(id); }
  profile(id, body, now = Date.now()) {
    if (!body || typeof body !== 'object' || Array.isArray(body) || !REGIONS.includes(body.region)
        || body.transport !== TRANSPORT
        || typeof body.location !== 'string' || !/^[0-9a-f]{32}$/.test(body.location)
        || typeof body.age_seconds !== 'number' || !Number.isFinite(body.age_seconds)
        || body.age_seconds < 0 || body.age_seconds * 1000 > PROFILE_TTL
        || (body.cross_region !== undefined && typeof body.cross_region !== 'boolean')) {
      throw new Error('Choose your region and wait for a fresh relay measurement.');
    }
    const old = this.player(id);
    const changed = !old || old.location !== body.location || old.region !== body.region
      || old.cross_region !== (body.cross_region === true);
    const p = { id, transport:TRANSPORT, region:body.region, cross_region:body.cross_region === true,
      location:body.location, sampled:now - body.age_seconds * 1000,
      revision:changed ? crypto.randomBytes(12).toString('hex') : old.revision,
      pings:changed ? {} : old.pings };
    if (changed) this.retirePlayer(id);
    this.profiles.set(id,p);
    this.preferences.set(id,{region:p.region,cross_region:p.cross_region});
    return p;
  }
  authorizeAttempt(offerer, answerer, offererRevision, answererRevision, attempt, now = Date.now()) {
    if (!/^[0-9a-f]{32}$/.test(String(attempt || '')) || offerer === answerer
        || !ready(this.player(offerer),now) || !ready(this.player(answerer),now)
        || this.player(offerer).revision !== offererRevision
        || this.player(answerer).revision !== answererRevision
        || !compatible([this.player(offerer),this.player(answerer)])) {
      throw new Error('Invalid relay attempt.');
    }
    const existing = this.attempts.get(attempt);
    if (existing && fresh(existing.created,now,ATTEMPT_TTL)) {
      throw new Error('Relay attempt is already in use.');
    }
    this.attempts.set(attempt,{attempt,offerer,answerer,offererRevision,answererRevision,
      created:now,expires:now + ATTEMPT_TTL,answered:false});
    while (this.attempts.size > MAX_ATTEMPTS) this.attempts.delete(this.attempts.keys().next().value);
    return this.attempts.get(attempt);
  }
  attempt(id, now = Date.now()) {
    const value = this.attempts.get(id);
    if (!value || now >= value.expires) { if (value) this.attempts.delete(id); return null; }
    return value;
  }
  retirePlayer(id) {
    for (const [attempt,value] of this.attempts) {
      if (value.offerer === id || value.answerer === id) this.attempts.delete(attempt);
    }
  }
  authorized(id, peerId, localRevision, remoteRevision, attempt, now = Date.now()) {
    const value = this.attempt(attempt,now);
    if (!value || !value.answered) return false;
    return (value.offerer === id && value.answerer === peerId
      && value.offererRevision === localRevision && value.answererRevision === remoteRevision)
      || (value.answerer === id && value.offerer === peerId
        && value.answererRevision === localRevision && value.offererRevision === remoteRevision);
  }
  report(id, body, now = Date.now()) {
    const p = this.player(id);
    if (!ready(p,now) || !body || body.revision !== p.revision
        || !Array.isArray(body.peers) || body.peers.length > MAX_PEERS) {
      throw new Error('Refresh your connection profile before reporting estimates.');
    }
    // Validate the full request before changing state. Null/string/negative RTTs are not zero.
    if (body.peers.some(r => !r || typeof r.steam_id !== 'string' || typeof r.revision !== 'string'
        || r.transport !== TRANSPORT || !/^[0-9a-f]{32}$/.test(String(r.attempt || ''))
        || typeof r.ping !== 'number' || !Number.isFinite(r.ping) || r.ping <= 0 || r.ping > 10000
        || !Number.isInteger(r.samples) || r.samples < 5 || r.samples > 1000
        || typeof r.age_seconds !== 'number' || !Number.isFinite(r.age_seconds)
        || r.age_seconds < 0 || r.age_seconds > 15)) {
      throw new Error('Invalid peer connection estimate.');
    }
    const accepted = body.peers.map((r) => {
      const peer = this.player(r.steam_id);
      if (r.steam_id === id || !ready(peer,now) || peer.revision !== r.revision
          || !compatible([p,peer])
          || !this.authorized(id,r.steam_id,p.revision,peer.revision,r.attempt,now)) {
        throw new Error('Peer relay attempt is not authorized.');
      }
      return [r.steam_id,{ ms:r.ping,at:now - r.age_seconds * 1000,
        localRevision:p.revision,remoteRevision:peer.revision,transport:TRANSPORT,
        samples:r.samples,attempt:r.attempt }];
    });
    for (const [peerId,value] of accepted) p.pings[peerId] = value;
  }
  prune(now = Date.now()) {
    for (const [id,p] of this.profiles) {
      if (!fresh(p.sampled,now,PROFILE_TTL * 2)) {
        this.profiles.delete(id); this.retirePlayer(id); continue;
      }
      for (const [peer,r] of Object.entries(p.pings)) if (!fresh(r.at,now,PING_TTL)) delete p.pings[peer];
    }
    for (const [id,value] of this.attempts) if (now >= value.expires) this.attempts.delete(id);
  }
}
module.exports = { Registry, REGIONS, MAX_PING, TARGET_PING, PROFILE_TTL, PING_TTL, MAX_PEERS,
  ATTEMPT_TTL, MAX_ATTEMPTS, TRANSPORT, enforced, ready, compatible, pairPing, selectHost };
