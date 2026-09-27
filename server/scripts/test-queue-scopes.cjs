'use strict';
// node --test scripts/test-queue-scopes.cjs
const test = require('node:test');
const assert = require('node:assert/strict');
const {QueueScopes} = require('../queue-scopes.cjs');

function fixture() {
  let sequence = 0;
  const streams = new Map();
  const q = new QueueScopes({
    mode:'BB5', nonce:() => (++sequence).toString(16).padStart(32, '0'),
    isCurrent:({player, token, clientId}) => {
      const s = streams.get(player);
      return !!s && s.token === token && s.clientId === clientId;
    },
  });
  function open(id, token = `token-${id}`, clientId = `stream-${id}`) {
    streams.set(id, {token, clientId});
    return q.open(id, token, clientId);
  }
  for (const id of ['a', 'b', 'c']) open(id);
  const solo = id => ({code:'', leader:id, members:[id], contexts:{[id]:`party-${id}-1`}});
  const party = () => ({code:'ABC', leader:'a', members:['a','b'], contexts:{a:'party-a-1', b:'party-b-1'}});
  const body = (id, attempt, extra = {}) => ({...q.view(id), queue_attempt:attempt, queue_unit:'', ...extra});
  const begin = (id, n, p = solo(id)) => q.begin(id, `token-${id}`, body(id,n), p);
  function admit(start, p) {
    assert.equal(start.kind, 'pending');
    assert.equal(q.claim(start.ticket, p), true);
    const unit = {key:p.code || p.leader, code:p.code, members:[...p.members]};
    q.track(unit);
    assert.equal(q.bind(start.ticket, unit, p), true);
    return unit;
  }
  return {q, streams, open, solo, party, body, begin, admit};
}

test('legacy decisions do not mutate; partial scopes cannot downgrade', () => {
  const {q, solo, body} = fixture(), before = q.view('a');
  assert.deepEqual(q.begin('a','old-token',{},solo('a')), {ok:true,kind:'legacy'});
  assert.deepEqual(q.leave('a','old-token',undefined,solo('a')), {ok:true,kind:'legacy'});
  assert.deepEqual(q.view('a'), before);
  for (const b of [{queue_attempt:1}, body('a',0), body('a',1.5),
                   body('a',Number.MAX_SAFE_INTEGER+1), body('a',1,{queue_unit:null})])
    assert.equal(q.begin('a','token-a',b,solo('a')).status,400);
});
test('actor binds player, token and actual current stream', () => {
  const {q, solo, body, streams} = fixture(), b = body('a',1);
  assert.equal(q.begin('a','token-b',b,solo('a')).ok,false);
  assert.equal(q.begin('b','token-b',b,solo('b')).ok,false);
  streams.delete('a');
  assert.equal(q.begin('a','token-a',b,solo('a')).ok,false);
});
test('same-token older stream never receives newest actor', () => {
  const {q, open, begin, solo, body} = fixture();
  const old = body('a',1), pending = begin('a',1);
  const fresh = open('a','token-a','new-stream');
  assert.notEqual(fresh.queue_actor,old.queue_actor);
  assert.equal(q.forStream('a','token-a','stream-a').queue_actor,'');
  assert.equal(q.forStream('a','other-token','new-stream').queue_actor,'');
  assert.equal(q.forStream('a','token-a','new-stream').queue_actor,fresh.queue_actor);
  assert.equal(q.current(pending.ticket,solo('a')),false);
  assert.equal(q.close('a','token-a','stream-a'),false);
  assert.equal(q.begin('a','token-a',old,solo('a')).ok,false);
  assert.equal(begin('a',1).kind,'pending');
});
test('cancel-before-join survives old and refreshed contexts', () => {
  const {q, body, solo, begin} = fixture(), b = body('a',1);
  assert.equal(q.leave('a','token-a',b,solo('a')).ok,true);
  assert.equal(q.begin('a','token-a',b,solo('a')).ok,false);
  assert.equal(begin('a',1).ok,false);
  assert.equal(begin('a',2).kind,'pending');
});
test('one in-flight attempt has one worker and admission claim', () => {
  const {q, begin, solo} = fixture(), first = begin('a',1);
  assert.equal(begin('a',1).code,'queue_admission_pending');
  assert.equal(q.claim(first.ticket,solo('a')),true);
  assert.equal(q.claim(first.ticket,solo('a')),false);
});
test('old finally cannot clear newer attempt', () => {
  const {q, begin, solo} = fixture(), old = begin('a',1), fresh = begin('a',2);
  assert.equal(fresh.kind,'pending');
  assert.equal(q.current(old.ticket,solo('a')),false);
  assert.equal(q.release(old.ticket),false);
  assert.equal(q.current(fresh.ticket,solo('a')),true);
});
test('transient release retries same attempt; permanent abort cannot', () => {
  const {q, begin, solo} = fixture(), first = begin('a',1);
  assert.equal(q.release(first.ticket),true);
  const retry = begin('a',1);
  assert.equal(retry.kind,'pending');
  assert.notEqual(retry.ticket,first.ticket);
  assert.equal(q.release(first.ticket),false);
  assert.equal(q.current(retry.ticket,solo('a')),true);
  q.abort(retry.ticket);
  assert.equal(begin('a',1).code,'queue_attempt_retired');
  assert.equal(begin('a',2).kind,'pending');
});
test('pre-ack leave removes own attempt; successful join retry idempotent', () => {
  const {q, body, solo, admit} = fixture(), b = body('a',1), p = solo('a');
  const unit = admit(q.begin('a','token-a',b,p),p);
  const retry = q.begin('a','token-a',b,p);
  assert.equal(retry.kind,'joined');
  assert.equal(retry.unit,unit);
  assert.equal(q.leave('a','token-a',b,p).unit,unit);
  q.retire(unit);
  assert.equal(q.leave('a','token-a',b,p).duplicate,true);
});
test('duplicate cancelled leave cannot affect a newer legacy unit', () => {
  const {q, body, solo} = fixture(), b = body('a',1), p = solo('a');
  q.leave('a','token-a',b,p);
  const newer = {members:['a'],code:''};
  q.track(newer);
  const nonce = q.view('a').queue_unit;
  const retry = q.leave('a','token-a',b,p);
  assert.equal(retry.duplicate,true);
  assert.equal(retry.unit,null);
  assert.deepEqual(retry.members,[]);
  assert.equal(q.view('a').queue_unit,nonce);
});
test('party member needs exact observed unit; rejection preserves context', () => {
  const {q, party, body, begin, admit} = fixture(), p = party();
  const unit = admit(begin('a',1,p),p), before = q.view('b');
  assert.equal(q.leave('b','token-b',body('b',1),p).code,'queue_unit_stale');
  assert.deepEqual(q.view('b'),before);
  assert.equal(q.leave('b','token-b',body('b',1,{queue_unit:before.queue_unit}),p).unit,unit);
});
test('member cancel defeats awaiting leader and delayed pre-arrival join', () => {
  for (const started of [false,true]) {
    const {q, party, body, begin} = fixture(), p = party(), delayed = body('a',1);
    const pending = started ? begin('a',1,p) : null;
    q.leave('b','token-b',body('b',1),p);
    if (pending) assert.equal(q.current(pending.ticket,p),false);
    assert.equal(q.begin('a','token-a',delayed,p).ok,false);
  }
});
test('party ABA, member replacement and overlapping reservation are fenced', () => {
  const {q, party, begin, body, solo, open} = fixture(), p = party();
  const pending = begin('a',1,p);
  assert.equal(q.current(pending.ticket,{...p,contexts:{...p.contexts,b:'party-b-3'}}),false);
  assert.equal(q.begin('b','token-b',body('b',1),solo('b')).code,'queue_admission_pending');
  open('b','token-b','replacement-b');
  assert.equal(q.current(pending.ticket,p),false);
});
test('retired unit cannot be targeted through reused display key', () => {
  const {q, body, solo, begin, admit} = fixture(), p = solo('a'), oldBody = body('a',1);
  const old = admit(begin('a',1),p), nonce = q.view('a').queue_unit;
  q.retire(old);
  const fresh = admit(begin('a',2),p);
  assert.equal(fresh.key,old.key);
  assert.notEqual(q.view('a').queue_unit,nonce);
  assert.equal(q.leave('a','token-a',oldBody,p).ok,false);
});
test('legacy admission during await prevents claim and resurrection', () => {
  const {q, begin, solo} = fixture(), pending = begin('a',1);
  const legacy = {code:'',members:['a']};
  q.track(legacy);
  assert.equal(q.claim(pending.ticket,solo('a')),false);
  q.retire(legacy);
  assert.equal(q.current(pending.ticket,solo('a')),false);
});
test('new actor must observe unit nonce before cancelling existing queue', () => {
  const {q, solo, begin, admit, open, body} = fixture(), p = solo('a');
  const unit = admit(begin('a',1),p);
  open('a','new-token','new-stream');
  assert.equal(q.leave('a','new-token',body('a',1),p).ok,false);
  assert.equal(q.leave('a','new-token',body('a',1,{queue_unit:q.view('a').queue_unit}),p).unit,unit);
});
