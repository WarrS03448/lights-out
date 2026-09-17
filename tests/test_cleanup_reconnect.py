"""Exercise real cleanup registration with a simulated crash and replacement process."""
import pytest

from hub import game, match_cleanup as cleanup
from test_hub import _live_session


@pytest.fixture
def playing(monkeypatch, tmp_path):
    session, panel = _live_session()
    session.match_id = 'a' * 16
    session.phase = 'live'
    session._game_launch_at = 100.0
    exe = tmp_path / 'Bodycam' / 'Binaries' / 'Win64' / cleanup.GAME_EXE
    exe.parent.mkdir(parents=True)
    exe.touch()
    monkeypatch.setattr(session, '_game_dir', lambda: str(tmp_path))
    monkeypatch.setattr(cleanup.sys, 'platform', 'win32')
    monkeypatch.setattr(cleanup, 'ensure_worker', lambda _: None)
    processes = {42: {'pid': 42, 'created': 116444737010000000,
                      'exe': cleanup._normal_path(str(exe))}}
    monkeypatch.setattr(game, 'game_pids', lambda: list(processes))
    monkeypatch.setattr(game, 'game_running', lambda: bool(processes))
    monkeypatch.setattr(cleanup, 'capture_identity', lambda pid, _: processes.get(pid))
    monkeypatch.setattr(game, 'launch_game', lambda: True)
    session._take_game_ownership()
    old_job = session._cleanup_job
    assert cleanup._read(cleanup._job_path(old_job))['identity']['pid'] == 42
    panel.pending.clear()

    def crash_and_reopen():
        processes.clear()
        # More than two minutes after the original launch; the old launch window is invalid.
        processes[43] = {'pid': 43, 'created': 116444740000000000,
                         'exe': cleanup._normal_path(str(exe))}

    return session, panel, processes, old_job, crash_and_reopen


@pytest.mark.parametrize('host', [False, True])
def test_manual_reconnect_replaces_finished_cleanup_job(playing, host):
    session, panel, processes, old_job, reopen = playing
    session.host = session.me if host else {'steam_id': '76561198000000042'}
    processes.clear()
    cleanup._status(old_job, 'done', reason='verified_exit')
    session._watch_cleanup(old_job, session._close_gen)
    assert panel.pending, 'an active match must keep observing after the original process exits'
    reopen()
    panel.pending.pop(0)()
    assert session._cleanup_job != old_job
    replacement = cleanup._read(cleanup._job_path(session._cleanup_job))
    assert replacement['identity']['pid'] == 43
    assert replacement['match_id'] == 'a' * 16
    assert session._game_ours and session.game_close == ''
    assert cleanup.read_status(session._cleanup_job)['state'] == 'pending'


def test_result_reconciles_replacement_before_old_worker_notices_crash(playing):
    session, _, _, old_job, reopen = playing
    reopen()
    # The old worker still says pending; completion can beat its next process poll.
    session._on_result({'match_id': session.match_id, 'winner': 1, 'score': [2, 1]})
    assert session.phase == 'result' and session.game_close == 'armed'
    assert session._cleanup_job != old_job
    assert cleanup._read(cleanup._job_path(session._cleanup_job))['identity']['pid'] == 43


def test_relaunch_button_adopts_game_already_reopened_in_steam(playing):
    session, _, _, old_job, reopen = playing
    reopen()
    session.relaunch_game()
    replacement = cleanup._read(cleanup._job_path(session._cleanup_job))
    assert session._cleanup_job != old_job
    assert replacement['identity']['pid'] == 43, 'must not wait for another process to appear'


@pytest.mark.parametrize('ending', ['reset', 'result'])
def test_old_cleanup_callback_cannot_adopt_a_game_after_match_ends(playing, ending):
    session, panel, _, old_job, reopen = playing
    generation = session._close_gen
    if ending == 'reset':
        session.reset_match()
        session.match_id = 'b' * 16
    else:
        session.phase = 'result'
        session.match_complete = 'match_result'
    cleanup._status(old_job, 'done', reason='verified_exit')
    reopen()
    session._watch_cleanup(old_job, generation)
    assert not panel.pending
    assert session._cleanup_job in (None, old_job)
    assert len([p for p in cleanup._directory().glob('*.json') if len(p.stem) == 32]) == 1


def test_old_callback_cannot_clear_replacement_ownership(playing):
    session, _, _, old_job, reopen = playing
    old_generation = session._close_gen
    reopen()
    session.relaunch_game()
    replacement = session._cleanup_job
    cleanup._status(old_job, 'done', reason='verified_exit')
    session._watch_cleanup(old_job, old_generation)
    assert session._cleanup_job == replacement and session._game_ours


def test_completing_a_pending_launch_cannot_capture_a_later_process(playing, monkeypatch):
    session, _, processes, _, _ = playing
    processes.clear()
    monkeypatch.setattr(cleanup.time, 'time', lambda: 500.0)
    session.relaunch_game()
    pending = session._cleanup_job
    assert cleanup._read(cleanup._job_path(pending))['identity'] is None
    session._on_result({'match_id': session.match_id, 'winner': 1, 'score': [2, 1]})
    processes[99] = {'pid': 99, 'created': 116444741010000000, 'exe': 'unused-by-fake'}
    assert cleanup._capture_launched(cleanup._read(cleanup._job_path(pending))) is None


def test_completion_during_process_enumeration_enforces_the_new_cutoff(playing, monkeypatch):
    session, _, processes, _, _ = playing
    processes.clear()
    monkeypatch.setattr(cleanup.time, 'time', lambda: 500.0)
    session.relaunch_game()
    pending = session._cleanup_job
    processes[99] = {'pid': 99, 'created': 116444741010000000, 'exe': 'unused-by-fake'}
    def enumerate_after_completion():
        cleanup.finish_launch(pending)
        return [99]
    monkeypatch.setattr(game, 'game_pids', enumerate_after_completion)
    assert cleanup._capture_launched(cleanup._read(cleanup._job_path(pending))) is None


def test_delayed_registration_does_not_capture_a_future_game_after_completion(playing, monkeypatch):
    session, panel, processes, _, reopen = playing
    processes.clear()
    session._game_launch_at = None
    session._take_game_ownership()
    assert session._cleanup_job is None and panel.pending
    session._on_result({'match_id': session.match_id, 'winner': 1, 'score': [2, 1]})
    reopen()
    session._register_cleanup(session._close_gen, attempt=1)
    assert session._cleanup_job is None


def test_failed_registration_never_falls_back_to_closing_every_bodycam(playing, monkeypatch):
    session, panel, _, _, _ = playing
    session._cleanup_job = None
    monkeypatch.setattr(cleanup, 'register', lambda *args, **kwargs: None)
    broad_closes = []
    monkeypatch.setattr(game, 'close_game', lambda **kwargs: broad_closes.append(kwargs) or 'closed')
    session._run_off_thread = lambda fn: fn()
    session._on_result({'match_id': session.match_id, 'winner': 1, 'score': [2, 1]})
    panel.pump(limit=30)
    assert not broad_closes
