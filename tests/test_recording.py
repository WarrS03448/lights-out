"""Run: python -m pytest tests/test_recording.py -q; all game paths are temporary."""
import hashlib
import json
from pathlib import Path
import pytest
from hub import recording, version, paths, game

@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(version, 'RECORDING', True, raising=False)
    monkeypatch.setenv('HUB_STATE_DIR', str(tmp_path/'state'))
    monkeypatch.setattr(game, 'game_running', lambda: False)
    root = tmp_path/'Bodycam'
    mods = root/'Bodycam/Content/Paks/~mods'
    mods.mkdir(parents=True)
    (mods.parent/'stock.pak').write_bytes(b'stock')
    return root, mods

def test_roundtrip_restores_original_and_preserves_unrelated_files(setup):
    root, mods = setup
    target = mods/'CommunityGamemodes_P.pak'
    target.write_bytes(b'public')
    (mods/'other.pak').write_bytes(b'other')
    new = root/'new.pak'; new.write_bytes(b'private')
    recording.before_write(root, target.name, new)
    target.write_bytes(new.read_bytes())
    recording.restore()
    assert target.read_bytes() == b'public'
    assert (mods/'other.pak').read_bytes() == b'other'
    assert not (paths.state_dir()/'recording-active.json').exists()

def test_absent_original_is_restored_as_absent_without_deleting_private_bytes(setup):
    root, mods = setup
    new = root/'new.pak'; new.write_bytes(b'private')
    recording.before_write(root, 'CommunityGamemodes_P.pak', new)
    (mods/'CommunityGamemodes_P.pak').write_bytes(b'private')
    recording.restore()
    assert not (mods/'CommunityGamemodes_P.pak').exists()
    assert not list(mods.glob('*recording-saved-*'))
    assert any(p.read_bytes() == b'private' for p in (paths.state_dir()/'recording-backups').rglob('*.recording'))

def test_unknown_newer_modification_is_never_overwritten(setup):
    root, mods = setup
    target = mods/'CommunityGamemodes_P.pak'; target.write_bytes(b'public')
    new = root/'new.pak'; new.write_bytes(b'private')
    recording.before_write(root, target.name, new)
    target.write_bytes(b'newer-unexpected')
    with pytest.raises(RuntimeError, match='changed outside'):
        recording.restore()
    assert target.read_bytes() == b'newer-unexpected'
    assert (paths.state_dir()/'recording-active.json').exists()

def test_corrupt_backup_stops_all_restoration(setup):
    root, mods = setup
    target = mods/'CommunityGamemodes_P.pak'; target.write_bytes(b'public')
    new = root/'new.pak'; new.write_bytes(b'private')
    recording.before_write(root, target.name, new); target.write_bytes(b'private')
    backup = next((paths.state_dir()/'recording-backups').rglob('*.original'))
    backup.write_bytes(b'broken')
    with pytest.raises(RuntimeError, match='backup'):
        recording.restore()
    assert target.read_bytes() == b'private'

def test_crash_before_replace_and_repeated_restore_are_safe(setup):
    root, mods = setup
    target = mods/'CommunityGamemodes_P.pak'; target.write_bytes(b'public')
    new = root/'new.pak'; new.write_bytes(b'private')
    recording.before_write(root,target.name,new)
    recording.restore(); recording.restore()
    assert target.read_bytes() == b'public'

def test_crash_before_first_install_with_no_originals_is_safe(setup):
    root, mods=setup
    new=root/'new.pak';new.write_bytes(b'private')
    recording.before_write(root,'CommunityGamemodes_P.pak',new)
    recording.restore()
    assert not (paths.state_dir()/'recording-active.json').exists()

def test_game_running_refuses_changes_and_restore(setup,monkeypatch):
    root, mods = setup
    monkeypatch.setattr(game,'game_running',lambda:True)
    with pytest.raises(RuntimeError,match='Close Bodycam'):
        recording.before_write(root,'CommunityGamemodes_P.pak',None)

def test_cleanup_absence_does_not_lose_original_lobby(setup):
    root, mods = setup
    lobby = mods/'CommunityLobby_P.pak'; lobby.write_bytes(b'oldlobby')
    new=root/'new.pak'; new.write_bytes(b'privatelobby')
    recording.before_write(root,lobby.name,new); lobby.write_bytes(b'privatelobby')
    recording.before_write(root,lobby.name,None); lobby.unlink()
    recording.restore()
    assert lobby.read_bytes() == b'oldlobby'

def test_denied_login_finishes_instead_of_polling_forever():
    from hub.auth import wait_for, AuthError
    with pytest.raises(AuthError,match='not invited'):
        wait_for('x',poll_fn=lambda code:{'status':'denied'})

def test_restart_during_recording_match_preserves_private_files_for_rejoin(setup,monkeypatch):
    root,mods=setup
    target=mods/'CommunityGamemodes_P.pak';target.write_bytes(b'public')
    new=root/'new.pak';new.write_bytes(b'private')
    recording.before_write(root,target.name,new);target.write_bytes(b'private')
    monkeypatch.setattr(game,'game_running',lambda:True)
    recording.resume_or_restore()
    assert target.read_bytes()==b'private'
    assert (paths.state_dir()/'recording-active.json').exists()
    target.write_bytes(b'unexpected')
    with pytest.raises(RuntimeError,match='changed outside'):
        recording.resume_or_restore()

def test_no_tray_close_cannot_release_the_client_before_restore(setup,monkeypatch):
    from hub.webui.shell import CloseToTray
    from types import SimpleNamespace
    monkeypatch.setattr(recording,'ensure_exit',lambda:False)
    closer=CloseToTray(SimpleNamespace())
    assert closer.on_closing() is False
