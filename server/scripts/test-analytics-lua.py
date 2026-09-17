"""Run: python server/scripts/test-analytics-lua.py. Executes production Lua in fakeredis."""
import json
import subprocess
import unittest
from pathlib import Path
import fakeredis
ROOT = Path(__file__).resolve().parents[2]
S = json.loads(subprocess.check_output(["node", "-e", "console.log(JSON.stringify(require('./server/analytics-store.cjs')))"], cwd=ROOT, text=True))
class AnalyticsLua(unittest.TestCase):
    def setUp(self): self.db = fakeredis.FakeRedis(decode_responses=True)
    def events(self):
        row = dict(id="one", key="event:one", at=5000, json='{"type":"app.action"}',group="g",kind="app.action",source="client",version="test",metric=dict(events=1))
        return self.db.eval(S["EVENTS"],4,"events","health","reliability","event:one",json.dumps([row]),2592000,0,5000,4102444800)
    def test_retry_is_acknowledged_without_duplicate_count(self):
        self.assertEqual(self.events(), ["one"]); self.assertEqual(self.events(), ["one"])
        self.assertEqual(self.db.hget("health","events"),"1")
        self.assertEqual(self.db.zcard("events"),1)
    def test_bad_type_has_no_partial_effect(self):
        self.db.set("health","wrong")
        with self.assertRaises(Exception): self.events()
        self.assertIsNone(self.db.get("event:one")); self.assertEqual(self.db.zcard("events"),0)
    def project(self):
        b=dict(day="2026-09-17",cohort=dict(map="Rome"),metrics=dict(matches=1,rr_sum=12))
        return self.db.eval(S["PROJECT"],6,"marker","match","matches","day","health","summary",'{"id":"m"}',5000,"cohort",json.dumps(b),4102444800,31536000,94867200,0,5000,"hash-v1",'{"id":"m"}')
    def test_projection_retry_and_restart_do_not_inflate_aggregates(self):
        self.assertEqual(self.project(),"stored"); self.assertEqual(self.project(),"duplicate")
        self.assertEqual(json.loads(self.db.hget("day","cohort"))["metrics"]["matches"],1)
        self.assertEqual(self.db.zcard("matches"),1)
    def test_bad_projection_key_preserves_receipt_and_other_keys(self):
        self.db.set("day","wrong")
        with self.assertRaises(Exception): self.project()
        self.assertIsNone(self.db.get("marker")); self.assertIsNone(self.db.get("match"))
    def test_corrupt_aggregate_and_counter_cannot_leave_partial_writes(self):
        self.db.hset("day","cohort",'{"metrics":{"matches":"bad"}}')
        with self.assertRaises(Exception): self.project()
        self.assertIsNone(self.db.get("marker")); self.assertIsNone(self.db.get("match"))
        self.db.hset("health","events","9223372036854775807")
        with self.assertRaises(Exception): self.events()
        self.assertIsNone(self.db.get("event:one"))
    def test_daily_reliability_retry_and_payload_retention(self):
        self.events();self.events()
        self.assertEqual(json.loads(self.db.hget("reliability","g"))["metrics"]["events"],1)
        self.assertGreater(self.db.ttl("event:one"),2591900)
        self.project();self.assertGreater(self.db.ttl("marker"),self.db.ttl("match"))
if __name__ == "__main__": unittest.main()
