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
import shutil
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools", "release"))
import publish  # noqa: E402

REAL_RUN = publish.run          # linux_site forbids run(); one test puts the real one back


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


# ---------------------------------------------------------------- publish.py linux
# Tiny stand-ins with the real layout: build_bundle's player ZIP (Unix S_IFREG/S_IFDIR members) and
# report, build_source_bundle's source ZIP and sign_update's {key_id, manifest, signature}. The record
# is really signed (RFC 8032, below) with a test key that the ZIP's own update_trust_linux.py pins,
# exactly as sign_update does it with the production key.
import base64  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import stat  # noqa: E402
import subprocess  # noqa: E402
import zipfile  # noqa: E402

import linux_update_trust as trust  # noqa: E402

LINUX_WINDOWS_HUB = {"version": "3.0.3", "download_url": "https://play.lightsoutranked.com/hub/LightsOut-Setup-3.0.3.exe",
                     "page_url": "https://lightsoutranked.com/", "sha256": "5" * 64, "size": 21514304,
                     "kind": "inno-setup"}
# Test signing keys (32-byte Ed25519 seeds). Never production keys: those live outside every repository.
PRIMARY, SPARE, NEWKEY, STRANGER = (bytes([n]) * 32 for n in (1, 2, 3, 4))


def ed25519_sign(seed, message):
    """(public key, signature): RFC 8032 section 5.1.6, as sign_update.pure_sign does it."""
    digest = hashlib.sha512(seed).digest()
    scalar = (int.from_bytes(digest[:32], "little") & ((1 << 254) - 8)) | (1 << 254)
    public = trust._encode(trust._multiply(scalar, trust._BASE))
    nonce = trust._challenge(digest[32:], message)
    commitment = trust._encode(trust._multiply(nonce, trust._BASE))
    s = (nonce + trust._challenge(commitment, public, message) * scalar) % trust._L
    return public, commitment + s.to_bytes(32, "little")


def public_of(seed):
    return ed25519_sign(seed, b"")[0]


def key_id_of(seed):
    return trust.key_id(public_of(seed))


def _client_rules():
    """The client's rules as linux_update_trust.py copies them, without its (always empty) PINNED_KEYS."""
    text = Path(trust.__file__).read_text(encoding="utf-8")
    text = text[:text.index("\n# ---------------------------------------------------------------- this file's own")]
    assert text.count("\nPINNED_KEYS = {}\n") == 1
    return text.replace("\nPINNED_KEYS = {}\n", "\n")


CLIENT_RULES = _client_rules()


def trust_module(*seeds, rules=CLIENT_RULES):
    """A runtime hub/update_trust_linux.py: the client's rules, pinning these keys as Sam pastes
    sign_update's pin lines."""
    pins = "".join(f"    {key_id_of(s)!r}: bytes.fromhex({public_of(s).hex()!r}),\n" for s in seeds)
    return f"{rules}\n\nPINNED_KEYS = {{\n{pins}}}\n"


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _zip(path, members):
    """Members as build_bundle writes them: Unix (create_system 3), S_IFDIR for names ending in "/",
    S_IFREG otherwise. A value (data, mode, create_system) sets the member's type bits and system.
    Stored, not deflated: a test can then damage one member's bytes in place."""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as z:
        for name, data in members.items():
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            if isinstance(data, tuple):
                data, mode, info.create_system = data
            else:
                mode, info.create_system = (stat.S_IFDIR | 0o755 if name.endswith("/") else stat.S_IFREG | 0o644), 3
            info.external_attr = mode << 16 | (0x10 if stat.S_ISDIR(mode) else 0)
            z.writestr(info, data)


def make_linux_release(directory, version="3.0.3", *, name_version=None, runtime_version=None,
                       report_edit=None, manifest_edit=None, record_edit=None, player_extra=None,
                       bundle_edit=None, source_manifest_edit=None, player_bytes_edit=None, drop_member=None,
                       source_bytes_edit=None, source_extra=None, signer=PRIMARY, pins=(PRIMARY,),
                       trust_source=None, manifest_bytes_edit=None):
    """Write the four release files for `version` into `directory`; the *_edit hooks tamper with one field
    (player_bytes_edit changes the ZIP's bytes BEFORE the report and manifest hash them; manifest_edit and
    manifest_bytes_edit change the manifest BEFORE it is signed; record_edit changes the signed record).
    The ZIP's runtime pins `pins` (or holds `trust_source`), and `signer` signs.
    `name_version` puts another version in the four file names only."""
    directory.mkdir(parents=True, exist_ok=True)
    named = name_version or version
    folder = "LightsOut-Linux-Native-" + version
    launcher = b"\x7fELF static launcher stand-in"
    payload = json.dumps({"hub/version.py": "0" * 64}).encode()
    bundle_record = {"schema": 1, "release": version,
                     "runtime": {"payload_sha256": _sha(payload), "files": {}},
                     "launcher": {"sha256": _sha(launcher), "size": len(launcher), "mode": 493}}
    if bundle_edit:
        bundle_edit(bundle_record)
    bundle = (json.dumps(bundle_record, sort_keys=True, indent=1) + "\n").encode()
    members = {
        folder + "/": b"",
        folder + "/Install and Open Lights Out": (launcher, stat.S_IFREG | 0o755, 3),
        folder + "/READ ME.txt": b"Lights Out - native Linux beta\n",
        folder + "/support/": b"",
        folder + "/support/start-setup": (b"#!/bin/sh\n", stat.S_IFREG | 0o755, 3),
        folder + "/support/native_setup.py": b"# setup\n",
        folder + "/support/portable_setup.py": b"# setup helpers\n",
        folder + "/support/bundle.json": bundle,
        folder + "/support/native-launcher": (launcher, stat.S_IFREG | 0o755, 3),
        folder + "/support/runtime/payload.json": payload,
        folder + "/support/runtime/hub/version.py": f'HUB_VERSION = "{runtime_version or version}"\n'.encode(),
        folder + "/support/runtime/hub/update_trust_linux.py": (trust_source or trust_module(*pins)).encode(),
    }
    members.update(player_extra or {})
    members.pop(folder + "/" + (drop_member or "-"), None)
    player = directory / f"LightsOut-Linux-Native-{named}-x86_64.zip"
    _zip(player, members)
    if player_bytes_edit:
        player.write_bytes(player_bytes_edit(player.read_bytes()))
    source = directory / f"LightsOut-Linux-Native-{named}-source.zip"
    source_manifest = {"schema": 1, "release": version, "source_commit": "c" * 40}
    if source_manifest_edit:
        source_manifest_edit(source_manifest)
    source_members = {"README.txt": b"corresponding source\n"}
    if source_manifest is not None and source_manifest != {}:
        source_members["SOURCE-MANIFEST.json"] = json.dumps(source_manifest).encode()
    source_members.update(source_extra or {})
    _zip(source, source_members)
    if source_bytes_edit:
        source.write_bytes(source_bytes_edit(source.read_bytes()))
    p, s = player.read_bytes(), source.read_bytes()
    report = {"schema": 1, "release": version, "hub_version": version, "publishable": True,
              "archive": player.name, "download_bytes": len(p), "download_sha256": _sha(p),
              "installed_bytes": 66794479, "runtime_payload_sha256": _sha(payload),
              "launcher": {"sha256": _sha(launcher), "bytes": len(launcher)}, "runtime_files": 3,
              "source": {"name": source.name, "bytes": len(s), "sha256": _sha(s)},
              "claim": "Bundle measurement only; managed data peaks are separate."}
    parts = [int(x) for x in version.split(".")]
    manifest = {"schema": 1, "product": "Lights Out", "role": "linux-native-update", "platform": "linux",
                "arch": "x86_64", "channel": "public-beta", "version": (parts + [0, 0, 0, 0])[:4],
                "display_version": version, "release": version, "folder": folder,
                "archive": {"name": player.name, "size": len(p), "sha256": _sha(p)},
                "source": {"name": source.name, "size": len(s), "sha256": _sha(s)},
                "bundle_sha256": _sha(bundle), "payload_sha256": _sha(payload),
                "launcher_sha256": _sha(launcher), "installed_bytes": 66794479}
    if report_edit:
        report_edit(report)
    if manifest_edit:
        manifest_edit(manifest)
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    if manifest_bytes_edit:
        raw = manifest_bytes_edit(raw)
    _public, signature = ed25519_sign(signer, trust.DOMAIN + raw)
    record = {"key_id": key_id_of(signer), "manifest": base64.b64encode(raw).decode(),
              "signature": base64.b64encode(signature).decode()}
    if record_edit:
        record_edit(record)
    (directory / f"LightsOut-Linux-Native-{named}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (directory / f"LightsOut-Linux-Native-{named}.update.json").write_text(json.dumps(record), encoding="utf-8")
    return {"player": player, "source": source, "folder": folder, "record": record}


def installed_linux(site, version, **options):
    """Make `version` the published Linux release, as publish_linux leaves it: its player ZIP in
    server/public/hub/ and catalogue.linux naming it. (Written directly, so it may be behind Windows.)"""
    made = make_linux_release(site.root / ("published-" + version), version, **options)
    (site.hub / made["player"].name).write_bytes(made["player"].read_bytes())
    data = made["player"].read_bytes()
    _set_catalogue(site, lambda c: c.update(linux={
        "version": version, "kind": "linux-native-zip", "arch": "x86_64",
        "download_url": "https://play.lightsoutranked.com/hub/" + made["player"].name,
        "size": len(data), "sha256": _sha(data), "update": made["record"]}))
    shutil.rmtree(site.root / ("published-" + version))
    return made


def _real_release_state():
    hub = Path(ROOT) / "server/public/hub"
    return (Path(ROOT, "server/public/catalogue.json").read_bytes(),
            sorted(p.name for p in hub.iterdir()) if hub.is_dir() else None)


@pytest.fixture
def linux_site(tmp_path, monkeypatch):
    """A throwaway checkout: catalogue, server/public/hub with the Windows installer, hub/version.py.
    run() and write_hub_version() raise, so a Linux release that builds, signs or bumps anything fails.
    Afterwards the REAL catalogue and server/public/hub must be exactly as they were."""
    real = _real_release_state()
    yield from _linux_site(tmp_path, monkeypatch)
    assert _real_release_state() == real, "a Linux publish test wrote into the real checkout"


def _linux_site(tmp_path, monkeypatch):
    root = tmp_path / "site"
    public = root / "server/public"
    (public / "hub").mkdir(parents=True)
    (public / "hub/LightsOut-Setup-3.0.3.exe").write_bytes(b"windows installer")
    (public / "hub/.gitkeep").write_bytes(b"")
    (root / "hub").mkdir()
    (root / "hub/version.py").write_text('HUB_VERSION = "3.0.3"\n', encoding="utf-8")
    catalogue = {"catalogue_version": 1, "hub": dict(LINUX_WINDOWS_HUB),
                 "gamemodes": [{"id": "BB5", "version": "1.0.30",
                                "pack_url": "https://play.lightsoutranked.com/packs/BB5-1.0.30.zip",
                                "sha256": "b" * 64, "size": 10}]}
    (public / "catalogue.json").write_text(json.dumps(catalogue, indent=2), encoding="utf-8")
    monkeypatch.setattr(publish, "ROOT", str(root))
    monkeypatch.setattr(publish, "PUBLIC", str(public))
    monkeypatch.setattr(publish, "CATALOGUE", str(public / "catalogue.json"))
    monkeypatch.setattr(publish, "VERSION_PY", str(root / "hub/version.py"))

    def forbidden(*args, **kwargs):
        raise AssertionError(f"a Linux release must not run, build or sign anything: {args}")
    monkeypatch.setattr(publish, "run", forbidden)
    monkeypatch.setattr(publish, "write_hub_version", forbidden)
    yield types.SimpleNamespace(root=root, public=public, hub=public / "hub", release=tmp_path / "release")


def linux_args(directory, **kw):
    base = dict(dir=directory, no_deploy=True, required=False, source_url=None, allow_behind_windows=False,
                break_in_app_updates=False)
    base.update(kw)
    return types.SimpleNamespace(**base)


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def refused(site, capsys, args, reason):
    """The release is refused with `reason`, and nothing in the checkout changed."""
    before = snapshot(site.root)
    with pytest.raises(SystemExit):
        publish.publish_linux(args)
    out = capsys.readouterr().out
    assert "Linux release refused" in out and reason in out, out
    assert snapshot(site.root) == before


def _set_catalogue(site, edit):
    path = site.public / "catalogue.json"
    c = json.loads(path.read_text(encoding="utf-8"))
    edit(c)
    path.write_text(json.dumps(c, indent=2), encoding="utf-8")


def test_linux_release_writes_only_the_linux_entry_and_its_zips(linux_site, capsys):
    site = linux_site
    made = make_linux_release(site.release)
    (site.hub / "LightsOut-Linux-Native-3.0.2-x86_64.zip").write_bytes(b"old player zip")
    (site.hub / "LightsOut-Linux-Native-3.0.2-source.zip").write_bytes(b"old source zip")
    before = json.loads((site.public / "catalogue.json").read_text(encoding="utf-8"))
    version_py = (site.root / "hub/version.py").read_bytes()

    assert publish.publish_linux(linux_args(site.release)) == "3.0.3"

    after = json.loads((site.public / "catalogue.json").read_text(encoding="utf-8"))
    assert after["hub"] == before["hub"] and after["gamemodes"] == before["gamemodes"]
    assert (site.root / "hub/version.py").read_bytes() == version_py
    p, s = made["player"].read_bytes(), made["source"].read_bytes()
    record = json.loads((site.release / "LightsOut-Linux-Native-3.0.3.update.json").read_text(encoding="utf-8"))
    assert after["linux"] == {
        "version": "3.0.3", "kind": "linux-native-zip", "arch": "x86_64",
        "download_url": "https://play.lightsoutranked.com/hub/LightsOut-Linux-Native-3.0.3-x86_64.zip",
        "size": len(p), "sha256": _sha(p),
        "source_url": "https://play.lightsoutranked.com/hub/LightsOut-Linux-Native-3.0.3-source.zip",
        "source_size": len(s), "source_sha256": _sha(s),
        "page_url": "https://lightsoutranked.com/", "required": False, "update": record}
    assert (site.hub / made["player"].name).read_bytes() == p
    assert (site.hub / made["source"].name).read_bytes() == s
    assert sorted(x.name for x in site.hub.iterdir()) == sorted(
        [".gitkeep", "LightsOut-Setup-3.0.3.exe", made["player"].name, made["source"].name])
    assert "removed old LightsOut-Linux-Native-3.0.2-x86_64.zip" in capsys.readouterr().out


def test_linux_release_can_be_required_and_four_part(linux_site):
    make_linux_release(linux_site.release, "3.0.3.1")
    publish.publish_linux(linux_args(linux_site.release, required=True))
    linux = publish.load_catalogue()["linux"]
    assert linux["version"] == "3.0.3.1" and linux["required"] is True


def test_linux_release_with_external_source_copies_only_the_player_zip(linux_site):
    site = linux_site
    made = make_linux_release(site.release)
    url = "https://github.com/WarrS03448/lights-out/releases/download/linux-v3.0.3/" + made["source"].name
    publish.publish_linux(linux_args(site.release, source_url=url))
    assert publish.load_catalogue()["linux"]["source_url"] == url
    assert not (site.hub / made["source"].name).exists()
    assert (site.hub / made["player"].name).exists()


def _redo_manifest(record):
    manifest = base64.b64decode(record["manifest"])
    record["manifest"] = base64.b64encode(manifest[:-1] + b',"schema":1}').decode()


def _backslash(raw):
    """zipfile on Windows rewrites a backslash in a new member's name to "/", so write "|" and patch the bytes."""
    return raw.replace(b"support|evil.sh", b"support\\evil.sh")


def _stray_padding_bits(record):
    """The same 64 signature bytes in a second base64 spelling (a padding bit set): the client accepts one."""
    text = record["signature"]
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    record["signature"] = text[:-3] + alphabet[alphabet.index(text[-3]) ^ 1] + "=="
    assert text.endswith("==") and base64.b64decode(record["signature"]) == base64.b64decode(text)


def _resigned(seed):
    """Replace the signature with one by `seed` over the same bytes, keeping the record's key_id."""
    def edit(record):
        raw = base64.b64decode(record["manifest"])
        record["signature"] = base64.b64encode(ed25519_sign(seed, trust.DOMAIN + raw)[1]).decode()
    return edit


def _spaced(raw):
    return json.dumps(json.loads(raw), sort_keys=True).encode()          # ", " and ": ": not canonical


def _unsorted(raw):
    return json.dumps(dict(reversed(list(json.loads(raw).items()))), separators=(",", ":")).encode()


F = "LightsOut-Linux-Native-3.0.3"
NON_HEX = "Z" * 64


REFUSALS = {
    # V has the X.Y.Z or X.Y.Z.N format.
    "two-part version": (dict(name_version="3.0"), "not X.Y.Z or X.Y.Z.N"),
    "five-part version": (dict(name_version="3.0.3.1.2"), "not X.Y.Z or X.Y.Z.N"),
    "leading zero": (dict(name_version="3.0.03"), "not X.Y.Z or X.Y.Z.N"),
    "suffix": (dict(name_version="3.0.3-beta"), "not X.Y.Z or X.Y.Z.N"),
    "part over the client's 65535": (dict(name_version="3.0.65536"), "not X.Y.Z or X.Y.Z.N"),
    # The player ZIP has a size in range.
    "zero-byte player": (dict(player_bytes_edit=lambda b: b""), "is 0 bytes; the download limit"),
    # V equals the report's release and hub_version; report.publishable is true.
    "report release": (dict(report_edit=lambda r: r.update(release="3.0.4")), "report's release"),
    "report hub_version": (dict(report_edit=lambda r: r.update(hub_version="3.0.2")), "report's release"),
    "report without hub_version": (dict(report_edit=lambda r: r.pop("hub_version")), "report's release"),
    "dev build": (dict(report_edit=lambda r: r.update(publishable=False)), "not publishable"),
    "publishable missing": (dict(report_edit=lambda r: r.pop("publishable")), "not publishable"),
    "publishable truthy": (dict(report_edit=lambda r: r.update(publishable=1)), "not publishable"),
    "installed over the limit": (dict(report_edit=lambda r: r.update(installed_bytes=100_000_001),
                                      manifest_edit=lambda m: m.update(installed_bytes=100_000_001)),
                                 "above the 100,000,000-byte limit"),
    "installed missing": (dict(report_edit=lambda r: r.pop("installed_bytes")), "installed_bytes is missing"),
    # The recomputed size and sha256 of both ZIPs equal the report...
    "report zip size": (dict(report_edit=lambda r: r.update(download_bytes=r["download_bytes"] + 1)),
                        "x86_64.zip's size or sha256 differs from the report"),
    "report zip sha": (dict(report_edit=lambda r: r.update(download_sha256="0" * 64)),
                       "x86_64.zip's size or sha256 differs from the report"),
    "report zip name": (dict(report_edit=lambda r: r.update(archive="other.zip")),
                        "x86_64.zip's size or sha256 differs from the report"),
    "report source size": (dict(report_edit=lambda r: r["source"].update(bytes=1)),
                           "source.zip's size or sha256 differs from the report"),
    "report source sha": (dict(report_edit=lambda r: r["source"].update(sha256="0" * 64)),
                          "source.zip's size or sha256 differs from the report"),
    "report without source": (dict(report_edit=lambda r: r.update(source=None)),
                              "source.zip's size or sha256 differs from the report"),
    # ...and the decoded signed manifest.
    "manifest zip size": (dict(manifest_edit=lambda m: m["archive"].update(size=1)),
                          "x86_64.zip's size or sha256 differs from the signed manifest"),
    "manifest zip sha": (dict(manifest_edit=lambda m: m["archive"].update(sha256="0" * 64)),
                         "x86_64.zip's size or sha256 differs from the signed manifest"),
    "manifest source size": (dict(manifest_edit=lambda m: m["source"].update(size=1)),
                             "source.zip's size or sha256 differs from the signed manifest"),
    "manifest source sha": (dict(manifest_edit=lambda m: m["source"].update(sha256="0" * 64)),
                            "source.zip's size or sha256 differs from the signed manifest"),
    "manifest display_version": (dict(manifest_edit=lambda m: m.update(display_version="3.0.4")),
                                 "display_version is '3.0.4'"),
    "manifest platform": (dict(manifest_edit=lambda m: m.update(platform="windows")), "platform is 'windows'"),
    "manifest role": (dict(manifest_edit=lambda m: m.update(role="windows-installer")), "role is"),
    "manifest arch": (dict(manifest_edit=lambda m: m.update(arch="aarch64")), "arch is 'aarch64'"),
    "manifest product": (dict(manifest_edit=lambda m: m.update(product="Other")), "product is"),
    "manifest channel": (dict(manifest_edit=lambda m: m.update(channel="stable")), "channel is"),
    "manifest schema as bool": (dict(manifest_edit=lambda m: m.update(schema=True)), "schema is True"),
    "manifest release": (dict(manifest_edit=lambda m: m.update(release="3.0.4")), "release is '3.0.4'"),
    "manifest folder": (dict(manifest_edit=lambda m: m.update(folder="elsewhere")), "folder is"),
    "manifest version tuple": (dict(manifest_edit=lambda m: m.update(version=[3, 0, 4, 0])), "manifest's version"),
    "manifest version bool": (dict(manifest_edit=lambda m: m.update(version=[3, False, 3, 0])), "manifest's version"),
    "manifest version three": (dict(manifest_edit=lambda m: m.update(version=[3, 0, 3])), "manifest's version"),
    "manifest bundle": (dict(manifest_edit=lambda m: m.update(bundle_sha256="0" * 64)), "bundle_sha256"),
    "manifest payload": (dict(manifest_edit=lambda m: m.update(payload_sha256="0" * 64)), "payload_sha256"),
    "manifest launcher": (dict(manifest_edit=lambda m: m.update(launcher_sha256="0" * 64)), "launcher_sha256"),
    "manifest installed": (dict(manifest_edit=lambda m: m.update(installed_bytes=1)), "installed_bytes"),
    "manifest extra key": (dict(manifest_edit=lambda m: m.update(extra=1)), "exactly the signed update keys"),
    "manifest missing key": (dict(manifest_edit=lambda m: m.pop("channel")), "exactly the signed update keys"),
    "manifest installed as bool": (dict(report_edit=lambda r: r.update(installed_bytes=1),
                                        manifest_edit=lambda m: m.update(installed_bytes=True)),
                                   "installed_bytes differs from the report"),
    "payload not hex anywhere": (dict(report_edit=lambda r: r.update(runtime_payload_sha256=NON_HEX),
                                      manifest_edit=lambda m: m.update(payload_sha256=NON_HEX),
                                      bundle_edit=lambda b: b["runtime"].update(payload_sha256=NON_HEX)),
                                 "payload_sha256 is not a SHA-256"),
    "launcher not hex anywhere": (dict(report_edit=lambda r: r["launcher"].update(sha256=NON_HEX),
                                       manifest_edit=lambda m: m.update(launcher_sha256=NON_HEX)),
                                  "launcher_sha256 is not a SHA-256"),
    # The Linux client's own rules for the signed manifest: canonical bytes and integers only.
    "manifest with spaces": (dict(manifest_bytes_edit=_spaced), "not in canonical form"),
    "manifest keys unsorted": (dict(manifest_bytes_edit=_unsorted), "not in canonical form"),
    "manifest float installed": (dict(manifest_edit=lambda m: m.update(installed_bytes=66794479.0)),
                                 "is not an integer"),
    "manifest float size": (dict(manifest_edit=lambda m: m["archive"].update(size=float(m["archive"]["size"]))),
                            "is not an integer"),
    # The signed update record itself.
    "record extra key": (dict(record_edit=lambda r: r.update(note="x")), "exactly key_id, manifest and signature"),
    "record bad base64": (dict(record_edit=lambda r: r.update(manifest="not base64!")), "not valid base64"),
    "record short signature": (dict(record_edit=lambda r: r.update(
        signature=base64.b64encode(b"x" * 63).decode())), "not 64 bytes"),
    "record empty key_id": (dict(record_edit=lambda r: r.update(key_id="")), "invalid key_id"),
    "record manifest not a string": (dict(record_edit=lambda r: r.update(manifest=5)), "not a base64 string"),
    "record duplicate manifest key": (dict(record_edit=_redo_manifest), "duplicate key"),
    "record oversized manifest": (dict(record_edit=lambda r: r.update(
        manifest=base64.b64encode(b" " * (16 * 1024 + 1)).decode())), "larger than 16384"),
    "record too large": (dict(record_edit=lambda r: r.update(key_id="k" * 70000)), "larger than 65536"),
    "record key_id not hex": (dict(record_edit=lambda r: r.update(key_id="test-key-1")), "invalid key_id"),
    "record key_id 129 characters": (dict(record_edit=lambda r: r.update(key_id="k" * 129)), "invalid key_id"),
    "record key_id 17 digits": (dict(record_edit=lambda r: r.update(key_id=r["key_id"] + "0")), "invalid key_id"),
    "record key_id upper case": (dict(record_edit=lambda r: r.update(key_id=r["key_id"].upper())), "invalid key_id"),
    # The signature, under the keys the new ZIP pins.
    "signed with a key the ZIP does not pin": (dict(signer=STRANGER), f"pins only {key_id_of(PRIMARY)}"),
    "signature by another key": (dict(record_edit=_resigned(STRANGER)), "does not verify under key"),
    "signature in a second base64 spelling": (dict(record_edit=_stray_padding_bits), "by the client's own rules"),
    "ZIP pins no key": (dict(pins=()), "pins no update key"),
    "no update_trust_linux.py": (dict(drop_member="support/runtime/hub/update_trust_linux.py"),
                                 "lacks support/runtime/hub/update_trust_linux.py, so it trusts no update key"),
    "PINNED_KEYS computed": (dict(trust_source="PINNED_KEYS = load_keys()\n"), "no readable PINNED_KEYS"),
    "PINNED_KEYS twice": (dict(trust_source=trust_module(PRIMARY) + "PINNED_KEYS = {}\n"), "no readable PINNED_KEYS"),
    "pinned key_id names another key": (dict(trust_source="PINNED_KEYS = {'0123456789abcdef': bytes.fromhex(%r)}\n"
                                                          % public_of(PRIMARY).hex()), "no readable PINNED_KEYS"),
    # The player ZIP's own claims.
    "runtime HUB_VERSION": (dict(runtime_version="3.0.2"), "HUB_VERSION '3.0.2'"),
    "member outside the folder": (dict(player_extra={"elsewhere/evil.sh": b"x"}), "outside"),
    "member climbing out": (dict(player_extra={"LightsOut-Linux-Native-3.0.3/../evil.sh": b"x"}), "outside"),
    "member with a backslash": (dict(player_extra={F + "/support|evil.sh": b"x"}, player_bytes_edit=_backslash),
                                "outside"),
    "member with an empty part": (dict(player_extra={F + "/support//evil.sh": b"x"}), "outside"),
    "symlink member": (dict(player_extra={F + "/support/link": (b"../../../..", stat.S_IFLNK | 0o777, 3)}),
                       "not a plain file or folder: 'LightsOut-Linux-Native-3.0.3/support/link'"),
    "FIFO member": (dict(player_extra={F + "/support/fifo": (b"", stat.S_IFIFO | 0o644, 3)}),
                    "not a plain file or folder"),
    "folder member without its slash": (dict(player_extra={F + "/support/dir": (b"", stat.S_IFDIR | 0o755, 3)}),
                                        "not a plain file or folder"),
    "file member named like a folder": (dict(player_extra={F + "/support/dir/": (b"", stat.S_IFREG | 0o644, 3)}),
                                        "not a plain file or folder"),
    "member from an MS-DOS system": (dict(player_extra={F + "/support/dos.txt": (b"x", stat.S_IFREG | 0o644, 0)}),
                                     "not a plain file or folder"),
    "no start-setup": (dict(drop_member="support/start-setup"), "lacks support/start-setup"),
    "no runtime version": (dict(drop_member="support/runtime/hub/version.py"), "lacks support/runtime/hub/version.py"),
    "bundle of another release": (dict(bundle_edit=lambda b: b.update(release="3.0.2")),
                                  "support/bundle.json is not release 3.0.3"),
    "bundle of another runtime": (dict(bundle_edit=lambda b: b["runtime"].update(payload_sha256="0" * 64)),
                                  "names another runtime payload"),
    "damaged member": (dict(player_bytes_edit=lambda b: b.replace(b"native Linux beta", b"native Linux BETA")),
                       "is damaged at LightsOut-Linux-Native-3.0.3/READ ME.txt"),
    "player not a zip": (dict(player_bytes_edit=lambda b: b"not a zip at all"), "is not a readable ZIP"),
    # The source ZIP's own claims.
    "source of another release": (dict(source_manifest_edit=lambda m: m.update(release="3.0.2")),
                                  "SOURCE-MANIFEST.json is not release 3.0.3"),
    "source without manifest": (dict(source_manifest_edit=lambda m: m.clear()), "has no SOURCE-MANIFEST.json"),
    "damaged source": (dict(source_bytes_edit=lambda b: b.replace(b"corresponding source", b"CORRESPONDING source")),
                       "source.zip is damaged at README.txt"),
    "source not a zip": (dict(source_bytes_edit=lambda b: b"not a zip at all"), "source.zip is not a readable ZIP"),
    "source member climbing out": (dict(source_extra={"../evil.sh": b"x"}), "source.zip has a member outside"),
    "source symlink member": (dict(source_extra={"source/link": (b"/etc", stat.S_IFLNK | 0o777, 3)}),
                              "source.zip has a member that is not a plain file or folder"),
}


@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_linux_release_refusals(linux_site, capsys, case):
    options, reason = REFUSALS[case]
    make_linux_release(linux_site.release, "3.0.3", **options)
    refused(linux_site, capsys, linux_args(linux_site.release), reason)


def test_linux_release_needs_exactly_one_of_each_file(linux_site, capsys):
    site = linux_site
    make_linux_release(site.release)
    (site.release / "LightsOut-Linux-Native-3.0.3.update.json").unlink()
    refused(site, capsys, linux_args(site.release), "lacks the update file")
    make_linux_release(site.release)
    make_linux_release(site.release / "other", "3.0.4")
    (site.release / "other/LightsOut-Linux-Native-3.0.4-x86_64.zip").replace(
        site.release / "LightsOut-Linux-Native-3.0.4-x86_64.zip")
    refused(site, capsys, linux_args(site.release), "more than one player file")
    (site.release / "LightsOut-Linux-Native-3.0.4-x86_64.zip").unlink()
    (site.release / "LightsOut-Linux-Native-3.0.3.json").replace(site.release / "LightsOut-Linux-Native-3.0.4.json")
    refused(site, capsys, linux_args(site.release), "different versions")
    (site.release / "LightsOut-Linux-Native-3.0.4.json").replace(site.release / "LightsOut-Linux-Native-3.0.3.json")
    (site.release / "LightsOut-Linux-Native-3.0.3.notes.txt").write_text("stray", encoding="utf-8")
    refused(site, capsys, linux_args(site.release), "not one of the four")
    (site.release / "LightsOut-Linux-Native-3.0.3.notes.txt").unlink()
    refused(site, capsys, linux_args(site.root / "missing"), "is not a folder")
    publish.publish_linux(linux_args(site.release))       # the same folder, now complete, publishes


def test_linux_release_refuses_unreadable_inputs(linux_site, capsys, monkeypatch):
    site = linux_site
    make_linux_release(site.release)
    report = site.release / "LightsOut-Linux-Native-3.0.3.json"
    good = report.read_bytes()
    report.write_text("{not json", encoding="utf-8")
    refused(site, capsys, linux_args(site.release), "is not valid JSON")
    report.write_text("[]", encoding="utf-8")
    refused(site, capsys, linux_args(site.release), "is not a build report")
    report.write_bytes(good)
    (site.release / "LightsOut-Linux-Native-3.0.3.update.json").write_text("[]", encoding="utf-8")
    refused(site, capsys, linux_args(site.release), "exactly key_id, manifest and signature")
    make_linux_release(site.release)
    with monkeypatch.context() as limited:              # never monkeypatch.undo(): it would undo linux_site too
        limited.setattr(publish, "LINUX_LIMIT", 100)
        refused(site, capsys, linux_args(site.release), "the download limit is 100")
    shutil_copy = publish.shutil.copyfile

    def bad_copy(src, dst):
        shutil_copy(src, dst)
        with open(dst, "ab") as f:
            f.write(b"!")
    monkeypatch.setattr(publish.shutil, "copyfile", bad_copy)
    before = snapshot(site.root)
    with pytest.raises(SystemExit):
        publish.publish_linux(linux_args(site.release))
    assert "does not match the file that was checked" in capsys.readouterr().out
    assert snapshot(site.root) == before


def test_linux_release_refuses_duplicate_zip_members(linux_site, capsys):
    """Two members under one name: extractors disagree about which one wins."""
    def duplicate(raw):
        path = linux_site.release / "dup.tmp"
        path.write_bytes(raw)
        with pytest.warns(UserWarning), zipfile.ZipFile(path, "a") as z:
            z.writestr("LightsOut-Linux-Native-3.0.3/support/start-setup", b"#!/bin/sh\nexit 1\n")
        data = path.read_bytes()
        path.unlink()
        return data
    make_linux_release(linux_site.release, player_bytes_edit=duplicate)
    refused(linux_site, capsys, linux_args(linux_site.release), "duplicate member names")


def test_linux_release_file_must_be_a_regular_file(linux_site, capsys):
    make_linux_release(linux_site.release)
    (linux_site.release / "LightsOut-Linux-Native-3.0.3.json").unlink()
    (linux_site.release / "LightsOut-Linux-Native-3.0.3.json").mkdir()
    refused(linux_site, capsys, linux_args(linux_site.release), "is not a regular file")


def test_linux_release_refuses_an_unreadable_windows_version(linux_site, capsys):
    _set_catalogue(linux_site, lambda c: c["hub"].update(version="beta"))
    make_linux_release(linux_site.release)
    refused(linux_site, capsys, linux_args(linux_site.release), "hub.version 'beta' is unreadable")


@pytest.mark.parametrize("published", ["3.0.3", "3.0.3.0", "3.0.4", "3.1.0"])
def test_linux_release_must_be_strictly_newer_than_the_published_linux(linux_site, capsys, published):
    _set_catalogue(linux_site, lambda c: c.update(linux={"version": published}))
    make_linux_release(linux_site.release, "3.0.3")
    refused(linux_site, capsys, linux_args(linux_site.release), "is not newer than the published Linux")


def test_linux_release_refuses_an_unreadable_published_linux_version(linux_site, capsys):
    _set_catalogue(linux_site, lambda c: c.update(linux={"version": "beta"}))
    make_linux_release(linux_site.release, "3.0.3")
    refused(linux_site, capsys, linux_args(linux_site.release), "linux.version 'beta'")


def test_linux_release_replaces_an_older_linux_entry(linux_site):
    installed_linux(linux_site, "3.0.2")
    make_linux_release(linux_site.release, "3.0.3")
    assert publish.publish_linux(linux_args(linux_site.release)) == "3.0.3"
    make_linux_release(linux_site.release / "next", "3.0.3.1")
    assert publish.publish_linux(linux_args(linux_site.release / "next")) == "3.0.3.1"
    assert sorted(p.name for p in linux_site.hub.glob("LightsOut-Linux-Native-*")) == [
        "LightsOut-Linux-Native-3.0.3.1-source.zip", "LightsOut-Linux-Native-3.0.3.1-x86_64.zip"]


def test_linux_release_behind_windows_needs_the_platform_gate(linux_site, capsys):
    site = linux_site
    _set_catalogue(site, lambda c: c["hub"].update(version="3.0.4"))
    make_linux_release(site.release, "3.0.3.1")
    refused(site, capsys, linux_args(site.release), "older than the Windows hub 3.0.4")
    refused(site, capsys, linux_args(site.release, allow_behind_windows=True), "per-platform gate")
    (site.root / "server/live.cjs").write_text("capabilities: { hub_platform_gate_v1: true }", encoding="utf-8")
    assert publish.publish_linux(linux_args(site.release, allow_behind_windows=True)) == "3.0.3.1"


def test_linux_release_at_or_ahead_of_windows_needs_no_flag(linux_site):
    make_linux_release(linux_site.release, "3.0.3.2")
    assert publish.publish_linux(linux_args(linux_site.release)) == "3.0.3.2"


def test_linux_release_never_changes_bytes_behind_a_served_name(linux_site, capsys):
    site = linux_site
    made = make_linux_release(site.release)
    (site.hub / made["source"].name).write_bytes(b"a different source zip")
    refused(site, capsys, linux_args(site.release), "already exists with different bytes or as a link")
    (site.hub / made["source"].name).write_bytes(made["source"].read_bytes())   # identical bytes are fine
    publish.publish_linux(linux_args(site.release))
    assert (site.hub / made["source"].name).read_bytes() == made["source"].read_bytes()


def test_linux_release_refuses_a_name_git_once_served_with_other_bytes(linux_site, capsys):
    site = linux_site

    def git(*a):
        subprocess.run(["git", "-C", str(site.root), "-c", "user.name=T", "-c", "user.email=t@example.test", *a],
                       check=True, capture_output=True)
    git("init", "-q")
    name = "LightsOut-Linux-Native-3.0.3-x86_64.zip"
    (site.hub / name).write_bytes(b"an earlier build under the same name")
    git("add", "-A")
    git("commit", "-qm", "earlier")
    (site.hub / name).unlink()
    git("add", "-A")
    git("commit", "-qm", "removed")
    make_linux_release(site.release)
    outside_git = lambda: {k: v for k, v in snapshot(site.root).items() if not k.startswith(".git/")}  # noqa: E731
    before = outside_git()
    with pytest.raises(SystemExit):
        publish.publish_linux(linux_args(site.release))
    assert "was published before with different bytes" in capsys.readouterr().out
    assert outside_git() == before
    # The same bytes that were served before under that name are fine (re-publishing after a revert).
    (site.hub / name).write_bytes((site.release / name).read_bytes())
    git("add", "-A")
    git("commit", "-qm", "the release, reverted below")
    (site.hub / name).unlink()
    git("add", "-A")
    git("commit", "-qm", "reverted")
    assert publish.publish_linux(linux_args(site.release)) == "3.0.3"


@pytest.mark.parametrize("url,reason", [
    ("http://github.com/x/LightsOut-Linux-Native-3.0.3-source.zip", "plain https URL"),
    ("https://user:pw@github.com/x/LightsOut-Linux-Native-3.0.3-source.zip", "plain https URL"),
    ("https://github.com/x/LightsOut-Linux-Native-3.0.3-source.zip?x=1", "plain https URL"),
    ("https://github.com/x/LightsOut-Linux-Native-3.0.3-source.zip#top", "plain https URL"),
    ("https://github.com/x/source.zip", "must end in /LightsOut-Linux-Native-3.0.3-source.zip"),
    ("https://play.lightsoutranked.com/hub/LightsOut-Linux-Native-3.0.3-source.zip", "names this site"),
])
def test_linux_release_checks_an_external_source_url(linux_site, capsys, url, reason):
    make_linux_release(linux_site.release)
    refused(linux_site, capsys, linux_args(linux_site.release, source_url=url), reason)


def test_linux_release_rolls_back_when_the_catalogue_cannot_be_written(linux_site, monkeypatch):
    site = linux_site
    make_linux_release(site.release)
    before = snapshot(site.root)

    def full(c):
        raise OSError("disk full")
    monkeypatch.setattr(publish, "save_catalogue", full)
    with pytest.raises(OSError, match="disk full"):
        publish.publish_linux(linux_args(site.release))
    assert snapshot(site.root) == before


def test_save_catalogue_normalises_owned_linux_urls_only(linux_site):
    c = publish.load_catalogue()
    c["linux"] = {"version": "3.0.3",
                  "download_url": "https://lightsout.up.railway.app/hub/L.zip",
                  "source_url": "https://www.lightsoutranked.com/hub/L-source.zip?x=1",
                  "page_url": "https://play.lightsoutranked.com/"}
    publish.save_catalogue(c)
    linux = publish.load_catalogue()["linux"]
    assert linux["download_url"] == "https://play.lightsoutranked.com/hub/L.zip"
    assert linux["source_url"] == "https://play.lightsoutranked.com/hub/L-source.zip?x=1"
    assert linux["page_url"] == "https://lightsoutranked.com/"
    external = "https://github.com/WarrS03448/lights-out/releases/download/linux-v3.0.3/L-source.zip"
    c["linux"]["source_url"] = external
    publish.save_catalogue(c)
    assert publish.load_catalogue()["linux"]["source_url"] == external


def test_a_hub_release_keeps_the_linux_entry_and_zips(linux_site, monkeypatch, capsys):
    site = linux_site
    make_linux_release(site.release)
    publish.publish_linux(linux_args(site.release))
    linux = publish.load_catalogue()["linux"]
    zips = {p.name: p.read_bytes() for p in site.hub.glob("LightsOut-Linux-Native-*")}
    assert len(zips) == 2
    setup = site.root / "dist/LightsOut-Setup-3.0.4.exe"
    setup.parent.mkdir()
    setup.write_bytes(b"new windows installer")
    monkeypatch.setattr(publish, "read_hub_version", lambda: "3.0.3")
    monkeypatch.setattr(publish, "write_hub_version", lambda v: None)
    monkeypatch.setattr(publish, "find_iscc", lambda: "iscc-test")
    monkeypatch.setattr(publish, "build_hub", lambda version, iscc: str(setup))
    assert publish.publish_hub(types.SimpleNamespace(version="3.0.4", bump=None, same_version=False)) == "3.0.4"
    after = publish.load_catalogue()
    assert after["hub"]["version"] == "3.0.4"
    assert after["linux"] == linux
    assert {p.name: p.read_bytes() for p in site.hub.glob("LightsOut-Linux-Native-*")} == zips
    assert not (site.hub / "LightsOut-Setup-3.0.3.exe").exists()
    assert "the published Linux beta is 3.0.3, older than this hub" in capsys.readouterr().out


def test_a_hub_release_without_linux_says_nothing_about_linux(linux_site, monkeypatch, capsys):
    site = linux_site
    setup = site.root / "dist/LightsOut-Setup-3.0.4.exe"
    setup.parent.mkdir()
    setup.write_bytes(b"new windows installer")
    monkeypatch.setattr(publish, "read_hub_version", lambda: "3.0.3")
    monkeypatch.setattr(publish, "write_hub_version", lambda v: None)
    monkeypatch.setattr(publish, "find_iscc", lambda: "iscc-test")
    monkeypatch.setattr(publish, "build_hub", lambda version, iscc: str(setup))
    publish.publish_hub(types.SimpleNamespace(version="3.0.4", bump=None, same_version=False))
    assert "linux" not in publish.load_catalogue()
    assert "Linux" not in capsys.readouterr().out


def test_linux_command_deploys_then_verifies_unless_told_not_to(linux_site, monkeypatch):
    calls = []
    monkeypatch.setattr(publish, "deploy", lambda message=None, target_branch=None: calls.append(("deploy", message)))
    monkeypatch.setattr(publish, "verify", lambda *a, **k: calls.append(("verify",)))
    monkeypatch.setattr(publish, "current_branch", lambda: "main")
    make_linux_release(linux_site.release, "3.0.3")
    publish.main(["linux", "--dir", str(linux_site.release), "--no-deploy"])
    assert calls == []
    make_linux_release(linux_site.release / "next", "3.0.3.1")
    publish.main(["linux", "--dir", str(linux_site.release / "next")])
    assert calls == [("deploy", "Release Linux beta 3.0.3.1"), ("verify",)]


def test_deploy_stages_new_linux_zips_as_release_artefacts(fake_git):
    calls, state = fake_git
    state["status"] = (" M server/public/catalogue.json\n?? server/public/hub/LightsOut-Linux-Native-3.0.3-x86_64.zip\n"
                       "?? server/public/hub/LightsOut-Linux-Native-3.0.3-source.zip\n")
    publish.deploy("Release Linux beta 3.0.3")
    assert ["git", "push", "origin", "main"] in calls


def test_status_prints_the_linux_version_and_file(linux_site, monkeypatch, capsys):
    make_linux_release(linux_site.release)
    publish.publish_linux(linux_args(linux_site.release))
    capsys.readouterr()
    local = publish.load_catalogue()
    monkeypatch.setattr(publish, "http", lambda url, **k: (200, {}, json.dumps(local).encode()))
    monkeypatch.setattr(publish, "read_hub_version", lambda: "3.0.3")
    publish.status()
    out = capsys.readouterr().out
    assert out.count("linux 3.0.3  LightsOut-Linux-Native-3.0.3-x86_64.zip") == 2, out
    assert "source LightsOut-Linux-Native-3.0.3-source.zip" in out and "in sync" in out
    del local["linux"]
    publish.save_catalogue(local)
    publish.status()
    assert "linux: none published" in capsys.readouterr().out


def test_linux_release_file_must_not_be_a_symlink(linux_site, capsys, monkeypatch):
    """A symlink to a regular file passes is_file(). Making one needs rights Windows may not grant, so
    the link is simulated."""
    make_linux_release(linux_site.release)
    linked = linux_site.release / "LightsOut-Linux-Native-3.0.3.json"
    real = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda self: self == linked or real(self))
    refused(linux_site, capsys, linux_args(linux_site.release), "is not a regular file")


def test_a_served_name_behind_a_link_is_refused(linux_site, capsys, monkeypatch):
    made = make_linux_release(linux_site.release)
    target = linux_site.hub / made["source"].name
    target.write_bytes(made["source"].read_bytes())          # the right bytes, but (simulated) behind a link
    real = os.path.islink
    monkeypatch.setattr(publish.os.path, "islink", lambda path: (
        os.path.normcase(os.path.abspath(path)) == os.path.normcase(str(target)) or real(path)))
    refused(linux_site, capsys, linux_args(linux_site.release), "already exists with different bytes or as a link")


def test_linux_release_refuses_a_source_zip_the_client_cannot_accept(linux_site, capsys, monkeypatch):
    make_linux_release(linux_site.release)
    monkeypatch.setattr(publish, "LINUX_SOURCE_LIMIT", 100)
    refused(linux_site, capsys, linux_args(linux_site.release), "the Linux client accepts at most 100")


def test_a_hub_release_at_the_linux_version_prints_no_note(linux_site, monkeypatch, capsys):
    site = linux_site
    make_linux_release(site.release, "3.0.4")
    publish.publish_linux(linux_args(site.release))
    capsys.readouterr()
    setup = site.root / "dist/LightsOut-Setup-3.0.4.exe"
    setup.parent.mkdir()
    setup.write_bytes(b"new windows installer")
    monkeypatch.setattr(publish, "read_hub_version", lambda: "3.0.3")
    monkeypatch.setattr(publish, "write_hub_version", lambda v: None)
    monkeypatch.setattr(publish, "find_iscc", lambda: "iscc-test")
    monkeypatch.setattr(publish, "build_hub", lambda version, iscc: str(setup))
    publish.publish_hub(types.SimpleNamespace(version="3.0.4", bump=None, same_version=False))
    assert publish.load_catalogue()["linux"]["version"] == "3.0.4"
    assert "NOTE" not in capsys.readouterr().out


# ---------------------------------------------------------------- publish.py linux deploys only from main
@pytest.mark.parametrize("branch", [None, "HEAD", "release/linux-3.0.3"])
def test_publish_linux_deploys_only_from_main(linux_site, capsys, monkeypatch, branch):
    monkeypatch.setattr(publish, "current_branch", lambda: branch)
    monkeypatch.setattr(publish, "deploy", lambda *a, **k: pytest.fail("deploy() ran"))
    make_linux_release(linux_site.release)
    before = snapshot(linux_site.root)
    with pytest.raises(SystemExit):
        publish.main(["linux", "--dir", str(linux_site.release)])
    assert "not main. Run it with --no-deploy" in capsys.readouterr().out
    assert snapshot(linux_site.root) == before


def test_publish_linux_off_main_never_pushes(linux_site, capsys, monkeypatch, tmp_path):
    """The runbook's release worktree is on release/linux-<V>, and deploy() pushes the current branch:
    a deploying `publish.py linux` there would push a branch Railway never builds, then wait out verify.
    A real repository with a local bare origin: nothing may reach it."""
    site = linux_site
    remote = tmp_path / "origin.git"

    def git(*a):
        subprocess.run(["git", "-C", str(site.root), "-c", "user.name=T", "-c", "user.email=t@example.test", *a],
                       check=True, capture_output=True)
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True)
    git("init", "-q")
    git("checkout", "-q", "-b", "main")
    git("add", "-A")
    git("commit", "-qm", "base")
    git("remote", "add", "origin", str(remote))
    git("push", "-q", "origin", "main")
    git("checkout", "-q", "-b", "release/linux-3.0.3")
    make_linux_release(site.release)
    monkeypatch.setattr(publish, "run", REAL_RUN)                      # a real push, were one attempted
    monkeypatch.setattr(publish, "verify", lambda *a, **k: None)
    outside_git = lambda: {k: v for k, v in snapshot(site.root).items() if not k.startswith(".git/")}  # noqa: E731
    before = outside_git()
    with pytest.raises(SystemExit):
        publish.main(["linux", "--dir", str(site.release)])
    out = capsys.readouterr().out
    assert "on release/linux-3.0.3, not main" in out and "publish.py deploy --branch main" in out
    refs = subprocess.run(["git", "--git-dir", str(remote), "for-each-ref", "--format=%(refname)"],
                          check=True, capture_output=True, text=True).stdout.split()
    assert refs == ["refs/heads/main"]
    assert outside_git() == before


# ---------------------------------------------------------------- the update record, as installed clients see it
def test_linux_release_publishes_a_record_the_client_verifies(linux_site):
    made = make_linux_release(linux_site.release)
    publish.publish_linux(linux_args(linux_site.release))
    entry = publish.load_catalogue()["linux"]
    manifest = trust.offer_manifest(entry, {key_id_of(PRIMARY): public_of(PRIMARY)})
    assert manifest["archive"] == {"name": made["player"].name, "size": entry["size"], "sha256": entry["sha256"]}
    with pytest.raises(trust.UpdateRefused):
        trust.offer_manifest(entry, {key_id_of(STRANGER): public_of(STRANGER)})


def test_installed_clients_must_pin_the_signing_key(linux_site, capsys):
    """sign_update only checks that the NEW ZIP pins its key. Clients installed from the published ZIP
    hold that ZIP's keys, and a record they cannot verify is one they never offer."""
    installed_linux(linux_site, "3.0.3", pins=(PRIMARY, SPARE))
    make_linux_release(linux_site.release, "3.0.3.1", signer=NEWKEY, pins=(NEWKEY,))
    refused(linux_site, capsys, linux_args(linux_site.release),
            f"clients installed from Linux 3.0.3 cannot be shown to offer this update: it is signed with key "
            f"{key_id_of(NEWKEY)}, and the published LightsOut-Linux-Native-3.0.3-x86_64.zip pins only")


def test_a_rotation_through_the_spare_key_publishes(linux_site):
    """docs/linux-update-signing.md: sign with the spare both releases pin, and pin a new spare."""
    installed_linux(linux_site, "3.0.3", pins=(PRIMARY, SPARE))
    make_linux_release(linux_site.release, "3.0.3.1", signer=SPARE, pins=(SPARE, NEWKEY))
    assert publish.publish_linux(linux_args(linux_site.release)) == "3.0.3.1"


def test_lost_keys_publish_only_with_the_explicit_flag(linux_site, capsys):
    installed_linux(linux_site, "3.0.3")
    make_linux_release(linux_site.release, "3.0.3.1", signer=NEWKEY, pins=(NEWKEY,))
    refused(linux_site, capsys, linux_args(linux_site.release), "pass --break-in-app-updates")
    assert publish.publish_linux(linux_args(linux_site.release, break_in_app_updates=True)) == "3.0.3.1"
    assert "WARNING: clients installed from Linux 3.0.3 may not update in-app" in capsys.readouterr().out


def test_the_flag_never_excuses_the_new_zips_own_keys(linux_site, capsys):
    installed_linux(linux_site, "3.0.3")
    make_linux_release(linux_site.release, "3.0.3.1", signer=STRANGER, pins=(NEWKEY,))
    refused(linux_site, capsys, linux_args(linux_site.release, break_in_app_updates=True),
            "the Linux client would never offer this update")


@pytest.mark.parametrize("damage,reason,overridable", [
    ("missing", "is not in server/public/hub/ with the catalogue's sha256", False),
    ("other bytes", "is not in server/public/hub/ with the catalogue's sha256", False),
    ("not a zip", "LightsOut-Linux-Native-3.0.3-x86_64.zip is not a readable ZIP", False),
    ("no update_trust_linux.py", "lacks support/runtime/hub/update_trust_linux.py", True),
    ("other rules", "checks updates by other rules than publish.py (its MAX_MANIFEST differ", True),
])
def test_the_published_zip_must_show_what_installed_clients_trust(linux_site, capsys, damage, reason, overridable):
    other_rules = CLIENT_RULES.replace("MAX_MANIFEST = 16 * 1024", "MAX_MANIFEST = 8 * 1024")
    assert other_rules != CLIENT_RULES
    options = {"no update_trust_linux.py": dict(drop_member="support/runtime/hub/update_trust_linux.py"),
               "other rules": dict(trust_source=trust_module(PRIMARY, rules=other_rules))}.get(damage, {})
    made = installed_linux(linux_site, "3.0.3", **options)
    published = linux_site.hub / made["player"].name
    if damage == "missing":
        published.unlink()
    elif damage == "other bytes":
        published.write_bytes(published.read_bytes() + b"!")
    elif damage == "not a zip":
        published.write_bytes(b"not a zip")
        _set_catalogue(linux_site, lambda c: c["linux"].update(sha256=_sha(b"not a zip")))
    make_linux_release(linux_site.release, "3.0.3.1")
    refused(linux_site, capsys, linux_args(linux_site.release), reason)
    if overridable:
        assert publish.publish_linux(linux_args(linux_site.release, break_in_app_updates=True)) == "3.0.3.1"
    else:
        refused(linux_site, capsys, linux_args(linux_site.release, break_in_app_updates=True), reason)


def test_a_client_with_other_update_rules_is_refused(linux_site, capsys):
    """publish.py checks records with a copy of the client's rules. A build whose client checks by other
    rules waits until the copy is updated: a green publish would otherwise prove nothing."""
    changed = CLIENT_RULES.replace("if type(name) is not str: raise", "if type(name) is not str or len(name) > 8: raise")
    assert changed != CLIENT_RULES
    make_linux_release(linux_site.release, trust_source=trust_module(PRIMARY, rules=changed))
    refused(linux_site, capsys, linux_args(linux_site.release),
            "checks updates by other rules than publish.py: its verified_manifest differ")


CLIENT_TRUST = Path(ROOT, "hub", "update_trust_linux.py")


@pytest.mark.skipif(not CLIENT_TRUST.is_file(), reason="the Linux client (hub/update_trust_linux.py) is not in this tree")
def test_the_copied_rules_are_the_linux_clients_own():
    assert trust.client_rules_problem(CLIENT_TRUST.read_text(encoding="utf-8")) is None


def test_the_rules_comparison_ignores_docstrings_and_names_what_differs():
    client = trust_module(PRIMARY)
    assert trust.client_rules_problem(client) is None
    assert trust.client_rules_problem(client.replace("strict and cofactorless", "strict")) is None
    assert trust.client_rules_problem(client.replace("MAX_SOURCE = 1_000_000_000", "MAX_SOURCE = 1")) == (
        "its MAX_SOURCE differ from tools/release/linux_update_trust.py")
    assert "its extra differ" in trust.client_rules_problem(client + "\ndef extra():\n    pass\n")
    assert "does not parse" in trust.client_rules_problem("def (")


def test_pinned_keys_are_read_without_running_the_module():
    public = public_of(PRIMARY)
    source = f"import os\nos.remove('never-run')\nPINNED_KEYS = {{{key_id_of(PRIMARY)!r}: {public!r}}}\n"
    assert trust.pinned_keys(source) == {key_id_of(PRIMARY): public}


# RFC 8032 section 7.1, TESTs 1 to 3: (secret key, public key, message, signature).
RFC8032_VECTORS = [
    ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
     "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
    ("c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
     "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025", "af82",
     "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"),
]


@pytest.mark.parametrize("seed,public,message,signature", RFC8032_VECTORS)
def test_the_copied_verifier_and_the_test_signer_match_rfc8032(seed, public, message, signature):
    seed, public, message, signature = (bytes.fromhex(x) for x in (seed, public, message, signature))
    assert ed25519_sign(seed, message) == (public, signature)
    assert trust.verify(public, message, signature)
    assert not trust.verify(public, message + b"!", signature)
    assert not trust.verify(public, message, signature[:32] + bytes([signature[32] ^ 1]) + signature[33:])
    assert not trust.verify(public, message, signature[:63])


@pytest.mark.parametrize("field,value", [("kind", "inno-setup"), ("arch", "aarch64"), ("version", "3.0.4"),
                                         ("size", 1), ("size", True), ("sha256", "0" * 64), ("update", None)])
def test_offer_manifest_is_check_offer_without_the_version_step(linux_site, field, value):
    make_linux_release(linux_site.release)
    publish.publish_linux(linux_args(linux_site.release))
    entry = publish.load_catalogue()["linux"]
    pinned = {key_id_of(PRIMARY): public_of(PRIMARY)}
    assert trust.offer_manifest(entry, pinned)["display_version"] == "3.0.3"
    entry[field] = value
    with pytest.raises(trust.UpdateRefused):
        trust.offer_manifest(entry, pinned)
