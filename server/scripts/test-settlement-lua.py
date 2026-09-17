"""Execute the production Lua with fakeredis/lupa: pip install 'fakeredis[lua]'.

Optional --deps PATH points at an isolated test dependency directory.
"""
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
SCRIPTS = json.loads(subprocess.check_output(
    ["node", "-e", "console.log(JSON.stringify(require('./server/settlement.cjs')))"],
    cwd=ROOT, text=True))


class SettlementLuaTests(unittest.TestCase):
    def setUp(self):
        self.db = fakeredis.FakeRedis(decode_responses=True)
        self.keys = ["receipt:m", "board", "live:m", "live:index", "rating:a", "rating:b"]
        self.db.set("live:m", '{"marker":"live snapshot"}')
        self.db.sadd("live:index", "m")
        self.rows = [dict(id=p, expected=0, json=json.dumps(dict(revision=1, matches=21)),
                         placing=False, progress=1000) for p in ["a", "b"]]

    def commit(self):
        return self.db.eval(SCRIPTS["COMMIT"], len(self.keys), *self.keys,
                            "m", '{"matchId":"m"}', json.dumps(self.rows))

    def test_atomic_commit_and_replay(self):
        self.assertEqual(self.commit()[0], "committed")
        self.assertIsNone(self.db.get("live:m"))
        self.assertFalse(self.db.sismember("live:index", "m"))
        self.assertEqual(self.db.zscore("board", "a"), 1000)
        self.assertEqual(self.commit()[0], "replayed")
        self.assertEqual(json.loads(self.db.get("rating:a"))["matches"], 21)

    def test_revision_conflict_changes_nothing(self):
        self.db.set("rating:b", '{"revision":2,"matches":22}')
        self.assertEqual(self.commit(), ["conflict", "b"])
        self.assertIsNone(self.db.get("rating:a"))
        self.assertIsNone(self.db.get("receipt:m"))
        self.assertIsNotNone(self.db.get("live:m"))

    def test_wrong_type_validated_before_any_write(self):
        self.db.set("board", "wrong type")
        self.assertEqual(self.commit(), ["invalid-type"])
        self.assertIsNone(self.db.get("rating:a"))
        self.assertIsNone(self.db.get("receipt:m"))

    def test_individual_write_idempotency_and_stale_revision(self):
        keys = ["rating:a", "board"]
        row = self.rows[0]
        def write(r):
            return self.db.eval(SCRIPTS["WRITE"], len(keys), *keys, json.dumps([r]))
        self.assertEqual(write(row)[0], "written")
        self.assertEqual(write(row)[0], "written")
        stale = dict(row, json='{"revision":1,"matches":999}')
        self.assertEqual(write(stale), ["conflict"])
        self.assertEqual(json.loads(self.db.get("rating:a"))["matches"], 21)

    def test_reset_removes_leaderboard_seat_atomically(self):
        self.db.zadd("board", {"a": 2500})
        row = dict(self.rows[0], placing=True, progress=0)
        self.assertEqual(self.db.eval(SCRIPTS["WRITE"], 2, "rating:a", "board",
                                     json.dumps([row]))[0], "written")
        self.assertIsNone(self.db.zscore("board", "a"))

    def test_snapshot_cannot_resurrect_a_settled_match(self):
        keys = ["receipt:m", "live:m", "live:index"]
        self.assertEqual(self.db.eval(SCRIPTS["SNAPSHOT"], 3, *keys, "m", '{"marker":"new snapshot"}', 3600), ["saved"])
        self.commit()
        self.assertEqual(self.db.eval(SCRIPTS["SNAPSHOT"], 3, *keys, "m", '{"marker":"stale snapshot"}', 3600)[0], "settled")
        self.assertIsNone(self.db.get("live:m"))
        self.assertFalse(self.db.sismember("live:index", "m"))

    def test_snapshot_validates_index_before_writing(self):
        self.db.delete("live:index")
        self.db.set("live:index", "wrong type")
        self.assertEqual(self.db.eval(SCRIPTS["SNAPSHOT"], 3, "receipt:m", "live:m", "live:index",
                                     "m", '{"marker":"new snapshot"}', 3600), ["invalid-type"])
        self.assertEqual(self.db.get("live:m"), '{"marker":"live snapshot"}')

    def test_operation_receipt_survives_newer_rank_and_prevents_duplicate_charge(self):
        row = self.rows[0]
        keys = ["rating:a", "board", "operation:1"]
        first = self.db.eval(SCRIPTS["WRITE"], 3, *keys, json.dumps([row]))
        self.assertEqual(first[0], "written")
        self.db.set("rating:a", '{"revision":2,"matches":22,"progress":1200}')
        self.db.zadd("board", {"a": 1200})
        replay = self.db.eval(SCRIPTS["WRITE"], 3, *keys, json.dumps([row]))
        self.assertEqual(replay[0], "written")
        self.assertEqual(json.loads(replay[1])["revision"], 2)
        self.assertEqual(self.db.zscore("board", "a"), 1200)

    def test_manual_rating_audit_is_atomic_and_retry_safe(self):
        row = dict(self.rows[0], audit='{"action":"elo","before":1500,"after":1600}', audit_at=5000)
        keys = ["rating:a", "board", "operation:1", "audit:1", "audits"]
        self.db.set("audits", "wrong")
        self.assertEqual(self.db.eval(SCRIPTS["WRITE"],5,*keys,json.dumps([row])),["invalid-type"])
        self.assertIsNone(self.db.get("rating:a")); self.assertIsNone(self.db.get("audit:1"))
        self.db.delete("audits")
        for _ in range(2): self.assertEqual(self.db.eval(SCRIPTS["WRITE"],5,*keys,json.dumps([row]))[0],"written")
        self.assertEqual(self.db.get("audit:1"),row["audit"])
        self.assertEqual(self.db.zcard("audits"),1)

    def test_identical_concurrent_admin_action_still_has_a_noop_audit(self):
        row = dict(self.rows[0], audit='{"action":"rank","before":{},"after":{}}', audit_at=5000)
        self.db.set("rating:a",row["json"])
        result=self.db.eval(SCRIPTS["WRITE"],5,"rating:a","board","operation:new","audit:new","audits",json.dumps([row]))
        self.assertEqual(result[0],"written")
        audit=json.loads(self.db.get("audit:new"))
        self.assertEqual(audit["before"],audit["after"]);self.assertTrue(audit["noop"])
        self.assertEqual(self.db.zcard("audits"),1)


if __name__ == "__main__":
    unittest.main()
