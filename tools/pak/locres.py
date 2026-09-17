#!/usr/bin/env python3
"""Unreal .locres (version 2/3 'Optimized') parser + serializer with byte-exact round trip, plus the
hashes needed to ADD entries (verified against every one of the 1,560 namespace/key/source hashes in
Bodycam's en/Game.locres, 2026-09-14):

  * namespace / key hash  (FTextKey, locres v3 "Optimized_CityHash64_UTF16"):
        GetTypeHash(CityHash64(UTF-16LE bytes of the string)) = (uint32)h + 23 * (uint32)(h >> 32); "" -> 0
  * source-string hash    (FTextLocalizationResource::HashString = FCrc::StrCrc32<TCHAR>):
        standard CRC-32 over, for every UTF-16 code unit, the 4 bytes [lo, hi, 0, 0]

The runtime looks a text up by (namespace, key) and only uses the translation if the entry's source hash
equals the hash of the text's own source string — so add() needs the exact source (the English) too.

Usage: from locres import LocRes; l = LocRes.load(path); l.add("CommunityGamemodes", "CTF.Title", "CAPTURE THE FLAG", "FLAGGE EROBERN"); l.save(out)
"""
import struct
import os
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from paklib import read_fstring as _read_fstring, write_fstring as _write_fstring  # noqa: E402
from cityhash import cityhash64  # noqa: E402

M32 = 0xFFFFFFFF


def key_hash(s: str) -> int:
    """FTextKey hash of a namespace or key (locres v3)."""
    if not s:
        return 0
    h = cityhash64(s.encode("utf-16-le"))
    return ((h & M32) + 23 * (h >> 32)) & M32


def source_hash(s: str) -> int:
    """FCrc::StrCrc32 over the UTF-16 code units of the SOURCE string."""
    b = bytearray()
    for unit in struct.unpack(f"<{len(s.encode('utf-16-le')) // 2}H", s.encode("utf-16-le")):
        b += bytes((unit & 0xFF, (unit >> 8) & 0xFF, 0, 0))
    return zlib.crc32(bytes(b)) & M32


class RawStr(str):
    """str that remembers its original serialized bytes."""
    raw = None


def read_fstring(b, p):
    s, p2 = _read_fstring(b, p)
    r = RawStr(s)
    r.raw = bytes(b[p:p2])
    return r, p2


def write_fstring(s):
    if isinstance(s, RawStr) and s.raw is not None:
        return s.raw
    return _write_fstring(s)

MAGIC = bytes.fromhex("0E14747567 4A03FC4A15909DC3377F1B".replace(" ", ""))


class LocRes:
    def __init__(self):
        self.version = 0
        self.namespaces = []  # list of (ns_hash, ns, [(key_hash, key, src_hash, str_index), ...])
        self.strings = []     # list of (string, refcount)

    @classmethod
    def load(cls, path):
        b = open(path, "rb").read()
        self = cls()
        assert b[:16] == MAGIC, "not a locres file"
        p = 16
        self.version = b[p]; p += 1
        assert self.version in (2, 3), f"unsupported locres version {self.version}"
        strings_offset = struct.unpack_from("<q", b, p)[0]; p += 8
        total_entries = struct.unpack_from("<I", b, p)[0]; p += 4
        ns_count = struct.unpack_from("<I", b, p)[0]; p += 4
        for _ in range(ns_count):
            ns_hash = struct.unpack_from("<I", b, p)[0]; p += 4
            ns, p = read_fstring(b, p)
            key_count = struct.unpack_from("<I", b, p)[0]; p += 4
            keys = []
            for _ in range(key_count):
                key_hash = struct.unpack_from("<I", b, p)[0]; p += 4
                key, p = read_fstring(b, p)
                src_hash, idx = struct.unpack_from("<Ii", b, p); p += 8
                keys.append([key_hash, key, src_hash, idx])
            self.namespaces.append([ns_hash, ns, keys])
        assert p == strings_offset, (p, strings_offset)
        n = struct.unpack_from("<i", b, p)[0]; p += 4
        for _ in range(n):
            s, p = read_fstring(b, p)
            rc = struct.unpack_from("<i", b, p)[0]; p += 4
            self.strings.append([s, rc])
        assert p == len(b), (p, len(b))
        assert total_entries == sum(len(k) for _, _, k in self.namespaces)
        self._raw_strings = None
        return self

    def dumps(self):
        head = bytearray(MAGIC)
        head += bytes([self.version])
        body = bytearray()
        total = sum(len(k) for _, _, k in self.namespaces)
        body += struct.pack("<I", total)
        body += struct.pack("<I", len(self.namespaces))
        for ns_hash, ns, keys in self.namespaces:
            body += struct.pack("<I", ns_hash) + write_fstring(ns) + struct.pack("<I", len(keys))
            for key_hash, key, src_hash, idx in keys:
                body += struct.pack("<I", key_hash) + write_fstring(key) + struct.pack("<Ii", src_hash, idx)
        strings_offset = len(head) + 8 + len(body)
        out = head + struct.pack("<q", strings_offset) + body
        out += struct.pack("<i", len(self.strings))
        for s, rc in self.strings:
            out += write_fstring(s) + struct.pack("<i", rc)
        return bytes(out)

    def save(self, path):
        open(path, "wb").write(self.dumps())

    def entries(self):
        for ns_hash, ns, keys in self.namespaces:
            for kh, key, src_hash, idx in keys:
                yield ns, key, src_hash, idx, self.strings[idx][0]

    # ---------------------------------------------------------------- editing
    def _string_index(self, text: str) -> int:
        """Index of `text` in the string table (shared, refcounted), appending it if new."""
        for i, (s, rc) in enumerate(self.strings):
            if str(s) == text:
                self.strings[i][1] = rc + 1
                return i
        self.strings.append([text, 1])
        return len(self.strings) - 1

    def add(self, ns: str, key: str, source: str, text: str) -> None:
        """Add (or replace) the entry `ns`/`key`: shown as `text` when the game's text has source string `source`."""
        for entry in self.namespaces:
            if str(entry[1]) == ns:
                keys = entry[2]
                break
        else:
            keys = []
            self.namespaces.append([key_hash(ns), ns, keys])
        for k in keys:
            if str(k[1]) == key:                      # replace: drop the old string reference
                old = k[3]
                self.strings[old][1] -= 1
                k[2] = source_hash(source)
                k[3] = self._string_index(text)
                return
        keys.append([key_hash(key), key, source_hash(source), self._string_index(text)])

    def get(self, ns: str, key: str):
        for _h, n, keys in self.namespaces:
            if str(n) == ns:
                for kh, k, sh, idx in keys:
                    if str(k) == key:
                        return str(self.strings[idx][0]), sh
        return None

    def check_hashes(self):
        """Every stored namespace/key hash must equal what the game computes; returns the mismatches."""
        bad = []
        for ns_hash, ns, keys in self.namespaces:
            if key_hash(str(ns)) != ns_hash:
                bad.append(("ns", str(ns)))
            for kh, key, sh, idx in keys:
                if key_hash(str(key)) != kh:
                    bad.append(("key", str(ns), str(key)))
        return bad


if __name__ == "__main__":
    l = LocRes.load(sys.argv[1])
    print(f"version {l.version}, {len(l.namespaces)} namespaces, {sum(len(k) for _, _, k in l.namespaces)} entries, "
          f"{len(l.strings)} strings, hash mismatches: {len(l.check_hashes())}")
    for ns, key, sh, idx, text in l.entries():
        if len(sys.argv) < 3 or sys.argv[2].lower() in ns.lower() or sys.argv[2].lower() in key.lower():
            print(f"  {ns!r} {key!r} -> {text!r}")
