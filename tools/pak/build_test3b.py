#!/usr/bin/env python3
"""Test #3b builder: a fully matched custom card.
Adds row key DeathMatch_99 (FName "DeathMatch", number 100) to THREE tables, all byte-cloned from Deathmatch:
  1. UI/Menus/Play/Cards/Data/DT_UI_CustomGameModes  (card: title/desc replaced; Deathmatch card retitled as in Test #2)
  2. GM/DATA/DT/DT_GameModeData                      (enum + DA_GameModeDeathmatch config)
  3. GM/DATA/DT/DT_GamemodeInfo                      (ruleset; MaxKill 30 -> 5 so a match ends fast and visibly)
Writes DTTest_P.pak containing all six files (replaces the Test #2 pak of the same name).
Usage: python3 build_test3b.py <ext_ui_dir> <ext26_dir> <outdir>
"""
import hashlib, os, struct, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dtrows, dtdump, paklib
from uasset import Package
from build_dt_test import base_text, text_len

NEW_NUMBER = 100  # FName number -> displays as _99

def patch_uasset(uasset_bytes, new_serial):
    """Update export[0].SerialSize and summary BulkDataStartOffset for a package whose export data changed size."""
    pk = Package.__new__(Package); pk.b = uasset_bytes
    a = bytearray(uasset_bytes)
    # re-parse minimal summary to find export offset
    p = 24; cv = struct.unpack_from("<i", a, p)[0]; p += 4 + cv * 20
    header_size = struct.unpack_from("<i", a, p)[0]; p += 4
    n = struct.unpack_from("<i", a, p)[0]; p += 4 + (n if n >= 0 else -n * 2)
    p += 4  # flags
    name_count, name_offset = struct.unpack_from("<ii", a, p); p += 8
    ints = struct.unpack_from("<%di" % ((name_offset - p) // 4), a, p)
    export_count, export_offset = ints[4], ints[5]
    assert export_count == 1
    ss_at = export_offset + 28
    old_serial = struct.unpack_from("<q", a, ss_at)[0]
    old_bulk = header_size + old_serial
    # find BulkDataStartOffset (int64 == header + old serial) within the summary
    bulk_at = a.find(struct.pack("<q", old_bulk), 0, name_offset)
    assert bulk_at > 0, "BulkDataStartOffset not found"
    struct.pack_into("<q", a, ss_at, new_serial)
    struct.pack_into("<q", a, bulk_at, header_size + new_serial)
    return bytes(a), old_serial

def clone_row(row_bytes, number):
    idx = struct.unpack_from("<i", row_bytes, 0)[0]
    return struct.pack("<ii", idx, number) + row_bytes[8:]

def main(ui_dir, gm_dir, outdir):
    os.makedirs(outdir, exist_ok=True)
    files = {}

    # ---- 1. UI table (same as Test #2, new description) ----
    ua = os.path.join(ui_dir, "UI__Menus__Play__Cards__Data__DT_UI_CustomGameModes.uasset")
    ue = os.path.join(ui_dir, "UI__Menus__Play__Cards__Data__DT_UI_CustomGameModes.uexp")
    pk, imports, b, rowstruct, rows = dtrows.parse_table(ua, ue)
    assert rows[0][0] == "DeathMatch"
    ds, de = rows[0][1], rows[0][2]; row = b[ds:de]
    name_at = 11; name_len = text_len(row, name_at); desc_at = name_at + name_len; desc_len = text_len(row, desc_at)
    mod_row = row[:name_at] + base_text("Deathmatch [DT TEST]") + row[desc_at:]
    new_row = (struct.pack("<ii", struct.unpack_from("<i", row, 0)[0], NEW_NUMBER) + row[8:name_at]
               + base_text("COMMUNITY TEST MODE")
               + base_text("Deathmatch, first to 5 kills. Added by a community mod pak.")
               + row[desc_at + desc_len:])
    body = b[:10] + struct.pack("<i", len(rows) + 1) + mod_row + b[de:len(b) - 4] + new_row + b[len(b) - 4:]
    a2, _ = patch_uasset(open(ua, "rb").read(), len(body) - 4)
    files["Bodycam/Content/UI/Menus/Play/Cards/Data/DT_UI_CustomGameModes.uasset"] = a2
    files["Bodycam/Content/UI/Menus/Play/Cards/Data/DT_UI_CustomGameModes.uexp"] = body
    open(os.path.join(outdir, "ui.uasset"), "wb").write(a2); open(os.path.join(outdir, "ui.uexp"), "wb").write(body)
    pk2, _, _, _, rows2 = dtrows.parse_table(os.path.join(outdir, "ui.uasset"), os.path.join(outdir, "ui.uexp"))
    assert len(rows2) == 11 and rows2[10][0] == "DeathMatch_99"

    # ---- 2. DT_GameModeData ----
    ga = os.path.join(gm_dir, "GM__DATA__DT__DT_GameModeData.uasset"); ge = os.path.join(gm_dir, "GM__DATA__DT__DT_GameModeData.uexp")
    sa = os.path.join(gm_dir, "GM__DATA__Structure__STR_GameModeData.uasset"); se = os.path.join(gm_dir, "GM__DATA__Structure__STR_GameModeData.uexp")
    pk, imports, b, rows, props = dtdump.parse_table(ga, ge, sa, se)
    assert rows[0][0] == "DeathMatch" and rows[0][3]["GameMode"] == 2
    new_row = clone_row(b[rows[0][1]:rows[0][2]], NEW_NUMBER)
    body = b[:10] + struct.pack("<i", len(rows) + 1) + b[14:len(b) - 4] + new_row + b[len(b) - 4:]
    a2, _ = patch_uasset(open(ga, "rb").read(), len(body) - 4)
    files["Bodycam/Content/GM/DATA/DT/DT_GameModeData.uasset"] = a2
    files["Bodycam/Content/GM/DATA/DT/DT_GameModeData.uexp"] = body
    open(os.path.join(outdir, "gmd.uasset"), "wb").write(a2); open(os.path.join(outdir, "gmd.uexp"), "wb").write(body)
    _, _, _, rows2, _ = dtdump.parse_table(os.path.join(outdir, "gmd.uasset"), os.path.join(outdir, "gmd.uexp"), sa, se)
    assert len(rows2) == 9 and rows2[8][0] == "DeathMatch_99" and rows2[8][3] == rows[0][3]

    # ---- 3. DT_GamemodeInfo (MaxKill 30 -> 5 in the clone) ----
    ia = os.path.join(gm_dir, "GM__DATA__DT__DT_GamemodeInfo.uasset"); ie = os.path.join(gm_dir, "GM__DATA__DT__DT_GamemodeInfo.uexp")
    wa = os.path.join(gm_dir, "GM__DATA__Structure__STR_GamemodeWinInfo.uasset"); we = os.path.join(gm_dir, "GM__DATA__Structure__STR_GamemodeWinInfo.uexp")
    pk, imports, b, rows, props = dtdump.parse_table(ia, ie, wa, we)
    assert rows[0][0] == "Deathmatch" and rows[0][3]["MaxKill"] == 30
    src = b[rows[0][1]:rows[0][2]]
    # locate MaxKill: the int32 30 that sits between AdjustTimePerKill(bool) and ChooseTeam(bool). Find unique pattern
    # GamemodeTime double 600.0, AdjustTimePerKill 0x00, MaxKill int32 30, ChooseTeam 0x00
    maxkill_at, mk_end = rows[0][4]["MaxKill"]; assert mk_end - maxkill_at == 4
    assert struct.unpack_from("<i", src, maxkill_at)[0] == 30
    new_row = bytearray(clone_row(src, NEW_NUMBER))
    struct.pack_into("<i", new_row, maxkill_at, 5)
    new_row = bytes(new_row)
    body = b[:10] + struct.pack("<i", len(rows) + 1) + b[14:len(b) - 4] + new_row + b[len(b) - 4:]
    a2, _ = patch_uasset(open(ia, "rb").read(), len(body) - 4)
    files["Bodycam/Content/GM/DATA/DT/DT_GamemodeInfo.uasset"] = a2
    files["Bodycam/Content/GM/DATA/DT/DT_GamemodeInfo.uexp"] = body
    open(os.path.join(outdir, "gmi.uasset"), "wb").write(a2); open(os.path.join(outdir, "gmi.uexp"), "wb").write(body)
    _, _, _, rows2, _ = dtdump.parse_table(os.path.join(outdir, "gmi.uasset"), os.path.join(outdir, "gmi.uexp"), wa, we)
    assert len(rows2) == 11 and rows2[10][0] == "Deathmatch_99"
    d = {k: (rows[0][3][k], rows2[10][3][k]) for k in rows[0][3] if rows[0][3][k] != rows2[10][3][k]}
    assert d == {"MaxKill": (30, 5)}, d
    for i in range(10): assert rows2[i][3] == rows[i][3]

    # ---- pak ----
    pak = os.path.join(outdir, "DTTest_P.pak")
    paklib.write_pak(pak, "../../../", files, seed=0)
    r = paklib.PakReader(pak)
    assert set(r.files) == set(files)
    for rel, data in files.items():
        e, blocks = r.raw_blocks(rel); assert blocks[0] == data and e["encrypted"] == 0
        assert paklib.fnv64_path(rel, r.seed) in r.phi
    print("OK", pak, os.path.getsize(pak), "bytes sha256", hashlib.sha256(open(pak, "rb").read()).hexdigest())
    for rel in sorted(files): print("  ", rel, len(files[rel]), "B")

if __name__ == "__main__":
    main(*sys.argv[1:4])
