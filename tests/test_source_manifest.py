"""Run: python -m pytest tests/test_source_manifest.py."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest


def test_manifest_uses_immutable_source_catalogue_and_detects_changed_download(tmp_path):
    script = Path(__file__).resolve().parents[1] / "tools/release/source_manifest.py"
    spec = importlib.util.spec_from_file_location("source_manifest", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    payload = b"synthetic signed release stand-in"
    sha = hashlib.sha256(payload).hexdigest()
    catalogue = {"hub": {"version":"1.2.3", "download_url":"https://example.test/hub/Setup.exe",
                         "sha256":sha, "size":len(payload)}, "gamemodes":[]}
    location = tmp_path / "server/public/catalogue.json"
    location.parent.mkdir(parents=True)
    location.write_text(json.dumps(catalogue), encoding="utf-8")
    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], text=True).strip()
    git("init", "-q")
    git("add", "server/public/catalogue.json")
    git("-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-qm", "source")
    commit = git("rev-parse", "HEAD")
    location.write_text('{"hub":{"version":"tampered"}}', encoding="utf-8")
    manifest = module.make_manifest(tmp_path, commit)
    assert manifest["source_commit"] == commit
    assert manifest["release"] == "1.2.3"
    assert manifest["artifacts"][0]["sha256"] == sha
    artifact = tmp_path / "Setup.exe"
    artifact.write_bytes(payload)
    module.verify_artifact(artifact, manifest["artifacts"][0])
    artifact.write_bytes(b"changed")
    with pytest.raises(ValueError, match="match"):
        module.verify_artifact(artifact, manifest["artifacts"][0])


# ---------------------------------------------------------------- the Linux beta entry
def load_module():
    script = Path(__file__).resolve().parents[1] / "tools/release/source_manifest.py"
    spec = importlib.util.spec_from_file_location("source_manifest", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


INSTALLER = b"synthetic signed installer"
BB5 = b"synthetic bb5 pack"
CTF = b"synthetic ctf pack"
LINUX_ZIP = b"synthetic linux player zip"
LINUX_SOURCE = b"synthetic linux corresponding source zip"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def windows_catalogue():
    return {"catalogue_version": 1,
            "hub": {"version": "3.0.3", "download_url": "https://play.lightsoutranked.com/hub/LightsOut-Setup-3.0.3.exe",
                    "page_url": "https://lightsoutranked.com/", "sha256": digest(INSTALLER), "size": len(INSTALLER),
                    "kind": "inno-setup"},
            "gamemodes": [{"id": "BB5", "version": "1.0.30", "pack_url": "https://play.lightsoutranked.com/packs/BB5-1.0.30.zip",
                           "sha256": digest(BB5), "size": len(BB5)},
                          {"id": "CTF", "version": "1.0.4", "pack_url": "https://play.lightsoutranked.com/packs/CTF-1.0.4.zip",
                           "sha256": digest(CTF), "size": len(CTF)}]}


def linux_entry(**changes):
    entry = {"version": "3.0.3", "kind": "linux-native-zip", "arch": "x86_64",
             "download_url": "https://play.lightsoutranked.com/hub/LightsOut-Linux-Native-3.0.3-x86_64.zip",
             "size": len(LINUX_ZIP), "sha256": digest(LINUX_ZIP),
             "source_url": "https://play.lightsoutranked.com/hub/LightsOut-Linux-Native-3.0.3-source.zip",
             "source_size": len(LINUX_SOURCE), "source_sha256": digest(LINUX_SOURCE),
             "page_url": "https://lightsoutranked.com/", "required": False,
             "update": {"key_id": "k", "manifest": "e30=", "signature": "AA=="}}
    entry.update(changes)
    return entry


def committed(tmp_path, catalogue):
    """A repository whose commit holds `catalogue`; returns (root, full commit)."""
    root = tmp_path / "repo"
    location = root / "server/public/catalogue.json"
    location.parent.mkdir(parents=True)
    location.write_text(json.dumps(catalogue, indent=2), encoding="utf-8")

    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), "-c", "user.name=Test",
                                        "-c", "user.email=test@example.test", *args], text=True).strip()
    git("init", "-q")
    git("add", "server/public/catalogue.json")
    git("commit", "-qm", "source")
    return root, git("rev-parse", "HEAD")


def test_windows_only_manifest_is_byte_identical_to_before(tmp_path):
    """No catalogue.linux: exactly the record earlier releases produced, key order included."""
    module = load_module()
    root, commit = committed(tmp_path, windows_catalogue())
    manifest = module.make_manifest(root, commit)
    expected = {"schema_version": 1, "release": "3.0.3", "source_commit": commit,
                "catalogue_path": "server/public/catalogue.json",
                "claim": "Download hashes recorded in this exact source commit; not a bit-for-bit build attestation.",
                "artifacts": [
                    {"kind": "windows-installer", "version": "3.0.3", "filename": "LightsOut-Setup-3.0.3.exe",
                     "url": "https://play.lightsoutranked.com/hub/LightsOut-Setup-3.0.3.exe",
                     "sha256": digest(INSTALLER), "size": len(INSTALLER)},
                    {"kind": "BB5", "version": "1.0.30", "filename": "BB5-1.0.30.zip",
                     "url": "https://play.lightsoutranked.com/packs/BB5-1.0.30.zip", "sha256": digest(BB5), "size": len(BB5)},
                    {"kind": "CTF", "version": "1.0.4", "filename": "CTF-1.0.4.zip",
                     "url": "https://play.lightsoutranked.com/packs/CTF-1.0.4.zip", "sha256": digest(CTF), "size": len(CTF)}]}
    assert json.dumps(manifest, indent=2) == json.dumps(expected, indent=2)


def test_linux_entry_adds_two_artifacts_and_leaves_windows_and_packs_unchanged(tmp_path):
    module = load_module()
    windows_root, windows_commit = committed(tmp_path / "w", windows_catalogue())
    windows = module.make_manifest(windows_root, windows_commit)
    catalogue = windows_catalogue()
    catalogue["linux"] = linux_entry(version="3.0.3.1")
    root, commit = committed(tmp_path / "l", catalogue)
    manifest = module.make_manifest(root, commit)
    assert json.dumps(manifest["artifacts"][:3], indent=2) == json.dumps(windows["artifacts"], indent=2)
    assert manifest["artifacts"][3:] == [
        {"kind": "linux-native-zip", "version": "3.0.3.1", "filename": "LightsOut-Linux-Native-3.0.3-x86_64.zip",
         "url": catalogue["linux"]["download_url"], "sha256": digest(LINUX_ZIP), "size": len(LINUX_ZIP)},
        {"kind": "linux-source-zip", "version": "3.0.3.1", "filename": "LightsOut-Linux-Native-3.0.3-source.zip",
         "url": catalogue["linux"]["source_url"], "sha256": digest(LINUX_SOURCE), "size": len(LINUX_SOURCE)}]
    assert manifest["release"] == "3.0.3" and manifest["linux_release"] == "3.0.3.1"
    assert manifest["claim"].startswith(windows["claim"] + " ")
    assert "SOURCE-MANIFEST.json" in manifest["claim"] and "private-only files" in manifest["claim"]
    assert "\u2014" not in manifest["claim"]          # no em dash


def test_verify_file_accepts_both_linux_zips_and_catches_a_changed_one(tmp_path, capsys):
    module = load_module()
    catalogue = windows_catalogue()
    catalogue["linux"] = linux_entry(
        source_url="https://github.com/WarrS03448/lights-out/releases/download/linux-v3.0.3/"
                   "LightsOut-Linux-Native-3.0.3-source.zip")
    root, commit = committed(tmp_path, catalogue)
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    for name, data in (("LightsOut-Linux-Native-3.0.3-x86_64.zip", LINUX_ZIP),
                       ("LightsOut-Linux-Native-3.0.3-source.zip", LINUX_SOURCE),
                       ("LightsOut-Setup-3.0.3.exe", INSTALLER)):
        (downloads / name).write_bytes(data)
        module.main(["--source-root", str(root), "--source-ref", commit, "--verify-file", str(downloads / name)])
        assert capsys.readouterr().out.strip() == "Download matches source commit " + commit
    (downloads / "LightsOut-Linux-Native-3.0.3-source.zip").write_bytes(LINUX_SOURCE[:-1] + b"X")
    with pytest.raises(ValueError, match="does not match"):
        module.main(["--source-root", str(root), "--source-ref", commit,
                     "--verify-file", str(downloads / "LightsOut-Linux-Native-3.0.3-source.zip")])
    output = tmp_path / "release-source.json"
    module.main(["--source-root", str(root), "--source-ref", commit, "--output", str(output)])
    written = json.loads(output.read_text(encoding="utf-8"))
    assert [a["kind"] for a in written["artifacts"]] == ["windows-installer", "BB5", "CTF",
                                                          "linux-native-zip", "linux-source-zip"]


@pytest.mark.parametrize("changes", [
    {"kind": "windows-installer"}, {"arch": "aarch64"}, {"version": None},
    {"download_url": "http://play.lightsoutranked.com/hub/L.zip"}, {"source_url": None},
    {"sha256": "A" * 64}, {"source_sha256": "0" * 63}, {"size": 0}, {"source_size": "12"},
    {"source_url": "https://user:pw@example.test/L-source.zip"},
])
def test_an_invalid_linux_entry_is_refused(tmp_path, changes):
    module = load_module()
    catalogue = windows_catalogue()
    catalogue["linux"] = linux_entry(**changes)
    root, commit = committed(tmp_path, catalogue)
    with pytest.raises(ValueError, match="Invalid"):
        module.make_manifest(root, commit)
