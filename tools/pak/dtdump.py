#!/usr/bin/env python3
"""Generic decoder for cooked, unversioned UE5 DataTables whose row struct is a UserDefinedStruct.
Usage: python3 dtdump.py <DT.uasset> <DT.uexp> <RowStruct.uasset> <RowStruct.uexp>
Returns rows with byte ranges so rows can be byte-copied. Nested /Game user structs are not auto-loaded;
native structs are handled by a small registry (GameplayTag, GameplayTagContainer, LinearColor, Guid, GamePhaseUIDefinition).
"""
import os, struct, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from uasset import Package
from udstruct import parse_struct, pk_imports, fname, fstr, describe_ref
from dtrows import unversioned_header, read_text, read_gameplaytag, read_softpath, read_phase_ui

class Reader:
    def __init__(self, pk, imports, simports):
        self.pk, self.imports, self.simports = pk, imports, simports

    def value(self, f, b, p):
        t = f["type"]
        if t == "ByteProperty" or t == "EnumProperty": return b[p], p + 1
        if t == "BoolProperty": return bool(b[p]), p + 1
        if t == "IntProperty": return struct.unpack_from("<i", b, p)[0], p + 4
        if t == "UInt32Property": return struct.unpack_from("<I", b, p)[0], p + 4
        if t == "Int64Property": return struct.unpack_from("<q", b, p)[0], p + 8
        if t == "FloatProperty": return struct.unpack_from("<f", b, p)[0], p + 4
        if t == "DoubleProperty": return struct.unpack_from("<d", b, p)[0], p + 8
        if t == "NameProperty": return fname(b, p, self.pk)
        if t == "StrProperty": return fstr(b, p)
        if t == "TextProperty": return read_text(b, p, self.pk)
        if t in ("ObjectProperty", "ClassProperty"):
            i = struct.unpack_from("<i", b, p)[0]; return describe_ref(i, self.imports), p + 4
        if t in ("SoftObjectProperty", "SoftClassProperty"): return read_softpath(b, p, self.pk)
        if t == "ArrayProperty":
            n = struct.unpack_from("<i", b, p)[0]; p += 4; out = []
            for _ in range(n):
                v, p = self.value(f["inner"], b, p); out.append(v)
            return out, p
        if t == "StructProperty":
            sname = self.struct_name(f["struct"])
            if sname == "GameplayTagContainer":
                n = struct.unpack_from("<i", b, p)[0]; p += 4; tags = []
                for _ in range(n): tg, p = fname(b, p, self.pk); tags.append(tg)
                return tags, p
            if sname == "GameplayTag": return read_gameplaytag(b, p, self.pk)
            if sname == "Guid": return b[p:p+16].hex(), p + 16
            if sname == "GamePhaseUIDefinition": return read_phase_ui(b, p, self.pk)
            if sname == "LinearColor":  # native-serialized: 4 raw floats, no header
                r_, g_, b_, a_ = struct.unpack_from("<4f", b, p)
                return {"R": round(r_, 4), "G": round(g_, 4), "B": round(b_, 4), "A": round(a_, 4)}, p + 16
            raise ValueError(f"no schema for struct {sname} at {p}")
        raise ValueError(f"unhandled property type {t}")

    def struct_name(self, idx):
        if idx < 0: return self.simports[-idx-1][3]
        return f"export[{idx-1}]"

def parse_table(dt_uasset, dt_uexp, st_uasset, st_uexp):
    spk, simports, props, _, _ = parse_struct(st_uasset, st_uexp)
    pk = Package(dt_uasset); imports = pk_imports(pk); b = open(dt_uexp, "rb").read()
    rd = Reader(pk, imports, simports)
    p = 0
    hdr = struct.unpack_from("<H", b, p)[0]; p += 2; assert hdr == 0x0300, hex(hdr)
    p += 4 + 4  # RowStruct ref + trailer
    n = struct.unpack_from("<i", b, p)[0]; p += 4
    rows = []
    for _ in range(n):
        start = p
        name, p = fname(b, p, pk)
        present, p = unversioned_header(b, p)
        row = {}; offs = {}
        for idx, isp in present:
            f = props[idx]; key = f["name"].split("_")[0] if "_" in f["name"] else f["name"]
            if not isp: row[key] = 0; continue
            v0 = p
            row[key], p = rd.value(f, b, p)
            offs[key] = (v0 - start, p - start)  # relative to row start
        rows.append((name, start, p, row, offs))
    assert b[p:p+4] == bytes.fromhex("c1832a9e"), f"expected tag at {p}, got {b[p:p+8].hex()}"
    assert p + 4 == len(b)
    return pk, imports, b, rows, props

if __name__ == "__main__":
    pk, imports, b, rows, props = parse_table(*sys.argv[1:5])
    print("rows:", len(rows), "| uexp bytes:", len(b))
    for name, s, e, row, offs in rows:
        print(f"\n== row {name!r}  bytes [{s}, {e})  len={e-s}")
        for k, v in row.items(): print(f"   {k:24} {v}")
