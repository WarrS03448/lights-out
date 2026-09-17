#!/usr/bin/env python3
"""Decode DT_UI_CustomGameModes-style rows (Struct_GameModeDefinition) from a cooked unversioned DataTable.
Usage: python3 dtrows.py <DT.uasset> <DT.uexp>
"""
import struct, sys
sys.path.insert(0, "/tmp")
from uasset import Package
from udstruct import pk_imports, fname, fstr, describe_ref

def unversioned_header(b, p):
    frags = []
    while True:
        v = struct.unpack_from("<H", b, p)[0]; p += 2
        skip, haszero, last, num = v & 0x7f, bool(v & 0x80), bool(v & 0x100), v >> 9
        frags.append((skip, haszero, num));
        if last: break
    # zero mask: one bit per value of the fragments flagged HasAnyZeroes only (FUnversionedHeader::Load counts ZeroMaskNum that
    # way and FIterator advances ZeroMaskIndex only inside such fragments); sized uint8 <= 8 bits, uint16 <= 16, else uint32 words
    nz = sum(f[2] for f in frags if f[1])
    mask = None
    if nz:
        nbytes = 1 if nz <= 8 else 2 if nz <= 16 else ((nz + 31) // 32) * 4
        mask = int.from_bytes(b[p:p+nbytes], "little"); p += nbytes
    # expand to list of (property_index, present)
    idx = 0; present = []; vi = 0
    for skip, hz, num in frags:
        idx += skip
        for _ in range(num):
            zero = False
            if hz: zero = bool(mask >> vi & 1); vi += 1
            present.append((idx, not zero)); idx += 1
    return present, p

def read_text(b, p, pk):
    flags = struct.unpack_from("<I", b, p)[0]; p += 4
    ht = struct.unpack_from("<b", b, p)[0]; p += 1
    if ht == -1:
        has = struct.unpack_from("<i", b, p)[0]; p += 4
        s = None
        if has: s, p = fstr(b, p)
        return {"flags": flags, "type": "None", "text": s}, p
    if ht == 0:
        ns, p = fstr(b, p); key, p = fstr(b, p); src, p = fstr(b, p)
        return {"flags": flags, "type": "Base", "ns": ns, "key": key, "src": src}, p
    if ht == 11:
        table, p = fname(b, p, pk); key, p = fstr(b, p)
        return {"flags": flags, "type": "StringTable", "table": table, "key": key}, p
    raise ValueError(f"unhandled FText history {ht} at {p}")

def read_gameplaytag(b, p, pk):
    present, p = unversioned_header(b, p)
    tag = None
    for idx, isp in present:
        if isp: tag, p = fname(b, p, pk)
    return tag, p

def read_softpath(b, p, pk):
    pkg, p = fname(b, p, pk); asset, p = fname(b, p, pk); sub, p = fstr(b, p)
    return f"{pkg}.{asset}" + (f":{sub}" if sub else ""), p

def read_phase_ui(b, p, pk):
    """GamePhaseUIDefinition (native, /Script/Bodycam): observed layout [GameplayTag, Struct(GameplayTag, SoftClass, ?, ?), float, ?]."""
    present, p = unversioned_header(b, p)
    out = {}
    for idx, isp in present:
        if not isp: continue
        if idx == 0: out["phase"], p = read_gameplaytag(b, p, pk)
        elif idx == 1:
            inner, p = unversioned_header(b, p); ui = {}
            for j, jp in inner:
                if not jp: continue
                if j == 0: ui["layer"], p = read_gameplaytag(b, p, pk)
                elif j == 1: ui["widget"], p = read_softpath(b, p, pk)
                else: raise ValueError(f"unknown inner UI field {j} present at {p}")
            out["ui"] = ui
        elif idx == 2: out["delay"] = struct.unpack_from("<f", b, p)[0]; p += 4
        else: raise ValueError(f"unknown GamePhaseUIDefinition field {idx} present at {p}")
    return out, p

FIELDS = ["GameMode", "Name", "Description", "Thumbnail", "Icon", "PlayerMin", "PlayerMax", "MapTable",
          "EstimateTime", "Tag", "IsActive", "SpecialEventType", "SpecialEventMessage", "EndGameTabs", "GamePhaseUIs"]

def read_row(b, p, pk, imports):
    present, p = unversioned_header(b, p)
    row = {}
    for idx, isp in present:
        f = FIELDS[idx]
        if not isp: row[f] = 0; continue
        if f == "GameMode": row[f] = b[p]; p += 1
        elif f in ("Name", "Description", "Tag", "SpecialEventMessage"): row[f], p = read_text(b, p, pk)
        elif f == "Thumbnail": row[f], p = read_softpath(b, p, pk)
        elif f in ("Icon", "MapTable"):
            i = struct.unpack_from("<i", b, p)[0]; p += 4; row[f] = describe_ref(i, imports)
        elif f in ("PlayerMin", "PlayerMax", "EstimateTime"): row[f] = struct.unpack_from("<i", b, p)[0]; p += 4
        elif f == "IsActive": row[f] = b[p]; p += 1
        elif f == "SpecialEventType": row[f], p = read_gameplaytag(b, p, pk)
        elif f == "EndGameTabs":
            n = struct.unpack_from("<i", b, p)[0]; p += 4; tags = []
            for _ in range(n): t, p = fname(b, p, pk); tags.append(t)
            row[f] = tags
        elif f == "GamePhaseUIs":
            n = struct.unpack_from("<i", b, p)[0]; p += 4; items = []
            for _ in range(n): it, p = read_phase_ui(b, p, pk); items.append(it)
            row[f] = items
    return row, p

def parse_table(uasset, uexp):
    pk = Package(uasset); imports = pk_imports(pk); b = open(uexp, "rb").read()
    p = 0
    hdr = struct.unpack_from("<H", b, p)[0]; p += 2; assert hdr == 0x0300, hex(hdr)
    rowstruct = struct.unpack_from("<i", b, p)[0]; p += 4
    p += 4  # trailer after UObject props
    n = struct.unpack_from("<i", b, p)[0]; p += 4
    rows = []
    for _ in range(n):
        start = p
        name, p = fname(b, p, pk)
        row, p = read_row(b, p, pk, imports)
        rows.append((name, start, p, row))
    assert b[p:p+4] == bytes.fromhex("c1832a9e"), f"expected package tag at {p}, got {b[p:p+8].hex()}"
    assert p + 4 == len(b)
    return pk, imports, b, rowstruct, rows

if __name__ == "__main__":
    pk, imports, b, rowstruct, rows = parse_table(sys.argv[1], sys.argv[2])
    print("RowStruct:", describe_ref(rowstruct, imports), "| rows:", len(rows), "| uexp bytes:", len(b))
    for name, s, e, row in rows:
        print(f"\n== row {name!r}  bytes [{s}, {e})  len={e-s}")
        for k, v in row.items(): print(f"   {k:20} {v}")
