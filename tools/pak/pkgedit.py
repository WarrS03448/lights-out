#!/usr/bin/env python3
"""Cooked UE5 (legacy/non-IoStore, unversioned) package header editor with byte-exact round trip.

Models the .uasset header: summary, name table, imports, exports, depends, asset-registry data, preload deps.
Supports: rename package, add names, add imports, add preload dependencies, retarget exports; recomputes every offset.
Usage (library): pk = CookedPackage.load(path); ...; data = pk.serialize()
Self-test:      python3 pkgedit.py <file.uasset> [...]   (asserts serialize(load(x)) == x for each file)
"""
import os, struct, sys, zlib

def read_fstring(b, p):
    n = struct.unpack_from("<i", b, p)[0]; p += 4
    if n < 0: return b[p:p + (-n)*2].decode("utf-16-le").rstrip("\x00"), p + (-n)*2
    if n == 0: return "", p
    return b[p:p+n].decode("utf-8").rstrip("\x00"), p + n

def write_fstring(s):
    if s == "": return struct.pack("<i", 0)
    if all(ord(c) < 128 for c in s):
        d = s.encode("ascii") + b"\0"; return struct.pack("<i", len(d)) + d
    d = s.encode("utf-16-le") + b"\0\0"; return struct.pack("<i", -(len(d)//2)) + d

def case_preserving_hash(s):   # FCrc::StrCrc32 (each char widened to 32 bits) & 0xffff — verified against the game
    return zlib.crc32(s.encode("utf-32-le")) & 0xffff

_T = []
for _i in range(256):
    _c = _i << 24
    for _ in range(8): _c = ((_c << 1) ^ 0x04C11DB7) if (_c & 0x80000000) else (_c << 1)
    _T.append(_c & 0xffffffff)
def non_case_preserving_hash(s):  # best-effort FCrc::Strihash_DEPRECATED; the loader discards these ("DummyHashes")
    h = 0
    for ch in s.upper():
        code = ord(ch)
        for B in (code & 0xffff, (code >> 8) & 0xffff):
            h = ((h >> 8) & 0x00ffffff) ^ _T[(h ^ B) & 0xff]
    return h & 0xffff

EXPORT_FMT = "<iiiiiiIqqiiiiIiiiiiiii"   # 96 bytes
EXPORT_FIELDS = ["class_idx","super_idx","template_idx","outer_idx","name_idx","name_num","object_flags","serial_size",
                 "serial_offset","forced_export","not_for_client","not_for_server","inherited_instance","package_flags",
                 "not_always_loaded","is_asset","generate_public_hash","first_dep","ser_before_ser","create_before_ser",
                 "ser_before_create","create_before_create"]

class CookedPackage:
    @classmethod
    def load(cls, path, lenient=False):
        """lenient=True: read-only load of packages with data resources / world tiles (no round-trip guarantee)."""
        self = cls(); b = open(path, "rb").read(); self.src = b; self.lenient = lenient
        p = 0
        self.tag, self.legacy_ver, self.legacy_ue3, self.ver4, self.ver5, self.lic = struct.unpack_from("<IiiiiI", b, p); p += 24
        assert self.tag == 0x9E2A83C1 and self.legacy_ver == -8
        cv = struct.unpack_from("<i", b, p)[0]; p += 4
        self.custom_versions = b[p:p + cv*20]; p += cv*20
        self.total_header_size = struct.unpack_from("<i", b, p)[0]; p += 4
        self.package_name, p = read_fstring(b, p)
        self.package_flags = struct.unpack_from("<I", b, p)[0]; p += 4
        self.name_count, self.name_offset = struct.unpack_from("<ii", b, p); p += 8
        (self.soft_paths_count, self.soft_paths_offset, self.gatherable_count, self.gatherable_offset,
         self.export_count, self.export_offset, self.import_count, self.import_offset, self.depends_offset,
         self.soft_pkg_count, self.soft_pkg_offset, self.searchable_offset, self.thumbnail_offset) = struct.unpack_from("<13i", b, p); p += 52
        self.guid = b[p:p+16]; p += 16
        gen = struct.unpack_from("<i", b, p)[0]; p += 4
        self.generations = [struct.unpack_from("<ii", b, p + 8*i) for i in range(gen)]; p += 8*gen
        self.engine_versions = b[p:p+28]; p += 28   # SavedBy + CompatibleWith (both with empty branch)
        self.compression_flags = struct.unpack_from("<I", b, p)[0]; p += 4
        cc = struct.unpack_from("<i", b, p)[0]; p += 4; assert cc == 0
        self.package_source = struct.unpack_from("<I", b, p)[0]; p += 4
        apc = struct.unpack_from("<i", b, p)[0]; p += 4; assert apc == 0
        self.ar_data_offset = struct.unpack_from("<i", b, p)[0]; p += 4
        self.bulk_start = struct.unpack_from("<q", b, p)[0]; p += 8
        self.world_tile_offset = struct.unpack_from("<i", b, p)[0]; p += 4
        ch = struct.unpack_from("<i", b, p)[0]; p += 4; self.chunk_ids = list(struct.unpack_from("<%di" % ch, b, p)); p += 4*ch
        self.preload_count, self.preload_offset = struct.unpack_from("<ii", b, p); p += 8
        self.names_from_export_data = struct.unpack_from("<i", b, p)[0]; p += 4
        self.payload_toc = struct.unpack_from("<q", b, p)[0]; p += 8
        self.data_resource_offset = struct.unpack_from("<i", b, p)[0]; p += 4
        assert p == self.name_offset, (p, self.name_offset)
        assert self.soft_paths_count == 0 and self.gatherable_count == 0 and self.soft_pkg_count == 0
        if not lenient:
            assert self.searchable_offset == 0 and self.thumbnail_offset == 0 and self.world_tile_offset == 0
            assert self.payload_toc == -1 and self.data_resource_offset == -1
        # names
        self.names = []
        for _ in range(self.name_count):
            s, p = read_fstring(b, p); h1, h2 = struct.unpack_from("<HH", b, p); p += 4; self.names.append([s, h1, h2])
        assert p == self.import_offset == self.soft_paths_offset
        self.imports = []
        for _ in range(self.import_count):
            self.imports.append(list(struct.unpack_from("<iiiiiii", b, p))); p += 28  # cp.idx cp.num cn.idx cn.num outer on.idx on.num
            self.imports[-1].append(struct.unpack_from("<i", b, p)[0]); p += 4      # bImportOptional
        assert p == self.export_offset
        self.exports = []
        for _ in range(self.export_count):
            self.exports.append(dict(zip(EXPORT_FIELDS, struct.unpack_from(EXPORT_FMT, b, p)))); p += 96
        assert p == self.depends_offset
        self.depends = b[p:self.ar_data_offset]; p = self.ar_data_offset
        self.ar_data = b[p:self.preload_offset]; p = self.preload_offset
        self.preload = list(struct.unpack_from("<%di" % self.preload_count, b, p)); p += 4*self.preload_count
        if lenient:
            self.trailer = b[p:]   # data-resource table etc. (not modelled)
        else:
            assert p == self.total_header_size == len(b), (p, self.total_header_size, len(b))
        return self

    def name_index(self, s):
        for i, (n, _, _) in enumerate(self.names):
            if n == s: return i
        raise KeyError(s)

    def add_name(self, s):
        try: return self.name_index(s)
        except KeyError:
            self.names.append([s, non_case_preserving_hash(s), case_preserving_hash(s)]); return len(self.names) - 1

    def add_import(self, class_package, class_name, outer_pkgidx, object_name):
        self.imports.append([self.add_name(class_package), 0, self.add_name(class_name), 0, outer_pkgidx, self.add_name(object_name), 0, 0])
        return -len(self.imports)  # package index of the new import

    def serialize(self):
        names = b"".join(write_fstring(s) + struct.pack("<HH", h1, h2) for s, h1, h2 in self.names)
        imports = b"".join(struct.pack("<iiiiiiii", *im) for im in self.imports)
        # layout: summary | names | imports | exports | depends | ar_data | preload
        def summary(name_off, imp_off, exp_off, dep_off, ar_off, pre_off, total, bulk):
            s = struct.pack("<IiiiiI", self.tag, self.legacy_ver, self.legacy_ue3, self.ver4, self.ver5, self.lic)
            s += struct.pack("<i", len(self.custom_versions)//20) + self.custom_versions
            s += struct.pack("<i", total) + write_fstring(self.package_name) + struct.pack("<I", self.package_flags)
            s += struct.pack("<ii", len(self.names), name_off)
            s += struct.pack("<13i", 0, imp_off, 0, 0, len(self.exports), exp_off, len(self.imports), imp_off, dep_off, 0, 0, 0, 0)
            s += self.guid + struct.pack("<i", len(self.generations)) + b"".join(struct.pack("<ii", *g) for g in self.generations)
            s += self.engine_versions + struct.pack("<Ii", self.compression_flags, 0) + struct.pack("<Ii", self.package_source, 0)
            s += struct.pack("<iq", ar_off, bulk) + struct.pack("<ii", 0, len(self.chunk_ids)) + struct.pack("<%di" % len(self.chunk_ids), *self.chunk_ids)
            s += struct.pack("<iii", len(self.preload), pre_off, self.names_from_export_data) + struct.pack("<qi", self.payload_toc, self.data_resource_offset)
            return s
        if self.generations: self.generations[-1] = (len(self.exports), len(self.names))
        trailer = getattr(self, "trailer", b"")   # data-resource table (lenient loads only); offsets inside it are export-relative
        summary_len = len(summary(0, 0, 0, 0, 0, 0, 0, 0))
        name_off = summary_len; imp_off = name_off + len(names); exp_off = imp_off + len(imports)
        dep_off = exp_off + 96*len(self.exports); ar_off = dep_off + len(self.depends); pre_off = ar_off + len(self.ar_data)
        total = pre_off + 4*len(self.preload) + len(trailer)
        if trailer: self.data_resource_offset = pre_off + 4*len(self.preload)
        # exports: serial offsets are absolute (header + offset within .uexp); keep their relative layout
        exports = b""; running = total; bulk = None
        for e in self.exports:
            e = dict(e); e["serial_offset"] = running; running += e["serial_size"]; exports += struct.pack(EXPORT_FMT, *[e[k] for k in EXPORT_FIELDS])
        bulk = running
        out = summary(name_off, imp_off, exp_off, dep_off, ar_off, pre_off, total, bulk) + names + imports + exports + self.depends + self.ar_data
        out += struct.pack("<%di" % len(self.preload), *self.preload) + trailer
        assert len(out) == total
        self.total_header_size = total
        return out

if __name__ == "__main__":
    for path in sys.argv[1:]:
        pk = CookedPackage.load(path); out = pk.serialize()
        print(f"{os.path.basename(path)}: round-trip {'OK' if out == pk.src else 'MISMATCH'} ({len(out)} B, names {len(pk.names)}, imports {len(pk.imports)}, exports {len(pk.exports)}, preload {pk.preload})")
        if out != pk.src:
            i = next(i for i in range(min(len(out), len(pk.src))) if out[i] != pk.src[i]); print("  first diff at", i, out[i:i+8].hex(), "vs", pk.src[i:i+8].hex())
