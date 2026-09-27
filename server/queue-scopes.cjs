'use strict';
// Strict queue identities. Callers supply authoritative players, streams and parties.
const crypto = require('node:crypto');
const FIELDS = ['queue_actor', 'queue_context', 'queue_attempt', 'queue_unit'];
const NONCE = /^[A-Za-z0-9_-]{16,128}$/;
const own = (o, k) => Object.prototype.hasOwnProperty.call(o, k);
const fail = (code, status = 409) => ({ok:false, kind:'rejected', status, code});
const emptyScope = () => ({queue_actor:'', queue_context:'', queue_unit:''});

function partyStamp(party) {
  if (!party || !Array.isArray(party.members)) throw TypeError('Missing authoritative party');
  const members = [...party.members].sort();
  if (!members.length || new Set(members).size !== members.length
      || members.some(id => typeof id !== 'string' || !id)
      || !members.includes(party.leader)) throw TypeError('Invalid authoritative party');
  const code = party.code || '';
  if (typeof code !== 'string') throw TypeError('Invalid party code');
  const contexts = members.map(id => {
    const context = party.contexts?.[id];
    if (typeof context !== 'string' || !context) throw TypeError('Missing party context');
    return [id, context];
  });
  return {members, code, leader:party.leader,
          stamp:JSON.stringify([code, party.leader, contexts])};
}

/* One instance per ranked engine. claim -> actual enqueue -> bind is synchronous,
 * before matchmaking. Every enqueue/removal, including legacy/requeue, is tracked.
 */
class QueueScopes {
  constructor({mode, isCurrent, nonce} = {}) {
    if (typeof mode !== 'string' || !mode || typeof isCurrent !== 'function')
      throw TypeError('QueueScopes requires mode and isCurrent');
    this.mode = mode;
    this.isCurrent = isCurrent;
    this.nonce = nonce || (() => crypto.randomBytes(24).toString('hex'));
    this.players = new Map();
    this.pending = new Map();
    this.units = new Map();
    this.unitRecords = new WeakMap();
    this.tickets = new WeakMap();
  }
  _nonce() {
    const n = this.nonce();
    if (typeof n !== 'string' || !NONCE.test(n)) throw TypeError('Invalid queue nonce');
    return n;
  }
  _row(player) {
    let row = this.players.get(player);
    if (!row) {
      row = {context:this._nonce(), actor:null};
      this.players.set(player, row);
    }
    return row;
  }
  _online(actor) {
    if (!actor) return false;
    try {
      return this.isCurrent({player:actor.player, token:actor.token, clientId:actor.clientId}) === true;
    } catch { return false; }
  }
  _finish(ticket, status) {
    const r = this.tickets.get(ticket);
    if (!r) return false;
    this.tickets.delete(ticket);
    for (const id of r.party.members)
      if (this.pending.get(id) === ticket) this.pending.delete(id);
    if (r.actor.ticket === ticket) {
      r.actor.ticket = null;
      r.actor.status = status;
    }
    return true;
  }
  _collect(ids) {
    for (const id of ids) {
      const r = this.players.get(id);
      if (r && !r.actor && !this.units.has(id) && !this.pending.has(id)) this.players.delete(id);
    }
  }
  invalidate(ids) {
    const members = [...new Set(ids)];
    const tickets = new Set(members.map(id => this.pending.get(id)).filter(Boolean));
    for (const ticket of tickets) this._finish(ticket, 'retired');
    for (const id of members) this._row(id).context = this._nonce();
    this._collect(members);
  }
  open(player, token, clientId) {
    if (![player, token, clientId].every(v => typeof v === 'string' && v))
      throw TypeError('Invalid authoritative stream identity');
    this.invalidate([player]);
    this._row(player).actor = {
      id:this._nonce(), player, token, clientId,
      high:0, status:'idle', ticket:null, cancelled:null,
    };
    return this.forStream(player, token, clientId);
  }
  close(player, token, clientId) {
    const row = this.players.get(player), actor = row?.actor;
    if (!actor || actor.token !== token || actor.clientId !== clientId) return false;
    this.invalidate([player]);
    row.actor = null;
    this._collect([player]);
    return true;
  }
  // Internal only: never broadcast a player's current actor to older streams.
  view(player) {
    const row = this._row(player);
    return {queue_actor:row.actor?.id || '', queue_context:row.context,
            queue_unit:this.units.get(player)?.nonce || ''};
  }
  forStream(player, token, clientId) {
    const actor = this.players.get(player)?.actor;
    if (!actor || actor.token !== token || actor.clientId !== clientId || !this._online(actor))
      return emptyScope();
    return this.view(player);
  }
  _request(player, token, body) {
    const scoped = body && typeof body === 'object' && FIELDS.some(k => own(body, k));
    if (!scoped) return {ok:true, kind:'legacy'};
    if (Array.isArray(body)
        || typeof body.queue_actor !== 'string' || !NONCE.test(body.queue_actor)
        || typeof body.queue_context !== 'string' || !NONCE.test(body.queue_context)
        || !Number.isSafeInteger(body.queue_attempt) || body.queue_attempt < 1
        || (own(body, 'queue_unit') && (typeof body.queue_unit !== 'string'
            || (body.queue_unit !== '' && !NONCE.test(body.queue_unit)))))
      return fail('queue_scope_invalid', 400);
    const row = this.players.get(player), actor = row?.actor;
    if (!actor || actor.id !== body.queue_actor || actor.token !== token || !this._online(actor))
      return fail('queue_actor_stale');
    return {ok:true, kind:'strict', row, actor, attempt:body.queue_attempt,
            context:body.queue_context, unit:body.queue_unit || ''};
  }
  begin(player, token, body, party) {
    const r = this._request(player, token, body);
    if (!r.ok || r.kind === 'legacy') return r;
    const {row, actor, attempt, context} = r;
    if (context !== row.context) return fail('queue_context_stale');
    if (r.unit) return fail('queue_join_unit_invalid', 400);
    if (attempt < actor.high) return fail('queue_attempt_stale');
    const current = partyStamp(party);
    if (current.leader !== player || !current.members.includes(player)) return fail('queue_not_leader');
    if (attempt === actor.high) {
      if (actor.status === 'pending' || actor.status === 'admitting') return fail('queue_admission_pending');
      if (actor.status === 'joined') {
        const unit = this.units.get(player);
        if (unit && unit.owner?.actor === actor.id && unit.owner.attempt === attempt
            && unit.partyStamp === current.stamp)
          return {ok:true, kind:'joined', unit:unit.unit, queue_unit:unit.nonce};
        return fail('queue_attempt_retired');
      }
      if (actor.status !== 'retryable') return fail('queue_attempt_retired');
    }
    if (current.members.some(id => this.units.has(id))) return fail('queue_already_joined');
    if (current.members.some(id => {
      const p = this.pending.get(id);
      return p && p !== actor.ticket;
    })) return fail('queue_admission_pending');
    const cohort = current.members.map(id => {
      const member = this.players.get(id);
      return {id, context:member?.context, actor:member?.actor};
    });
    if (cohort.some(m => !m.actor || !this._online(m.actor))) return fail('queue_member_stream_stale');
    if (actor.ticket) this._finish(actor.ticket, 'retired');
    const ticket = Object.freeze({});
    Object.assign(actor, {high:attempt, status:'pending', ticket, cancelled:null});
    this.tickets.set(ticket, {actor, attempt, context, party:current, cohort, stage:'pending'});
    for (const id of current.members) this.pending.set(id, ticket);
    return {ok:true, kind:'pending', ticket};
  }
  _valid(ticket, party, stage, allowedUnit = null) {
    const r = this.tickets.get(ticket);
    if (!r || r.stage !== stage) return false;
    const actor = r.actor, row = this.players.get(actor.player);
    if (row?.actor !== actor || row.context !== r.context || actor.ticket !== ticket
        || actor.high !== r.attempt || !this._online(actor)
        || partyStamp(party).stamp !== r.party.stamp) return false;
    return r.cohort.every(m => {
      const current = this.players.get(m.id), unit = this.units.get(m.id);
      return current?.actor === m.actor && current.context === m.context
        && this._online(m.actor) && this.pending.get(m.id) === ticket
        && (!unit || unit.unit === allowedUnit);
    });
  }
  current(ticket, party) { return this._valid(ticket, party, 'pending'); }
  claim(ticket, party) {
    if (!this._valid(ticket, party, 'pending')) return false;
    const r = this.tickets.get(ticket);
    r.stage = 'admitting';
    r.actor.status = 'admitting';
    return true;
  }
  track(unit) {
    const existing = this.unitRecords.get(unit);
    if (existing) return existing.nonce;
    if (!unit || !Array.isArray(unit.members) || !unit.members.length) throw TypeError('Invalid queue unit');
    const members = [...unit.members].sort();
    if (new Set(members).size !== members.length
        || members.some(id => typeof id !== 'string' || !id || this.units.has(id)))
      throw Error('Queue unit overlaps an existing unit');
    const r = {unit, members, code:unit.code || '', nonce:this._nonce(), owner:null, partyStamp:null};
    this.unitRecords.set(unit, r);
    for (const id of members) { this._row(id); this.units.set(id, r); }
    return r.nonce;
  }
  bind(ticket, unit, party) {
    if (!unit || !Array.isArray(unit.members) || !this._valid(ticket, party, 'admitting', unit)) return false;
    const r = this.tickets.get(ticket), members = [...unit.members].sort();
    if ((unit.code || '') !== r.party.code || JSON.stringify(members) !== JSON.stringify(r.party.members)) return false;
    this.track(unit);
    const queued = this.unitRecords.get(unit);
    if (queued.owner) return false;
    queued.owner = {actor:r.actor.id, attempt:r.attempt};
    queued.partyStamp = r.party.stamp;
    this._finish(ticket, 'joined');
    return true;
  }
  // Only before enqueue, or after rolling back the exact unit this worker created.
  release(ticket) { return this._finish(ticket, 'retryable'); }
  abort(ticket) { return this._finish(ticket, 'retired'); }
  leave(player, token, body, party) {
    const r = this._request(player, token, body);
    if (!r.ok || r.kind === 'legacy') return r;
    const {row, actor, attempt, context} = r, prior = actor.cancelled;
    if (prior && attempt === actor.high && prior.attempt === attempt
        && prior.context === context && prior.unit === r.unit)
      return {ok:true, kind:'cancelled', duplicate:true, unit:null, members:[]};
    if (context !== row.context) return fail('queue_context_stale');
    if (attempt < actor.high) return fail('queue_attempt_stale');
    const current = partyStamp(party);
    if (!current.members.includes(player)) return fail('queue_party_stale');
    const queued = this.units.get(player);
    if (queued) {
      const exact = r.unit && r.unit === queued.nonce;
      const ownPending = !r.unit && queued.owner?.actor === actor.id && queued.owner.attempt === attempt;
      if (!exact && !ownPending) return fail('queue_unit_stale');
    } else if (r.unit) return fail('queue_unit_retired');
    const pending = this.pending.get(player);
    const pendingRecord = pending && this.tickets.get(pending);
    if (!queued && pendingRecord && pendingRecord.party.stamp !== current.stamp) return fail('queue_party_stale');
    const members = queued ? queued.members : current.members;
    this.invalidate(members);
    Object.assign(actor, {
      high:Math.max(actor.high, attempt), status:'cancelled', ticket:null,
      cancelled:{attempt, context, unit:r.unit},
    });
    return {ok:true, kind:'cancelled', duplicate:false, unit:queued?.unit || null, members:[...members]};
  }
  retire(unit) {
    const r = this.unitRecords.get(unit);
    if (!r) return false;
    this.unitRecords.delete(unit);
    for (const id of r.members) if (this.units.get(id) === r) this.units.delete(id);
    if (r.owner) for (const id of r.members) {
      const actor = this.players.get(id)?.actor;
      if (actor?.id === r.owner.actor && actor.high === r.owner.attempt) actor.status = 'retired';
    }
    this.invalidate(r.members);
    return true;
  }
}
module.exports = {QueueScopes};
