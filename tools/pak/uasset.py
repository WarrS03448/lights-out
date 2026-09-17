#!/usr/bin/env python3
"""Minimal reader for cooked UE5 (legacy, non-IoStore) .uasset package headers: summary, names, imports, exports.
Usage: python3 uasset.py <file.uasset>
"""
import os, struct, sys

def fstr(b, p):
    n = struct.unpack_from("<i", b, p)[0]; p += 4
    if n < 0:
        s = b[p:p + (-n) * 2].decode("utf-16-le").rstrip("\x00"); p += (-n) * 2
    elif n == 0:
        s = ""
    else:
        s = b[p:p + n].decode("utf-8", "replace").rstrip("\x00"); p += n
    return s, p

class Package:
    def __init__(self, path):
        b = self.b = open(path, "rb").read()
        p = 0
        (tag, legacy_ver, legacy_ue3, ver4, ver5, lic) = struct.unpack_from("<IiiiiI", b, p); p += 24
        assert tag == 0x9E2A83C1, hex(tag)
        self.legacy_ver, self.ver4, self.ver5 = legacy_ver, ver4, ver5
        cv_count = struct.unpack_from("<i", b, p)[0]; p += 4 + cv_count * 20
        self.total_header_size = struct.unpack_from("<i", b, p)[0]; p += 4
        self.package_name, p = fstr(b, p)
        self.package_flags = struct.unpack_from("<I", b, p)[0]; p += 4
        self.name_count, self.name_offset = struct.unpack_from("<ii", b, p); p += 8
        self.summary_fields_after_names = p
        self._parse_names()
        # Locate export/import tables heuristically from the remaining summary ints.
        ints = struct.unpack_from("<%di" % ((self.name_offset - p) // 4), b, p)
        self.summary_tail_ints = ints
    def _parse_names(self):
        b = self.b; p = self.name_offset; self.names = []
        for _ in range(self.name_count):
            s, p = fstr(b, p)
            p += 4  # two uint16 hashes
            self.names.append(s)
        self.name_table_end = p
    def name(self, idx, number=0):
        s = self.names[idx]
        return s if number == 0 else f"{s}_{number-1}"

if __name__ == "__main__":
    pk = Package(sys.argv[1])
    print(f"legacy={pk.legacy_ver} ue4={pk.ver4} ue5={pk.ver5} headerSize={pk.total_header_size} pkg={pk.package_name!r} flags={hex(pk.package_flags)}")
    print(f"unversioned properties: {bool(pk.package_flags & 0x2000)}  filterEditorOnly: {bool(pk.package_flags & 0x80000000)}")
    print(f"names ({pk.name_count}) @ {pk.name_offset}..{pk.name_table_end}:")
    for i, n in enumerate(pk.names): print(f"  [{i}] {n}")
    print("summary ints after NameOffset:", pk.summary_tail_ints)
