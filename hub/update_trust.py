"""Windows update trust, independent of the downloadable catalogue.

The identity EKU is Microsoft's durable Artifact Signing subscriber identity,
verified against the official 2.6.8 installer. Certificates renew daily, so their
thumbprints/public keys must not be pinned. A new identity requires a reviewed
client release; neither server metadata nor environment variables can override it.
"""
import contextlib
import ctypes
from ctypes import wintypes
import os

PUBLISHER = "Samuel Warren"
IDENTITY_EKU = "1.3.6.1.4.1.311.97.951605561.555398629.748726612.577571204"
_ERROR = "Update blocked: a valid timestamped Lights Out publisher signature could not be verified."


def _kernel():
    if os.name != "nt":
        raise RuntimeError("Automatic Windows updates require Windows signature verification.")
    return ctypes.WinDLL("kernel32", use_last_error=True)


@contextlib.contextmanager
def _locked_file(path):
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
        yield buffer.value, handle
    finally:
        close(handle)


@contextlib.contextmanager
def locked_update(path):
    with _locked_file(path) as (resolved, _):
        yield resolved


def verify_signature(path):
    """Fail closed using native Windows trust and the version in the signed PE section."""
    from .wintrust import verify_embedded_signature
    from .pe_version import read_version
    try:
        with _locked_file(path) as (resolved, handle):
            report = verify_embedded_signature(resolved, handle)
            report.update(read_version(resolved))
        if (report.get("status") != "Valid" or report.get("type") != "Authenticode"
                or report.get("publisher") != PUBLISHER or report.get("timestamp") is not True
                or IDENTITY_EKU not in report.get("identityOids", [])):
            raise ValueError("untrusted signature")
        return report
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise RuntimeError(_ERROR) from exc


def verify_update(path, kind=None):
    """Reject signed old clients too: they may predate these security checks."""
    from . import version
    report = verify_signature(path)
    parts = report.get("version")
    # The signed Inno uninstaller also says Lights Out, with Inno's own higher
    # file version. Require the signed role matching how launch will execute it.
    description = ("Lights Out installer (unofficial)" if kind == "inno-setup"
                   else version.FILE_DESCRIPTION)
    if (report.get("product") != version.PRODUCT_NAME or not isinstance(parts, list)
            or report.get("description") != description
            or len(parts) != 4 or any(type(n) is not int or not 0 <= n <= 65535 for n in parts)):
        raise RuntimeError("Update blocked: the signed file is not a recognized Lights Out update.")
    if tuple(parts) <= version.version_tuple(version.HUB_VERSION):
        raise RuntimeError("Update blocked: the signed file must be newer than this Lights Out version.")
