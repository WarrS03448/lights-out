"""Run with the project Python: validates the actual atomic reset Lua in Redis."""
import json
import subprocess
import unittest
from pathlib import Path

import fakeredis

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = json.loads(subprocess.check_output([
    'node', '-e', "console.log(JSON.stringify(require('./tools/release/reset-ranked.cjs').RESET))"
], cwd=ROOT, text=True, encoding='utf-8'))


class ResetLuaTests(unittest.TestCase):
    def setUp(self):
        self.db = fakeredis.FakeRedis(decode_responses=True)
        self.db.set('hub:rating:1', 'old-rating')
        self.db.set('hub:settlement:m', 'old-match')
        self.db.set('hub:auth:token', 'preserved')
        self.db.hset('hub:roster', '1', '{"persona":"Player","kills":9}')
        self.keys = ['hub:roster','hub:release:ranked-reset','hub:rating:1','hub:settlement:m']
        self.rows = [{'id':'1','before':'{"persona":"Player","kills":9}',
                      'after':'{"persona":"Player","kills":0}'}]

    def run_reset(self):
        return self.db.eval(SCRIPT,len(self.keys),*self.keys,json.dumps(self.rows),'123456')

    def test_atomic_reset_preserves_identity_and_cannot_run_again(self):
        self.assertEqual(self.run_reset(),2)
        self.assertIsNone(self.db.get('hub:rating:1'))
        self.assertIsNone(self.db.get('hub:settlement:m'))
        self.assertEqual(self.db.get('hub:auth:token'),'preserved')
        self.assertEqual(json.loads(self.db.hget('hub:roster','1'))['kills'],0)
        self.db.set('hub:rating:1','new-season-rating')
        with self.assertRaises(Exception): self.run_reset()
        self.assertEqual(self.db.get('hub:rating:1'),'new-season-rating')

    def test_concurrent_roster_change_aborts_without_deleting_anything(self):
        self.db.hset('hub:roster','1','{"persona":"Changed"}')
        with self.assertRaises(Exception): self.run_reset()
        self.assertEqual(self.db.get('hub:rating:1'),'old-rating')
        self.assertIsNone(self.db.get('hub:release:ranked-reset'))


if __name__ == '__main__': unittest.main()
