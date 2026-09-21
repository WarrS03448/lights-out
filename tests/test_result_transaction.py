"""Execute the shipped Redis Lua with fakeredis[lua]==2.38.0 / lupa==2.8."""
import json
import subprocess
from pathlib import Path

import pytest

fakeredis = pytest.importorskip('fakeredis')
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def transaction():
    script = subprocess.check_output(['node', '-e', "process.stdout.write(require('./server/result-commit.cjs').SCRIPT)"],
                                     cwd=ROOT, text=True, encoding='utf-8')
    receipt = {'match_id': 'm', 'data_collected': True, 'events': {'a': {'won': True}}}
    keys = ['receipt', 'live', 'index', 'full', 'board', 'history:a', 'rating:a', 'careers']
    plan = {'id': 'm', 'types': ['string', 'string', 'set', 'string', 'zset', 'list', 'string', 'hash'],
            'ttl': 100, 'history_ttl': 200, 'keep': 3, 'live': 2, 'live_index': 3,
            'writes': [{'index': 4, 'value': '{"id":"m","score":[7,2]}', 'ttl': 100},
                       {'index': 7, 'value': '{"rating":1501}'}],
            'histories': [{'index': 6, 'value': '{"id":"m","won":true}'}],
            'board': [{'index': 5, 'member': 'a', 'value': 10}],
            'hashes': [{'index': 8, 'field': 'a', 'value': '{"played":1,"kills":3}'}],
            'rank_checks': [{'index': 7, 'expected': 0}],
            'receipt_json': json.dumps(receipt)}
    redis = fakeredis.FakeRedis(decode_responses=True)
    redis.set('live', '{"state":"live"}'); redis.sadd('index', 'm')
    redis.set('rating:a', '{"rating":1500}')
    redis.rpush('history:a', '{"id":"m","won":null}', 'malformed historical row', '{"id":"older"}')
    return redis, lambda p=plan: redis.eval(script, len(keys), *keys, json.dumps(p)), plan


def test_receipt_and_all_result_records_commit_together_and_retry_is_idempotent(transaction):
    redis, commit, plan = transaction
    first = commit()
    assert json.loads(first)['data_collected'] is True
    assert redis.get('rating:a') == '{"rating":1501}'
    assert redis.hget('careers', 'a') == '{"played":1,"kills":3}'
    assert redis.get('live') is None and not redis.sismember('index', 'm')
    assert redis.lrange('history:a', 0, -1) == ['{"id":"m","won":true}', 'malformed historical row', '{"id":"older"}']
    plan['writes'][1]['value'] = '{"rating":1999}'
    assert commit() == first
    assert redis.get('rating:a') == '{"rating":1501}'


def test_wrong_storage_type_fails_before_any_result_write(transaction):
    redis, commit, _ = transaction
    redis.delete('history:a'); redis.set('history:a', 'wrong type')
    with pytest.raises(Exception, match='type mismatch'): commit()
    assert redis.get('receipt') is None and redis.get('full') is None
    assert redis.hget('careers', 'a') is None
    assert redis.get('rating:a') == '{"rating":1500}'
    assert redis.get('live') == '{"state":"live"}'


def test_delayed_career_write_cannot_erase_committed_totals():
    script = subprocess.check_output(['node', '-e', "process.stdout.write(require('./server/result-commit.cjs').CAREER_SCRIPT)"],
                                     cwd=ROOT, text=True, encoding='utf-8')
    redis = fakeredis.FakeRedis(decode_responses=True)
    saved = json.dumps({'played': 3, 'result_revision': 123})
    redis.hset('roster', 'a', saved)
    assert redis.eval(script, 1, 'roster', 'a', json.dumps({'played': 2})) == 0
    assert redis.hget('roster', 'a') == saved
    newer = json.dumps({'played': 3, 'result_revision': 123, 'persona': 'updated'})
    redis.eval(script, 1, 'roster', 'a', newer)
    assert redis.hget('roster', 'a') == newer


def test_concurrent_rating_change_rejects_before_any_partial_commit(transaction):
    redis, commit, _ = transaction
    redis.set('rating:a', '{"rating":1700,"revision":1}')
    with pytest.raises(Exception, match='rank conflict'): commit()
    assert redis.get('receipt') is None and redis.get('full') is None
    assert redis.hget('careers', 'a') is None
    assert redis.get('rating:a') == '{"rating":1700,"revision":1}'
