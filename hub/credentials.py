"""Windows CurrentUser DPAPI storage; no plaintext fallback or machine-wide key."""
import base64
import ctypes as C
from ctypes import wintypes as W
import json
import os

ENTROPY = b"LightsOut.credentials.v1"


class CredentialError(Exception):
    pass


class _Blob(C.Structure):
    _fields_ = [("size", W.DWORD), ("data", C.POINTER(C.c_ubyte))]


def _transform(data, decrypt=False):
    if os.name != "nt" or not isinstance(data, bytes) or not 0 < len(data) <= 32768:
        raise CredentialError("Saved sign-in is unavailable.")
    crypt = C.WinDLL("crypt32", use_last_error=True)
    kernel = C.WinDLL("kernel32", use_last_error=True)
    source_buffer, entropy_buffer = C.create_string_buffer(data), C.create_string_buffer(ENTROPY)
    source = _Blob(len(data), C.cast(source_buffer, C.POINTER(C.c_ubyte)))
    entropy = _Blob(len(ENTROPY), C.cast(entropy_buffer, C.POINTER(C.c_ubyte)))
    output = _Blob()
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [C.POINTER(_Blob), C.c_void_p, C.POINTER(_Blob), C.c_void_p, C.c_void_p, W.DWORD, C.POINTER(_Blob)]
    function.restype = W.BOOL
    kernel.LocalFree.argtypes, kernel.LocalFree.restype = [C.c_void_p], C.c_void_p
    try:
        if not function(C.byref(source), None, C.byref(entropy), None, None, 1, C.byref(output)):
            raise CredentialError("Saved sign-in could not be opened.")
        return C.string_at(output.data, output.size)
    finally:
        C.memset(source_buffer, 0, len(data))
        if output.data:
            C.memset(output.data, 0, output.size)
            kernel.LocalFree(output.data)


def seal(value):
    try:
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return {"schema": 1, "protected": base64.b64encode(_transform(raw)).decode("ascii")}
    except Exception:
        raise CredentialError("Sign-in could not be saved securely.") from None


def open_sealed(value):
    try:
        if value.get("schema") != 1 or not isinstance(value.get("protected"), str) or len(value["protected"]) > 44000:
            raise ValueError()
        decoded = json.loads(_transform(base64.b64decode(value["protected"], validate=True), decrypt=True))
        if not isinstance(decoded, dict):
            raise ValueError()
        return decoded
    except Exception:
        raise CredentialError("Saved sign-in could not be opened.") from None
