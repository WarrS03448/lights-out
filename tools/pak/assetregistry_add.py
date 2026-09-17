#!/usr/bin/env python3
"""Append asset records to a cooked UE5 AssetRegistry.bin (registry v17, name-batch + fixed tag store).
Usage: python3 assetregistry_add.py <in.bin> <out.bin> <template package path> <new package path> [<new package path> ...]
Each new record is a copy of the template's record (class, tag-map handle, chunks, flags) with the package path,
package name and asset name replaced; new names are appended to the name batch with correct CityHash64 hashes.
Round trip without additions is byte-identical (checked on Bodycam's 68 MB registry).
"""
import struct, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cityhash import name_batch_hash

class Registry:
    def __init__(self, data):
        d = data; self.header = d[:24]
        p = 24
        self.num, self.nbytes = struct.unpack_from("<II", d, p); p += 8
        self.algo = struct.unpack_from("<Q", d, p)[0]; p += 8
        self.hashes = list(struct.unpack_from("<%dQ" % self.num, d, p)); p += 8 * self.num
        headers = [struct.unpack_from(">H", d, p + 2 * i)[0] for i in range(self.num)]; p += 2 * self.num
        self.names = []   # (is_wide, str)
        for h in headers:
            ln = h & 0x7fff
            if h >> 15: self.names.append((True, d[p:p + 2 * ln].decode("utf-16-le"))); p += 2 * ln
            else: self.names.append((False, d[p:p + ln].decode("latin-1"))); p += ln
        end = d.find(struct.pack("<I", 0x87654321), p); assert end > 0
        self.tagstore = d[p:end + 4]; p = end + 4
        n_assets = struct.unpack_from("<i", d, p)[0]; p += 4
        self.records = []
        for _ in range(n_assets):
            s0 = p
            for _ in range(5):
                v = struct.unpack_from("<I", d, p)[0]; p += 4 + (4 if v & 0x80000000 else 0)
            p += 8   # tag map handle
            v = struct.unpack_from("<I", d, p)[0]; p += 4 + (4 if v & 0x80000000 else 0)   # OptionalOuterPath
            nch = struct.unpack_from("<i", d, p)[0]; p += 4 + 4 * nch
            p += 4   # package flags
            self.records.append(d[s0:p])
        self.trailer = d[p:]
        self.index = {s: i for i, (w, s) in enumerate(self.names)}

    def name_id(self, s):
        if s in self.index: return self.index[s]
        wide = not all(ord(c) < 128 for c in s)
        self.names.append((wide, s)); self.hashes.append(name_batch_hash(s)); self.index[s] = len(self.names) - 1
        return len(self.names) - 1

    @staticmethod
    def parse_record(rec):
        p = 0; fields = []
        for _ in range(5):
            v = struct.unpack_from("<I", rec, p)[0]; p += 4; n = 0
            if v & 0x80000000: n = struct.unpack_from("<I", rec, p)[0]; p += 4
            fields.append((v & 0x7fffffff, n))
        tag = rec[p:p + 8]; p += 8
        v = struct.unpack_from("<I", rec, p)[0]; p += 4; n = 0
        if v & 0x80000000: n = struct.unpack_from("<I", rec, p)[0]; p += 4
        outer = (v & 0x7fffffff, n)
        rest = rec[p:]
        return fields, tag, outer, rest

    def find(self, package_path):
        for r in self.records:
            f, _, _, _ = self.parse_record(r)
            if self.names[f[3][0]][1] == package_path and f[3][1] == 0: return r
        raise KeyError(package_path)

    def add_like(self, template_pkg, new_pkg):
        fields, tag, outer, rest = self.parse_record(self.find(template_pkg))
        folder, asset = new_pkg.rsplit("/", 1)
        def enc(idx, num): return struct.pack("<I", idx | 0x80000000) + struct.pack("<I", num) if num else struct.pack("<I", idx)
        rec = enc(self.name_id(folder), 0) + enc(fields[1][0], fields[1][1]) + enc(fields[2][0], fields[2][1]) \
            + enc(self.name_id(new_pkg), 0) + enc(self.name_id(asset), 0) + tag + enc(outer[0], outer[1]) + rest
        self.records.append(rec); return rec

    def serialize(self):
        # The two loops below append to LISTS and join once. Appending to `bytes` instead (the
        # obvious `strings += b`) copies the whole buffer every iteration, which on Bodycam's
        # 88k-name batch is quadratic: it cost 27 s of a 30 s install (measured 2026-09-16), i.e.
        # the entire "this takes about a minute". Byte-for-byte identical output either way.
        out = bytearray(self.header)
        strings = []; headers = []
        for wide, s in self.names:
            if wide: strings.append(s.encode("utf-16-le")); headers.append(struct.pack(">H", 0x8000 | len(s)))
            else: strings.append(s.encode("latin-1")); headers.append(struct.pack(">H", len(s)))
        strings = b"".join(strings); headers = b"".join(headers)
        out += struct.pack("<II", len(self.names), len(strings)) + struct.pack("<Q", self.algo)
        out += struct.pack("<%dQ" % len(self.hashes), *self.hashes) + headers + strings
        out += self.tagstore + struct.pack("<i", len(self.records)) + b"".join(self.records) + self.trailer
        return bytes(out)

if __name__ == "__main__":
    src = open(sys.argv[1], "rb").read(); reg = Registry(src)
    assert reg.serialize() == src, "round trip mismatch"
    for new_pkg in sys.argv[4:]:
        reg.add_like(sys.argv[3], new_pkg); print("added", new_pkg)
    out = reg.serialize(); open(sys.argv[2], "wb").write(out)
    chk = Registry(out); assert chk.serialize() == out and len(chk.records) == len(reg.records)
    print("OK", sys.argv[2], len(out), "B, names", len(reg.names), "assets", len(reg.records))
