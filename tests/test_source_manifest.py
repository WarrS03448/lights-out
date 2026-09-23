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
