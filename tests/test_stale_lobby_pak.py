"""A lobby pak must not outlive the hub that installed it.

2026-09-27: a player's plain launch sat on "waiting for connection", failed and went black, because
the last ranked match's joiner pak was still in ~mods. The hub removes it only from reset_match()
and an in-memory retry list, so quitting from the result screen, an update or a reboot left it
there for every later launch. The same leftover was on Sam's machine.
"""
from pathlib import Path

import pytest

from hub import app, lobbypak, state as state_mod
from hub import game as game_mod

MATCH = '9b80656f20e994b4'


def fake_game(tmp_path):
    game = tmp_path / 'Bodycam'
    paks = game / 'Bodycam' / 'Content' / 'Paks'
    (paks / '~mods').mkdir(parents=True)
    (paks / 'pakchunk0-Windows.pak').write_bytes(b'stock')
    return game


def install(game, token='chm-' + MATCH):
    pak = Path(lobbypak.installed_path(str(game)))
    pak.write_bytes(b'\x00GM_CHJoin_C\x00' + token.encode('ascii') + b'\x00')
    return pak


@pytest.fixture
def running(monkeypatch):
    """Whether a Bodycam process is up; lobbypak asks game_running(), which asks game_pids()."""
    state = [False]
    monkeypatch.setattr(game_mod, 'game_pids', lambda: [4242] if state[0] else [])
    return state


def test_startup_removes_a_pak_no_running_game_has_mounted(tmp_path, running):
    game = fake_game(tmp_path)
    pak = install(game)
    assert lobbypak.remove_stale(str(game)) is True
    assert not pak.exists()


def test_startup_keeps_the_pak_a_running_game_has_mounted(tmp_path, running):
    game = fake_game(tmp_path)
    pak = install(game)
    running[0] = True
    assert lobbypak.remove_stale(str(game)) is False
    assert pak.exists()


def test_startup_with_nothing_installed_or_no_game_is_a_no_op(tmp_path, running):
    assert lobbypak.remove_stale(str(fake_game(tmp_path))) is False
    assert lobbypak.remove_stale('') is False
    assert lobbypak.remove_stale(None) is False


def test_hub_startup_finds_the_remembered_game_folder(tmp_path, running):
    game = fake_game(tmp_path)
    pak = install(game)
    state_mod.update_fields({'game_dir': str(game)})
    assert app.remove_stale_lobby_pak() is True
    assert not pak.exists()


def test_hub_startup_never_raises(monkeypatch):
    def boom():
        raise OSError('state unreadable')
    monkeypatch.setattr(state_mod, 'load', boom)
    assert app.remove_stale_lobby_pak() is False


@pytest.mark.parametrize('token', ['chm-' + MATCH, 'chm-' + MATCH + '-r' + 'c' * 16])
def test_the_worker_removes_only_its_own_match_pak(tmp_path, running, token):
    game = fake_game(tmp_path)
    pak = install(game, token)
    assert lobbypak.remove_for_match(str(game), '1' * 16) is False
    assert pak.exists(), 'the next match may already have installed its pak'
    assert lobbypak.remove_for_match(str(game), MATCH) is True
    assert not pak.exists()


def test_the_worker_leaves_a_pak_a_running_game_has_mounted(tmp_path, running):
    game = fake_game(tmp_path)
    pak = install(game)
    running[0] = True
    assert lobbypak.remove_for_match(str(game), MATCH) is False
    assert pak.exists()


def test_the_worker_refuses_a_malformed_match_id(tmp_path, running):
    game = fake_game(tmp_path)
    pak = install(game, 'chm-')
    for bad in ('', None, 'chm-', MATCH.upper(), MATCH[:-1]):
        assert lobbypak.remove_for_match(str(game), bad) is False
    assert pak.exists()
    assert lobbypak.remove_for_match(str(fake_game(tmp_path / 'other')), MATCH) is True, \
        'nothing installed is already the goal'
