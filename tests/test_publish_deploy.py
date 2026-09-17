"""publish.py's deploy step: it pushes to GitHub, and it refuses to ship a half-deploy.

`railway up` was the deploy path until 2026-09-15, when the community-hub service was reconnected
to the repo with Root Directory "/server". `railway up` uploads the CONTENTS of server/, railpack
then cannot find a server/ folder inside that archive, and the build dies with "railpack prepare
exited with an error" while the previous deployment keeps serving — so the site silently stays on
the old version. deploy() pushes instead.

Two behaviours are worth a test because both fail SILENTLY in production:

  * a `git add -A` would sweep another session's in-flight work into a release commit. This
    checkout is routinely shared, so that is not hypothetical.
  * an uncommitted change under server/ never reaches Railway, because Railway ships whatever is
    on the branch. The release would look like it worked and the live service would not match.

Nothing here runs git. `deploy()`'s subprocess calls are captured, so the test asserts what it
WOULD have run.
"""
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools", "release"))
import publish  # noqa: E402


@pytest.fixture
def fake_git(monkeypatch):
    """Capture every command deploy() runs, and script `git status` / `git diff --cached`."""
    calls = []
    state = {"status": "", "staged": "hub/version.py", "push_rc": 0}

    def fake_run(cmd, cwd=None, log=None, check=True):
        calls.append(list(cmd))
        return state["push_rc"] if cmd[:2] == ["git", "push"] else 0

    def fake_subprocess_run(cmd, **kw):
        calls.append(list(cmd))
        out = ""
        if cmd[:3] == ["git", "status", "--porcelain"]:
            out = state["status"]
        elif cmd[:2] == ["git", "diff"]:
            out = state["staged"]
        elif cmd[1:3] == ["rev-parse", "--abbrev-ref"]:
            out = "main"
        return types.SimpleNamespace(stdout=out, stderr="", returncode=0)

    monkeypatch.setattr(publish, "run", fake_run)
    monkeypatch.setattr(publish.subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(publish.shutil, "which", lambda n: "git")
    monkeypatch.setattr(publish.os.path, "isdir", lambda p: True)
    monkeypatch.setattr(publish.os.path, "exists", lambda p: True)
    return calls, state


def test_deploy_pushes_and_never_adds_everything(fake_git):
    calls, _ = fake_git
    publish.deploy("Release hub 9.9.9")

    adds = [c for c in calls if c[:2] == ["git", "add"]]
    assert adds, "deploy() staged nothing"
    for c in adds:
        assert "-A" not in c and "." not in c, f"deploy() must never stage everything: {c}"
        assert set(c[3:]) <= set(publish.RELEASE_PATHS), f"staged something unexpected: {c}"

    assert ["git", "commit", "-m", "Release hub 9.9.9"] in calls
    assert ["git", "push", "origin", "main"] in calls
    # The dead path must not come back.
    assert not any("railway" in str(c).lower() for c in calls), calls


def test_deploy_refuses_when_server_has_unrelated_changes(fake_git):
    _, state = fake_git
    state["status"] = " M server/live.cjs\n M server/server.cjs\n"
    with pytest.raises(SystemExit):
        publish.deploy("Release hub 9.9.9")


def test_release_artefacts_under_server_do_not_block_the_deploy(fake_git):
    calls, state = fake_git
    state["status"] = " M server/public/catalogue.json\n M server/public/hub/LightsOut-Setup-9.9.9.exe\n"
    publish.deploy("Release hub 9.9.9")
    assert ["git", "push", "origin", "main"] in calls


def test_a_failed_push_is_fatal(fake_git):
    """A push that fails means nothing deployed — it must not be reported as a release."""
    _, state = fake_git
    state["push_rc"] = 1
    with pytest.raises(SystemExit):
        publish.deploy("Release hub 9.9.9")


def test_installer_receives_matching_numeric_version(monkeypatch):
    calls = []
    monkeypatch.setattr(publish, "run", lambda cmd, **kwargs: calls.append(cmd) or 0)
    monkeypatch.setattr(publish.subprocess, "run", lambda cmd, **kwargs: calls.append(cmd))
    monkeypatch.setattr(publish, "_clear", lambda path: None)
    monkeypatch.setattr(publish.os.path, "isdir", lambda path: False)
    monkeypatch.setattr(publish.os.path, "isfile", lambda path: True)
    monkeypatch.setattr(publish.os.path, "getsize", lambda path: 100)
    publish.build_hub("2.3.43", "iscc-test")
    compiler = next(cmd for cmd in calls if cmd[0] == "iscc-test")
    assert "/DHubVersion=2.3.43" in compiler
    assert "/DHubFileVersion=2.3.43.0" in compiler
    assert not any(cmd[0] == "taskkill" for cmd in calls), "building must not close a player's hub"
