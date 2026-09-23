"""Run: python -m pytest tests/test_update_security.py. Never executes an installer."""
import contextlib
import os
from pathlib import Path
import shutil
from unittest.mock import patch

import pytest

from hub import update


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
    root = Path(__file__).resolve().parents[1]
    release = root / "server/public/hub/LightsOut-Setup-2.6.8.exe"
    if not release.exists():
        pytest.skip("historical release fixture absent")
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
    # A genuine Windows publisher is still not the Lights Out publisher.
    with pytest.raises(RuntimeError):
        verify_signature(str(Path(os.environ["SystemRoot"]) / "System32/notepad.exe"))


@pytest.mark.skipif(os.name != "nt", reason="Windows Authenticode")
def test_signed_old_payload_cannot_downgrade_client(tmp_path):
    from hub import update_trust
    release = Path(__file__).resolve().parents[1] / "server/public/hub/LightsOut-Setup-2.6.8.exe"
    if not release.exists():
        pytest.skip("historical release fixture absent")
    # The catalogue may claim any version; the signed payload must be newer.
    with patch.object(update, "Popen") as launched:
        with pytest.raises(RuntimeError, match="newer"):
            update.launch(str(release), "inno-setup")
        launched.assert_not_called()


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
        with patch.object(update_trust, "verify_signature", return_value={"product":"Lights Out","version":[2,6,9,0]}):
            update_trust.verify_update(str(tmp_path / "future.exe"))
        for report in [{"product":"Other program","version":[2,6,9,0]},
                       {"product":"Lights Out","version":[2,6,8,0]},
                       {"product":"Lights Out","version":[2,6,7,0]},
                       {"product":"Lights Out","version":"9999.0.0"}]:
            with patch.object(update_trust, "verify_signature", return_value=report):
                with pytest.raises(RuntimeError):
                    update_trust.verify_update(str(tmp_path / "unacceptable.exe"))
