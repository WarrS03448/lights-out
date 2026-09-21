"""Run: python -m pytest tests/test_tournament_lua.py (fakeredis[lua] installed)."""
import json
import subprocess
from pathlib import Path
import fakeredis
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = json.loads(subprocess.check_output(['node', '-e', "console.log(JSON.stringify(require('./server/tournament.cjs')))"], cwd=ROOT, text=True, encoding='utf-8'))

def register(db, player='p1', game='g1', end=9999999999999):
    return db.eval(SCRIPTS['REGISTER'], 2, 'registrations', 'identities', json.dumps(dict(player_id=player, game_steam_id=game, persona='Test')), end)

def test_registration_retry_keeps_timestamp_and_game_collision_cannot_write_partial_record():
    db = fakeredis.FakeRedis(decode_responses=True)
    first = register(db)
    assert first[0] == 'registered'
    assert register(db) == ['existing', first[1]]
    assert register(db, 'p2') == ['identity_conflict']
    assert db.hlen('registrations') == 1
    assert db.hget('identities', 'g1') == 'p1'
    assert json.loads(first[1])['registered_at'] > 0

def test_end_and_bad_index_fail_without_writes():
    db = fakeredis.FakeRedis(decode_responses=True)
    assert register(db, end=1) == ['ended']
    assert db.dbsize() == 0
    db.set('identities', 'wrong type')
    with pytest.raises(Exception, match='event key type'):
        register(db)
    assert db.exists('registrations') == 0

def test_immutable_ledger_retries_conflicts_and_snapshot():
    db = fakeredis.FakeRedis(decode_responses=True)
    def project(value):
        return db.eval(SCRIPTS['PROJECT'], 1, 'matches', 'm1', value)
    assert project('{"delta":25}') == 'stored'
    assert project('{"delta":25}') == 'duplicate'
    with pytest.raises(Exception, match='event receipt conflict'):
        project('{"delta":999}')
    assert db.hlen('matches') == 1
    register(db)
    snapshot = db.eval(SCRIPTS['READ'], 3, 'registrations', 'matches', 'operations')
    assert len(snapshot[0]) == 1
    assert snapshot[1] == ['{"delta":25}']
