"""Native Authenticode verification. No child processes or certificate-store writes.

Use only the signer and timestamp returned by the successful WinVerifyTrust state.
ctypes layouts follow the Windows SDK's WinTrust.h; pointers never outlive that state.
"""
import ctypes as c
import os
import uuid

DWORD = c.c_uint32
PTR = c.c_void_p


class FileInfo(c.Structure):
    _fields_ = [("size", DWORD), ("path", c.c_wchar_p), ("handle", PTR), ("subject", PTR)]


class SignatureSettings(c.Structure):
    _fields_ = [("size", DWORD), ("index", DWORD), ("flags", DWORD),
                ("secondary", DWORD), ("verified", DWORD), ("policy", PTR)]


class TrustData(c.Structure):
    _fields_ = [("size", DWORD), ("policy", PTR), ("sip", PTR), ("ui", DWORD),
                ("revocation", DWORD), ("choice", DWORD), ("file", c.POINTER(FileInfo)),
                ("action", DWORD), ("state", PTR), ("url", c.c_wchar_p),
                ("flags", DWORD), ("context", DWORD), ("signature", c.POINTER(SignatureSettings))]


class Signer(c.Structure):
    _fields_ = [("size", DWORD), ("time_low", DWORD), ("time_high", DWORD),
                ("cert_count", DWORD), ("certs", PTR), ("kind", DWORD), ("info", PTR),
                ("error", DWORD), ("counter_count", DWORD), ("counters", PTR), ("chain", PTR)]


class ProviderCert(c.Structure):
    _fields_ = [("size", DWORD), ("cert", PTR), ("commercial", DWORD), ("root", DWORD),
                ("self_signed", DWORD), ("test", DWORD), ("revoked_reason", DWORD),
                ("confidence", DWORD), ("error", DWORD), ("trust_list", PTR),
                ("trust_list_signer", DWORD), ("ctl", PTR), ("ctl_error", DWORD),
                ("cyclic", DWORD), ("element", PTR)]


class EnhancedUsage(c.Structure):
    _fields_ = [("count", DWORD), ("oids", c.POINTER(c.c_char_p))]


def _dll(name):
    if os.name != "nt":
        raise OSError("Windows signature verification requires Windows")
    # Search only Windows' system directory, never the download or current folder.
    return c.WinDLL(name, use_last_error=True, winmode=0x800)


def _api(dll, name, result, *arguments):
    fn = getattr(dll, name)
    fn.restype, fn.argtypes = result, list(arguments)
    return fn


def _certificate_identity(crypt, cert):
    name = _api(crypt, "CertGetNameStringW", DWORD, PTR, DWORD, DWORD, PTR, c.c_wchar_p, DWORD)
    length = name(cert, 4, 0, None, None, 0)  # CERT_NAME_SIMPLE_DISPLAY_TYPE
    if not 1 < length <= 32768:
        raise ValueError("missing signer name")
    buffer = c.create_unicode_buffer(length)
    if name(cert, 4, 0, None, buffer, length) != length:
        raise ValueError("cannot read signer name")
    usage = _api(crypt, "CertGetEnhancedKeyUsage", c.c_int32, PTR, DWORD, PTR, c.POINTER(DWORD))
    size = DWORD()
    # Read the signed certificate extension only, never a mutable local property.
    if not usage(cert, 2, None, c.byref(size)) or not c.sizeof(EnhancedUsage) <= size.value <= 1048576:
        raise ValueError("missing signer EKU extension")
    data = c.create_string_buffer(size.value)
    if not usage(cert, 2, data, c.byref(size)):
        raise ValueError("cannot read signer EKU extension")
    eku = c.cast(data, c.POINTER(EnhancedUsage)).contents
    if not 0 < eku.count <= 1024 or not eku.oids:
        raise ValueError("missing signer EKUs")
    return buffer.value, [eku.oids[i].decode("ascii") for i in range(eku.count)]


def verify_embedded_signature(path, handle):
    """Verify a caller-locked file; copy identities out before releasing trust state.

    Only single embedded signatures are supported. Catalogue-only, ambiguous multiple
    signatures, missing timestamps, and unverifiable revocation all fail closed.
    """
    trust, crypt = _dll("wintrust.dll"), _dll("crypt32.dll")
    verify = _api(trust, "WinVerifyTrust", c.c_int32, PTR, PTR, c.POINTER(TrustData))
    provider = _api(trust, "WTHelperProvDataFromStateData", PTR, PTR)
    signer_from = _api(trust, "WTHelperGetProvSignerFromChain", c.POINTER(Signer),
                       PTR, DWORD, c.c_int32, DWORD)
    cert_from = _api(trust, "WTHelperGetProvCertFromChain", c.POINTER(ProviderCert),
                     c.POINTER(Signer), DWORD)
    action = (c.c_ubyte * 16).from_buffer_copy(uuid.UUID("00aac56b-cd44-11d0-8cc2-00c04fc295ee").bytes_le)
    file = FileInfo(c.sizeof(FileInfo), os.path.abspath(path), handle, None)
    signature = SignatureSettings(c.sizeof(SignatureSettings), 0, 3, 0, 0, None)
    data = TrustData()
    data.size, data.ui, data.revocation, data.choice = c.sizeof(data), 2, 1, 1
    data.file, data.action, data.flags = c.pointer(file), 1, 0x80 | 0x2000
    data.signature = c.pointer(signature)
    try:
        status = verify(PTR(-1), action, c.byref(data))
        # This API returns LONG, not HRESULT. Exactly zero is the only success.
        if status != 0:
            raise ValueError(f"Windows rejected signature (0x{status & 0xffffffff:08x})")
        if signature.secondary != 0 or signature.verified != 0:
            raise ValueError("multiple signatures are not supported")
        state = provider(data.state)
        signer = signer_from(state, 0, False, 0) if state else None
        if not signer or signer.contents.error or not signer.contents.cert_count:
            raise ValueError("missing verified signer")
        if signer_from(state, 1, False, 0):
            raise ValueError("multiple primary signers are not supported")
        if signer.contents.counter_count != 1:
            raise ValueError("exactly one verified timestamp is required")
        timestamp = signer_from(state, 0, True, 0)
        if (not timestamp or timestamp.contents.error or not timestamp.contents.cert_count
                or not timestamp.contents.kind & 0x10):
            raise ValueError("missing verified timestamp")
        timestamp_cert = cert_from(timestamp, 0)
        if not timestamp_cert or not timestamp_cert.contents.cert or timestamp_cert.contents.error:
            raise ValueError("invalid timestamp certificate")
        cert = cert_from(signer, 0)
        if not cert or not cert.contents.cert or cert.contents.error:
            raise ValueError("missing verified signer certificate")
        publisher, oids = _certificate_identity(crypt, cert.contents.cert)
        return {"status": "Valid", "type": "Authenticode", "publisher": publisher,
                "timestamp": True, "identityOids": oids}
    finally:
        if data.state:
            data.action = 2  # WTD_STATEACTION_CLOSE, including failed verifications
            verify(PTR(-1), action, c.byref(data))
