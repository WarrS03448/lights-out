"""Run with Python test-combat-ledger-lua.py --deps work/lua-deps (fakeredis[lua]==2.31.3)."""
import json
from pathlib import Path
import subprocess
import sys
import unittest

if len(sys.argv) > 2 and sys.argv[1] == "--deps":
    sys.path.insert(0, sys.argv[2])
    del sys.argv[1:3]
import fakeredis

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = subprocess.check_output(["node", "-e", "process.stdout.write(require('./server/combat-ledger.cjs').COMMIT)"], cwd=ROOT, text=True)


class CombatLedgerTests(unittest.TestCase):
    def setUp(self):
        self.db = fakeredis.FakeRedis(decode_responses=True)
        self.keys = ["history", "rank", "penalty", "board", "receipt"]
        self.payload = dict(expected=0, historyJson='{"revision":1,"incidents":[]}',
            receipt=True, expectedRank=0, expectedPenalty="", rankJson='{"revision":1,"progress":975}',
            penaltyJson='{"reason":"team_kill","count":1}', receiptJson='{"decisionId":"d","rr":25}',
            placing=False, progress=975, player="p")

    def commit(self):
        return self.db.eval(SCRIPT, len(self.keys), *self.keys, json.dumps(self.payload))

    def test_atomic_charge_and_idempotent_replay(self):
        self.assertEqual(self.commit()[0], "committed")
        self.assertEqual(self.db.zscore("board", "p"), 975)
        self.assertEqual(json.loads(self.db.get("penalty"))["reason"], "team_kill")
        self.assertEqual(self.commit()[0], "replayed")
        self.assertEqual(json.loads(self.db.get("penalty"))["count"], 1)
        self.assertEqual(json.loads(self.db.get("history"))["revision"], 1)

    def test_all_conflicts_leave_every_other_key_unchanged(self):
        for key, value in [("history", '{"revision":2}'), ("rank", '{"revision":2}'), ("penalty", '{"count":2}')]:
            with self.subTest(key=key):
                self.db.flushall(); self.db.set(key, value)
                self.assertEqual(self.commit(), ["conflict"])
                self.assertEqual(self.db.keys(), [key])
                self.assertEqual(self.db.get(key), value)

    def test_wrong_board_type_cannot_partially_charge(self):
        self.db.set("board", "invalid")
        self.assertEqual(self.commit(), ["invalid-type"])
        self.assertEqual(self.db.keys(), ["board"])

    def test_invalid_score_cannot_partially_charge(self):
        self.payload["progress"] = "not-a-score"
        self.assertEqual(self.commit(), ["invalid-payload"])
        self.assertEqual(self.db.keys(), [])

    def test_invalid_json_cannot_partially_charge(self):
        self.payload["penaltyJson"] = "invalid"
        with self.assertRaises(Exception):
            self.commit()
        self.assertEqual(self.db.keys(), [])

    def test_observation_only_does_not_touch_ratings(self):
        self.payload = dict(expected=0, historyJson='{"revision":1,"incidents":[]}')
        self.assertEqual(self.commit()[0], "committed")
        self.assertEqual(self.db.keys(), ["history"])


if __name__ == "__main__":
    unittest.main()
