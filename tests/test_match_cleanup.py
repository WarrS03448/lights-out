import copy
import ctypes
import json

import pytest
from hub import match_cleanup as cleanup


def setup_worker(monkeypatch, receipt_after=0, identity_error=False):
    job_id = 'a' * 32
    identity = {'pid': 42, 'created': 1234, 'exe': 'bodycam.exe'}
    job = {'schema': 2, 'job_id': job_id, 'match_id': '1' * 16, 'steam_id': '76561198000000001',
           'credential': 'fixture-only', 'expected_exe': 'bodycam.exe', 'identity': identity}
    cleanup._write(cleanup._job_path(job_id), job)
    clock = [0.0]; calls = []; checks = [0]; exited = [False]

    class Kernel:
        def CreateMutexW(self, *_): return 7
        def CloseHandle(self, handle): calls.append(('release', handle))
        def TerminateProcess(self, handle, _):
            assert checks[0] > receipt_after, 'force before collection acknowledgement'
            assert handle == 8
            calls.append(('force', handle)); exited[0] = True; return True

    monkeypatch.setattr(cleanup, '_apis', lambda: (Kernel(), None, None))
    monkeypatch.setattr(ctypes, 'get_last_error', lambda: 0)
    monkeypatch.setattr(ctypes, 'set_last_error', lambda _: None)
    monkeypatch.setattr(cleanup, '_open_process', lambda pid: 8)
    monkeypatch.setattr(cleanup, '_exited', lambda _: exited[0])
    identity_calls = [0]
    def get_identity(*_):
        identity_calls[0] += 1
        if identity_error and identity_calls[0] == 1:
            raise OSError('query unavailable')
        return identity
    monkeypatch.setattr(cleanup, '_identity', get_identity)
    monkeypatch.setattr(cleanup.time, 'time', lambda: clock[0])
    monkeypatch.setattr(cleanup.time, 'monotonic', lambda: clock[0])
    def sleep(seconds):
        clock[0] += seconds
        assert clock[0] < 100, 'worker failed to finish bounded fixture'
    monkeypatch.setattr(cleanup.time, 'sleep', sleep)
    def receipt(_):
        checks[0] += 1
        if checks[0] <= receipt_after: return None
        return {'ok': True, 'match_id': job['match_id'], 'data_collected': True,
                'close_allowed': True, 'close_after': clock[0] * 1000 + 5000,
                'result': {'match_id': job['match_id']}}
    monkeypatch.setattr(cleanup, '_receipt', receipt)
    monkeypatch.setattr(cleanup, '_graceful_close', lambda handle, pid: calls.append(('graceful', clock[0])))
    return job_id, job, calls, clock, identity_calls


@pytest.mark.parametrize('host', [True, False])
def test_every_player_waits_for_saved_receipt_then_graceful_then_verified_force(monkeypatch, host):
    job_id, job, calls, clock, _ = setup_worker(monkeypatch, receipt_after=3)
    job['host'] = host; cleanup._write(cleanup._job_path(job_id), job)
    assert cleanup.run_worker(job_id) == 0
    assert [c[0] for c in calls if c[0] != 'release'] == ['graceful', 'force']
    assert clock[0] >= 3 * 3 + 5 + cleanup.GRACE_SECONDS
    status = cleanup.read_status(job_id)
    assert status['state'] == 'done' and status['reason'] == 'verified_exit'
    assert status['result']['match_id'] == job['match_id']


def test_process_identity_query_failure_retries_before_any_close(monkeypatch):
    job_id, _, calls, _, queries = setup_worker(monkeypatch, identity_error=True)
    assert cleanup.run_worker(job_id) == 0
    assert queries[0] == 2
    assert calls[0] == ('release', 8)


def test_reused_pid_or_replacement_game_is_never_closed(monkeypatch):
    job_id, job, calls, _, _ = setup_worker(monkeypatch)
    monkeypatch.setattr(cleanup, '_identity', lambda *_: {**job['identity'], 'created': 9999})
    assert cleanup.run_worker(job_id) == 0
    assert cleanup.read_status(job_id)['state'] == 'superseded'
    assert not any(c[0] in ('force', 'graceful') for c in calls)


def test_receipt_must_confirm_exact_match_and_complete_data():
    job = {'match_id': 'a' * 16}
    complete = {'ok': True, 'match_id': job['match_id'], 'data_collected': True, 'close_allowed': True}
    assert cleanup.receipt_allows_close(job, complete)
    for field in complete:
        broken = copy.deepcopy(complete); del broken[field]
        assert not cleanup.receipt_allows_close(job, broken)
    assert not cleanup.receipt_allows_close(job, {**complete, 'match_id': 'b' * 16})


def test_job_argument_cannot_be_a_path():
    with pytest.raises(ValueError): cleanup._job_path('../state')


def test_signout_waits_only_for_own_unfinished_cleanup():
    player = 'a1111111-1111-4111-8111-111111111111'
    job_id = 'b' * 32
    cleanup._write(cleanup._job_path(job_id), {'steam_id': player})
    assert cleanup.pending_for(player)
    assert not cleanup.pending_for('76561198000000001')
    for terminal in cleanup.TERMINAL:
        cleanup._status(job_id, terminal)
        assert not cleanup.pending_for(player)


def test_dispatch_does_not_import_the_ui(monkeypatch):
    monkeypatch.setattr(cleanup, 'run_worker', lambda job: 23 if job == 'a' * 32 else 1)
    with pytest.raises(SystemExit) as result:
        cleanup.dispatch(['LightsOut.exe', '--match-cleanup', 'a' * 32])
    assert result.value.code == 23


def test_captured_identity_must_be_saved_before_it_can_authorize_cleanup(monkeypatch):
    job_id, job, calls, _, _ = setup_worker(monkeypatch)
    job['identity'] = None; job['launched_at'] = 0
    cleanup._write(cleanup._job_path(job_id), job)
    candidate = {'pid': 42, 'created': 1234, 'exe': 'bodycam.exe'}
    captured = [0]
    def capture(_): captured[0] += 1; return candidate
    monkeypatch.setattr(cleanup, '_capture_launched', capture)
    original = cleanup._write; failed = [False]
    def write(path, value):
        if path == cleanup._job_path(job_id) and not failed[0]:
            failed[0] = True; raise OSError('disk busy')
        return original(path, value)
    monkeypatch.setattr(cleanup, '_write', write)
    assert cleanup.run_worker(job_id) == 0
    assert captured[0] == 2
    assert cleanup._read(cleanup._job_path(job_id))['identity'] == candidate
