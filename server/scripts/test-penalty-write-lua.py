"""Actual Lua: python test-penalty-write-lua.py --deps work/lua-deps."""
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest

if len(sys.argv) > 2 and sys.argv[1] == '--deps':
    sys.path.insert(0, sys.argv[2])
    del sys.argv[1:3]
import fakeredis

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = subprocess.check_output(['node', '-e', "process.stdout.write(require('./server/penalty-write.cjs').WRITE)"], cwd=ROOT, text=True)


class PenaltyWriteTests(unittest.TestCase):
    def setUp(self):
        self.db = fakeredis.FakeRedis(decode_responses=True)
        self.now = int(time.time() * 1000)
        self.record = dict(last=self.now, until=self.now+60000, reason='no_show', count=1, elo=25)
        self.op = self.operation(self.record)

    def operation(self, record, operation_id='a'*32):
        return dict(operationId=operation_id, json=json.dumps(record), last=record['last'], expiresAt=record['until']+14*86400000)

    def write(self, op=None):
        return self.db.eval(SCRIPT, 2, 'penalty', 'penalty:write', json.dumps(op or self.op))

    def test_first_write_and_retry_do_not_refresh_ttl_or_rewrite_state(self):
        self.assertEqual(self.write()[0], 'written')
        self.db.expire('penalty', 7)
        guard = self.db.get('penalty:write')
        self.assertEqual(self.write(), ['replayed', self.op['json']])
        self.assertLessEqual(self.db.ttl('penalty'), 7)
        self.assertEqual(self.db.get('penalty:write'), guard)
        self.assertEqual(self.db.ttl('penalty:write'), -1)

    def test_delayed_legacy_write_never_overwrites_newer_combat(self):
        current = {**self.record, 'last': self.now+1, 'until': self.now+120000, 'count': 2, 'reason': 'team_kill'}
        self.db.set('penalty', json.dumps(current))
        status, saved = self.write()
        self.assertEqual(status, 'stale'); self.assertEqual(json.loads(saved), current)
        self.assertEqual(json.loads(self.db.get('penalty')), current)
        self.assertFalse(self.db.exists('penalty:write'))

    def test_same_millisecond_combat_wins_but_distinct_legacy_strikes_can_advance(self):
        self.write()
        higher = {**self.record, 'count': 2, 'elo': 50, 'until': self.now+120000}
        self.assertEqual(self.write(self.operation(higher, 'b'*32))[0], 'written')
        self.assertEqual(json.loads(self.db.get('penalty'))['count'], 2)
        self.assertEqual(self.write()[0], 'stale')
        combat = {**higher, 'reason': 'team_kill'}
        self.db.set('penalty', json.dumps(combat))
        self.assertEqual(self.write(self.operation({**higher, 'count': 3}, 'c'*32))[0], 'stale')
        self.assertEqual(json.loads(self.db.get('penalty')), combat)

    def test_retry_returns_newer_durable_state_after_another_penalty(self):
        self.write()
        combat = {**self.record, 'last': self.now+1, 'reason': 'team_kill', 'count': 3}
        self.db.set('penalty', json.dumps(combat))
        status, saved = self.write()
        self.assertEqual(status, 'replayed'); self.assertEqual(json.loads(saved), combat)

    def test_tombstone_and_absolute_expiry_prevent_resurrection(self):
        self.write(); self.db.delete('penalty')
        self.assertEqual(self.write(), ['replayed', ''])
        older = self.operation({**self.record, 'last': self.now-1}, 'b'*32)
        self.assertEqual(self.write(older), ['stale', ''])
        expired = self.operation({**self.record, 'last': self.now+1}, 'c'*32)
        expired['expiresAt'] = self.now-1000
        self.assertEqual(self.write(expired), ['stale', ''])
        self.assertFalse(self.db.exists('penalty'))

    def test_reused_operation_id_cannot_change_its_original_payload(self):
        self.write()
        self.assertEqual(self.write(self.operation({**self.record, 'count': 5})), ['invalid-operation'])
        self.assertEqual(self.db.get('penalty'), self.op['json'])

    def test_all_validation_precedes_mutation(self):
        self.db.lpush('penalty:write', 'wrong-type')
        self.assertEqual(self.write(), ['invalid-type']); self.assertFalse(self.db.exists('penalty'))
        self.db.flushall()
        invalid = {**self.op, 'json': '{broken'}
        self.assertEqual(self.write(invalid), ['invalid-payload']); self.assertEqual(self.db.keys(), [])
        self.db.set('penalty', '{broken')
        self.assertEqual(self.write(), ['invalid-current']); self.assertEqual(self.db.keys(), ['penalty'])

    def test_legacy_saved_record_without_last_is_migratable(self):
        old = dict(until=self.now+30000, count=2, elo=50, reason='no_show')
        self.db.set('penalty', json.dumps(old))
        newer = {**self.record, 'count': 3, 'elo': 75}
        self.assertEqual(self.write(self.operation(newer))[0], 'written')
        self.assertEqual(json.loads(self.db.get('penalty')), newer)


if __name__ == '__main__':
    unittest.main()
