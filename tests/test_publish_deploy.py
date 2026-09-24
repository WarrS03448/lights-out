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
from pathlib import Path
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
    monkeypatch.setattr(publish.os, "makedirs", lambda *args, **kwargs: None)
    monkeypatch.setattr(publish.os.path, "isdir", lambda path: False)
    monkeypatch.setattr(publish.os.path, "isfile", lambda path: True)
    monkeypatch.setattr(publish.os.path, "getsize", lambda path: 100)
    monkeypatch.setattr(publish, "check_installer_signing_evidence", lambda *args: None, raising=False)
    publish.build_hub("2.3.43", "iscc-test")
    compiler = next(cmd for cmd in calls if cmd[0] == "iscc-test")
    assert "/DHubVersion=2.3.43" in compiler
    assert "/DHubFileVersion=2.3.43.0" in compiler
    assert "/DHubSignedRelease=1" in compiler
    callback = next(arg for arg in compiler if arg.startswith("/Slightsout="))
    assert "sign.ps1" in callback and callback.endswith(" -Path $f")
    signs = [cmd for cmd in calls if "-Path" in cmd and cmd[0] == "powershell.exe"]
    assert [cmd[-1] for cmd in signs] == [publish.onedir_exe(), publish.installer_path("2.3.43")]
    assert "-VerifyOnly" in signs[1], "Inno already signed the installer; never sign twice"
    assert calls.index(signs[0]) < calls.index(compiler) < calls.index(signs[1]), \
        "sign the bundled app before packaging, then sign the final installer"
    assert not any(cmd[0] == "taskkill" for cmd in calls), "building must not close a player's hub"


@pytest.mark.parametrize("failure", ["config", "app", "compiler", "installer"])
def test_signing_failure_cannot_update_the_public_download(monkeypatch, failure):
    def run(cmd, **kwargs):
        if cmd[0] == "iscc-test" and failure == "compiler":
            raise SystemExit("signing failed")
        if cmd[0] == "powershell.exe":
            stage = "config" if "-CheckConfiguration" in cmd else (
                "app" if cmd[-1] == publish.onedir_exe() else "installer")
            if stage == failure:
                raise SystemExit("signing failed")
        return 0
    monkeypatch.setattr(publish, "run", run)
    monkeypatch.setattr(publish, "read_hub_version", lambda: "2.3.87")
    monkeypatch.setattr(publish, "find_iscc", lambda: "iscc-test")
    monkeypatch.setattr(publish, "_clear", lambda path: None)
    monkeypatch.setattr(publish.os, "makedirs", lambda *args, **kwargs: None)
    monkeypatch.setattr(publish.os.path, "isdir", lambda path: False)
    monkeypatch.setattr(publish.os.path, "isfile", lambda path: True)
    monkeypatch.setattr(publish.os.path, "getsize", lambda path: 100)
    monkeypatch.setattr(publish, "check_installer_signing_evidence", lambda *args: None, raising=False)
    def unexpected(*args, **kwargs):
        raise AssertionError("a failed signing step must not stage installer/catalogue bytes")
    monkeypatch.setattr(publish.shutil, "copy2", unexpected)
    monkeypatch.setattr(publish, "save_catalogue", unexpected)
    with pytest.raises(SystemExit, match="signing failed"):
        publish.publish_hub(types.SimpleNamespace(version="2.3.87"))


def test_release_does_not_create_a_signed_uninstaller_accepted_by_older_clients():
    """Old updaters trust product/version alone: do not introduce a signed uninstaller.

    Inno defaults SignedUninstaller to yes when SignTool is set, so omission is
    unsafe too. The outer installer must retain its signing callback.
    """
    text = (Path(ROOT) / "hub/installer.iss").read_text(encoding="utf-8")
    settings = {}
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith(";"):
            key, value = line.split("=", 1)
            settings.setdefault(key.strip().lower(), []).append(value.strip().lower())
    assert settings["signtool"] == ["lightsout"]
    assert settings["signeduninstaller"] == ["no"], \
        "a signed Inno uninstaller is accepted by pre-2.8.4 update gates"
    assert "signeduninstallerdir" not in settings


def test_signing_evidence_contains_only_the_exact_final_installer(tmp_path):
    setup = tmp_path / "LightsOut-Setup-2.8.4.exe"
    setup.write_bytes(b"final signed installer")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    with pytest.raises(SystemExit):
        publish.check_installer_signing_evidence(setup, evidence)
    captured = evidence / setup.name
    captured.write_bytes(setup.read_bytes())
    publish.check_installer_signing_evidence(setup, evidence)
    captured.write_bytes(b"different output")
    with pytest.raises(SystemExit):
        publish.check_installer_signing_evidence(setup, evidence)
    captured.write_bytes(setup.read_bytes())
    (evidence / "uninst.e32.tmp").write_bytes(b"unsafe signed internal component")
    with pytest.raises(SystemExit):
        publish.check_installer_signing_evidence(setup, evidence)
