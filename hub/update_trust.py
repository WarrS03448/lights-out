"""Windows update trust, independent of the downloadable catalogue.

The identity EKU is Microsoft's durable Artifact Signing subscriber identity,
verified against the official 2.6.8 installer. Certificates renew daily, so their
thumbprints/public keys must not be pinned. A new identity requires a reviewed
client release; neither server metadata nor environment variables can override it.
"""
import base64
import contextlib
import ctypes
from ctypes import wintypes
import json
import os
import subprocess

PUBLISHER = "Samuel Warren"
IDENTITY_EKU = "1.3.6.1.4.1.311.97.951605561.555398629.748726612.577571204"
_ERROR = "Update blocked: a valid timestamped Lights Out publisher signature could not be verified."
_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSHOME 'Modules/Microsoft.PowerShell.Security/Microsoft.PowerShell.Security.psd1') -ErrorAction Stop
$s = Get-AuthenticodeSignature -LiteralPath $env:LIGHTSOUT_VERIFY_UPDATE
$v = [System.Diagnostics.FileVersionInfo]::GetVersionInfo($env:LIGHTSOUT_VERIFY_UPDATE)
$oids = @($s.SignerCertificate.Extensions | Where-Object {$_.Oid.Value -eq '2.5.29.37'} | ForEach-Object {$_.EnhancedKeyUsages | ForEach-Object {$_.Value}})
@{status=[string]$s.Status; type=[string]$s.SignatureType;
  publisher=if($s.SignerCertificate){$s.SignerCertificate.GetNameInfo([System.Security.Cryptography.X509Certificates.X509NameType]::SimpleName,$false)}else{''};
  timestamp=[bool]$s.TimeStamperCertificate; identityOids=$oids;
  product=$v.ProductName.Trim(); version=@($v.FileMajorPart,$v.FileMinorPart,$v.FileBuildPart,$v.FilePrivatePart)} | ConvertTo-Json -Compress
"""


def _kernel():
    if os.name != "nt":
        raise RuntimeError("Automatic Windows updates require Windows signature verification.")
    return ctypes.WinDLL("kernel32", use_last_error=True)


@contextlib.contextmanager
def locked_update(path):
    """Hold the exact executable read-only against replacement through process creation."""
    kernel = _kernel()
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    handle = create(os.path.abspath(path), 0x80000000, 1, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        final = kernel.GetFinalPathNameByHandleW
        final.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        final.restype = wintypes.DWORD
        buffer = ctypes.create_unicode_buffer(32768)
        length = final(handle, buffer, len(buffer), 0)
        if not length or length >= len(buffer):
            raise RuntimeError(_ERROR)
        yield buffer.value
    finally:
        close(handle)


def verify_signature(path):
    """Fail closed on invalid/untrusted/other-publisher signatures or verifier errors."""
    kernel = _kernel()
    system_dir = kernel.GetSystemDirectoryW
    system_dir.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    system_dir.restype = wintypes.UINT
    buffer = ctypes.create_unicode_buffer(32768)
    length = system_dir(buffer, len(buffer))
    if not length or length >= len(buffer):
        raise RuntimeError(_ERROR)
    powershell = os.path.join(buffer.value, "WindowsPowerShell", "v1.0", "powershell.exe")
    env = dict(os.environ, LIGHTSOUT_VERIFY_UPDATE=os.path.abspath(path))
    encoded = base64.b64encode(_SCRIPT.encode("utf-16le")).decode("ascii")
    try:
        result = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                                capture_output=True, timeout=30, env=env,
                                creationflags=subprocess.CREATE_NO_WINDOW, check=True)
        report = json.loads(result.stdout.decode("utf-8-sig"))
        if (report.get("status") != "Valid" or report.get("type") != "Authenticode"
                or report.get("publisher") != PUBLISHER or report.get("timestamp") is not True
                or IDENTITY_EKU not in report.get("identityOids", [])):
            raise ValueError("untrusted signature")
        return report
    except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError) as exc:
        raise RuntimeError(_ERROR) from exc


def verify_update(path):
    """Reject signed old clients too: they may predate these security checks."""
    from . import version
    report = verify_signature(path)
    parts = report.get("version")
    if (report.get("product") != version.PRODUCT_NAME or not isinstance(parts, list)
            or len(parts) != 4 or any(type(n) is not int or not 0 <= n <= 65535 for n in parts)):
        raise RuntimeError("Update blocked: the signed file is not a recognized Lights Out update.")
    if tuple(parts) <= version.version_tuple(version.HUB_VERSION):
        raise RuntimeError("Update blocked: the signed file must be newer than this Lights Out version.")
