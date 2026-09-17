#!/usr/bin/env python3
"""Test #2 builder: override DT_UI_CustomGameModes with (a) a renamed Deathmatch card and (b) an appended 11th row.
Usage: python3 build_dt_test.py <orig.uasset> <orig.uexp> <outdir>
"""
import hashlib, os, struct, sys, uuid
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dtrows, paklib
from udstruct import fstr

REL = "Bodycam/Content/UI/Menus/Play/Cards/Data/DT_UI_CustomGameModes"
SERIAL_SIZE_AT = 5439      # int64 SerialSize of export[0] in the .uasset (verified)
BULK_START_AT = 229        # int64 BulkDataStartOffset in the summary (verified: == header + SerialSize)
HEADER_SIZE = 5619

def base_text(src):
    key = uuid.uuid4().hex.upper().encode() + b"\0"
    s = src.encode("ascii") + b"\0"
    return (struct.pack("<I", 0) + b"\x00" + struct.pack("<i", 1) + b"\0"
            + struct.pack("<i", len(key)) + key + struct.pack("<i", len(s)) + s)

def text_len(b, p):
    """Length in bytes of the FText at p (StringTable / Base / None)."""
    q = p + 4; ht = struct.unpack_from("<b", b, q)[0]; q += 1
    if ht == 11:
        q += 8; _, q = fstr(b, q)
    elif ht == 0:
        for _ in range(3): _, q = fstr(b, q)
    elif ht == -1:
        has = struct.unpack_from("<i", b, q)[0]; q += 4
        if has: _, q = fstr(b, q)
    else: raise ValueError(ht)
    return q - p

def main(uasset, uexp, outdir):
    os.makedirs(outdir, exist_ok=True)
    pk, imports, b, rowstruct, rows = dtrows.parse_table(uasset, uexp)
    assert len(rows) == 10 and rows[0][0] == "DeathMatch"
    ds, de = rows[0][1], rows[0][2]
    row = b[ds:de]
    # layout: FName(8) + header(2, 0x1f00) + GameMode byte(1) + Name FText + Description FText + ...
    assert struct.unpack_from("<H", row, 8)[0] == 0x1f00
    name_at = 11
    name_len = text_len(row, name_at)
    desc_at = name_at + name_len
    desc_len = text_len(row, desc_at)
    tail = row[desc_at + desc_len:]

    # (a) modified Deathmatch row: only the Name text changes
    mod_row = row[:name_at] + base_text("Deathmatch [DT TEST]") + row[desc_at:]
    # (b) new row: same data, new key "DeathMatch_99" (FName number 100), new Name + Description
    idx = struct.unpack_from("<i", row, 0)[0]
    new_row = (struct.pack("<ii", idx, 100) + row[8:name_at]
               + base_text("COMMUNITY TEST MODE")
               + base_text("Added by a community mod pak. If you can read this, extra DataTable rows load.")
               + tail)

    body = b[:10] + struct.pack("<i", 11) + mod_row + b[de:len(b) - 4] + new_row + b[len(b) - 4:]
    serial = len(body) - 4
    a = bytearray(pk.b)
    assert struct.unpack_from("<q", a, SERIAL_SIZE_AT)[0] == len(b) - 4
    assert struct.unpack_from("<q", a, BULK_START_AT)[0] == HEADER_SIZE + len(b) - 4
    struct.pack_into("<q", a, SERIAL_SIZE_AT, serial)
    struct.pack_into("<q", a, BULK_START_AT, HEADER_SIZE + serial)

    ua = os.path.join(outdir, "DT_UI_CustomGameModes.uasset"); ue = os.path.join(outdir, "DT_UI_CustomGameModes.uexp")
    open(ua, "wb").write(a); open(ue, "wb").write(body)

    # verify by re-parsing with the same decoder
    pk2, imp2, b2, rs2, rows2 = dtrows.parse_table(ua, ue)
    assert len(rows2) == 11 and rows2[10][0] == "DeathMatch_99"
    assert rows2[0][3]["Name"]["src"] == "Deathmatch [DT TEST]"
    assert rows2[10][3]["Name"]["src"] == "COMMUNITY TEST MODE"
    for i in range(1, 10): assert rows2[i][3] == rows[i][3], i
    for k in rows[0][3]:
        if k != "Name": assert rows2[0][3][k] == rows[0][3][k], k
    for k in rows[0][3]:
        if k not in ("Name", "Description"): assert rows2[10][3][k] == rows[0][3][k], k
    print("verified: 11 rows; modified row 0 and appended row 10; other rows byte-identical in meaning")

    pak = os.path.join(outdir, "DTTest_P.pak")
    paklib.write_pak(pak, "../../../", {REL + ".uasset": bytes(a), REL + ".uexp": body}, seed=0)
    r = paklib.PakReader(pak)
    assert set(r.files) == {REL + ".uasset", REL + ".uexp"}
    for rel, data in ((REL + ".uasset", bytes(a)), (REL + ".uexp", body)):
        e, blocks = r.raw_blocks(rel); assert blocks[0] == data and e["encrypted"] == 0
        assert paklib.fnv64_path(rel, r.seed) in r.phi
    print("pak OK:", pak, os.path.getsize(pak), "bytes sha256", hashlib.sha256(open(pak, "rb").read()).hexdigest())

if __name__ == "__main__":
    main(*sys.argv[1:4])
