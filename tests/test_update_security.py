"""Run: python -m pytest tests/test_update_security.py. Never executes an installer."""
import contextlib
import os
from pathlib import Path
import shutil
import subprocess
import struct
from unittest.mock import patch

import pytest

from hub import update, version


def signed_release():
    release = (Path(__file__).resolve().parents[1] / "server/public/hub" /
               f"LightsOut-Setup-{version.HUB_VERSION}.exe")
    if not release.exists():
        pytest.skip("official signed installer fixture absent from source distribution")
    return release


@pytest.mark.skipif(os.name != "nt", reason="Windows Authenticode")
def test_signature_verification_never_starts_a_child_process():
    from hub.update_trust import verify_signature
    with patch.object(subprocess, "Popen", side_effect=AssertionError("child process forbidden")):
        report = verify_signature(str(signed_release()))
    assert report["product"] == "Lights Out"
    assert report["version"] == list(version.version_tuple())


def test_cleanup_only_removes_recognized_lights_out_downloads(tmp_path, monkeypatch):
    removed = ["LightsOut-Setup-2.8.1.exe", "LightsOut-Setup-2.8.2.exe.part",
               "CommunityHub-1.0.9.exe", "LightsOut-2.0.0.exe.part"]
    kept = ["other.exe", "other.part", "LightsOut.exe", "notes.txt", "LightsOut-Setup-custom.exe"]
    for name in removed + kept:
        (tmp_path / name).write_bytes(b"fixture")
    monkeypatch.setattr(update, "updates_dir", lambda: str(tmp_path))
    monkeypatch.setattr(update, "own_exe", lambda: None)
    assert sorted(update.clean_old_versions()) == sorted(removed)
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(kept)


@pytest.mark.parametrize("kind", [None, "inno-setup", "unknown"])
def test_untrusted_update_never_launches(kind, tmp_path):
    path = tmp_path / "untrusted.exe"
    path.write_bytes(b"not a signed executable")
    with patch.object(update, "Popen") as launched, \
         patch.object(update.os, "startfile", create=True) as opened:
        with pytest.raises((OSError, ValueError, RuntimeError)):
            update.launch(str(path), kind)
        launched.assert_not_called()
        opened.assert_not_called()


@pytest.mark.skipif(os.name != "nt", reason="Windows Authenticode")
def test_real_signed_release_is_accepted_and_tampering_is_rejected(tmp_path):
    from hub.update_trust import verify_signature, locked_update
    release = signed_release()
    signed_copy = tmp_path / "signed.exe"
    shutil.copyfile(release, signed_copy)
    with locked_update(str(signed_copy)) as path:
        verify_signature(path)
        with pytest.raises(OSError):
            with open(path, "r+b"):
                pass
        replacement = tmp_path / "replacement.exe"
        replacement.write_bytes(b"replacement")
        with pytest.raises(OSError):
            os.replace(replacement, path)
    damaged = tmp_path / "changed.exe"
    content = bytearray(release.read_bytes())
    content[4096] ^= 1
    damaged.write_bytes(content)
    with pytest.raises(RuntimeError):
        verify_signature(str(damaged))
    # Inno's compressed payload lives after the final PE section. Its integrity
    # matters just as much as the loader tested above.
    overlay = bytearray(release.read_bytes())
    overlay[len(overlay) // 2] ^= 1
    damaged.write_bytes(overlay)
    with pytest.raises(RuntimeError):
        verify_signature(str(damaged))
    # A genuine Windows publisher is still not the Lights Out publisher.
    with pytest.raises(RuntimeError):
        verify_signature(str(Path(os.environ["SystemRoot"]) / "System32/notepad.exe"))


@pytest.mark.skipif(os.name != "nt", reason="Windows Authenticode")
def test_missing_timestamp_is_rejected(tmp_path):
    from hub.update_trust import verify_signature
    content = bytearray(signed_release().read_bytes())
    pe = struct.unpack_from("<I", content, 60)[0]
    optional = pe + 24
    magic = struct.unpack_from("<H", content, optional)[0]
    directories = optional + (112 if magic == 0x20b else 96)
    offset, size = struct.unpack_from("<II", content, directories + 32)
    # Rename the RFC3161 unsigned attribute to an unknown OID. No signed PE
    # bytes or signing certificate change, but the signature loses its timestamp.
    oid = bytes.fromhex("060a2b060104018237030301")
    certificate = content[offset:offset + size]
    assert certificate.count(oid) == 1, "fixture must have exactly one RFC3161 timestamp"
    content[offset + certificate.index(oid) + len(oid) - 1] = 0x7f
    untimestamped = tmp_path / "no-timestamp.exe"
    untimestamped.write_bytes(content)
    with pytest.raises(RuntimeError):
        verify_signature(str(untimestamped))


@pytest.mark.skipif(os.name != "nt", reason="Windows Authenticode")
def test_forged_timestamp_is_rejected(tmp_path):
    from hub.update_trust import verify_signature
    content = bytearray(signed_release().read_bytes())
    oid = bytes.fromhex("060a2b060104018237030301")
    assert content.count(oid) == 1

    def node(offset):
        tag, length = content[offset:offset + 2]
        start = offset + 2
        if length & 128:
            count = length & 127
            assert 0 < count <= 4
            length = int.from_bytes(content[start:start + count], "big")
            start += count
        assert start + length <= len(content)
        return tag, start, start + length

    def children(parent):
        cursor, end = parent[1:]
        result = []
        while cursor < end:
            child = node(cursor)
            assert child[2] <= end
            result.append(child)
            cursor = child[2]
        return result

    values = node(content.index(oid) + len(oid))
    token = children(values)[0]              # ContentInfo
    signed = children(children(token)[1])[0]  # [0] -> SignedData
    signers = children(signed)[-1]           # SET OF SignerInfo
    signer = children(signers)[0]
    signature = [child for child in children(signer) if child[0] == 4]
    assert len(signature) == 1
    content[signature[0][2] - 1] ^= 1        # TSA signature only; retain timestamp
    forged = tmp_path / "forged-timestamp.exe"
    forged.write_bytes(content)
    with pytest.raises(RuntimeError):
        verify_signature(str(forged))


@pytest.mark.skipif(os.name != "nt", reason="Windows Authenticode")
def test_signed_old_payload_cannot_downgrade_client(tmp_path):
    from hub import update_trust
    release = signed_release()
    # The catalogue may claim any version; the signed payload must be newer.
    with patch.object(update, "Popen") as launched:
        with pytest.raises(RuntimeError, match="newer"):
            update.launch(str(release), "inno-setup")
        launched.assert_not_called()


@pytest.mark.skipif(os.name != "nt", reason="Windows Authenticode")
@pytest.mark.parametrize("kind", ["inno-setup", None, "legacy"])
def test_signed_uninstaller_can_never_be_launched_as_an_update(kind, tmp_path):
    uninstaller = Path(__file__).resolve().parents[1] / "dist/signing-evidence/uninst.e32.tmp"
    if not uninstaller.exists():
        pytest.skip("signed internal uninstaller exists only after a release build")
    renamed = tmp_path / "LightsOut-Setup-99.0.0.exe"
    shutil.copyfile(uninstaller, renamed)
    with patch.object(update, "Popen") as launched:
        with pytest.raises(RuntimeError, match="recognized"):
            update.launch(str(renamed), kind)
        launched.assert_not_called()


@pytest.mark.parametrize("kind", ["inno-setup", None, "legacy"])
@pytest.mark.parametrize("description", ["Lights Out installer (unofficial)", version.FILE_DESCRIPTION, "Setup/Uninstall"])
def test_signed_role_must_match_the_launch_mode(kind, description):
    from hub import update_trust
    report = {"product": "Lights Out", "description": description, "version": [51,1054,0,0]}
    expected = "Lights Out installer (unofficial)" if kind == "inno-setup" else version.FILE_DESCRIPTION
    with patch.object(update_trust, "verify_signature", return_value=report):
        if description == expected:
            update_trust.verify_update("fixture.exe", kind)
        else:
            with pytest.raises(RuntimeError, match="recognized"):
                update_trust.verify_update("fixture.exe", kind)


@pytest.mark.skipif(os.name != "nt", reason="Windows Authenticode")
def test_genuine_installer_is_accepted_as_a_newer_update():
    from hub import update_trust
    release = signed_release()
    with patch.object(version, "HUB_VERSION", "0.0.0"):
        update_trust.verify_update(str(release), "inno-setup")
        with pytest.raises(RuntimeError, match="recognized"):
            update_trust.verify_update(str(release), "legacy")


def test_verifier_failure_prevents_process_creation(tmp_path):
    from hub import update_trust
    path = tmp_path / "update.exe"
    path.write_bytes(b"fixture")
    with patch.object(update_trust, "locked_update", return_value=contextlib.nullcontext(str(path))), \
         patch.object(update_trust, "verify_signature", side_effect=RuntimeError("verification unavailable")), \
         patch.object(update, "Popen") as launched:
        with pytest.raises(RuntimeError, match="verification unavailable"):
            update.launch(str(path), "inno-setup")
        launched.assert_not_called()


def test_signed_version_and_product_gate_allows_only_a_newer_lights_out(tmp_path):
    from hub import update_trust, version
    with patch.object(version, "HUB_VERSION", "2.6.8"):
        with patch.object(update_trust, "verify_signature", return_value={"product":"Lights Out","description":version.FILE_DESCRIPTION,"version":[2,6,9,0]}):
            update_trust.verify_update(str(tmp_path / "future.exe"))
        for report in [{"product":"Other program","version":[2,6,9,0]},
                       {"product":"Lights Out","version":[2,6,8,0]},
                       {"product":"Lights Out","version":[2,6,7,0]},
                       {"product":"Lights Out","version":"9999.0.0"}]:
            report["description"] = version.FILE_DESCRIPTION
            with patch.object(update_trust, "verify_signature", return_value=report):
                with pytest.raises(RuntimeError):
                    update_trust.verify_update(str(tmp_path / "unacceptable.exe"))
