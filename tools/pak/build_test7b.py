#!/usr/bin/env python3
"""Test #7b builder — the community mode gets its OWN GameMode class with its own config asset.

Clones GM/Gamemode/GM_Deathmatch -> /Game/GM/Gamemode/GM_CommunityTest and sets the class-default
GameModeConfig (unversioned property #12 in the GM_<X>_C schema, learned from GM_TeamDeathMatch's CDO) to
DA_CommunityTest (the 5-kill config from Test #4). Then rebuilds the Test #7 level so its World Settings name
GM_CommunityTest_C, and packs DTTest_v7_P.pak.

CDO bytes (GM_Deathmatch):        04 02 | 8d 05 | 02 | 02 | 0a 00 00 00 | 00 00 00 00
  fragments: (skip4, 1 value)=#4 GameMode enum byte; (skip13, zeros, last, 2 values)=#18 SpawnSystemComponent, #19 zero(mask 02)
CDO bytes (GM_CommunityTest):     04 02 | 07 02 | 85 05 | 02 | 02 | <DA import> | 0a 00 00 00 | 00 00 00 00
  fragments: #4; (skip7, 1 value)=#12 GameModeConfig; (skip5, zeros, last, 2 values)=#18, #19
Usage: python3 build_test7b.py <ext26all_dir> <extm> <ext24> <ext_ui> <t3b> <t4> <t7_dir> <outdir>
"""
import hashlib, os, struct, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paklib
from pkgedit import CookedPackage
from build_test7 import rename, add_dep, verify, main as build_level_pak

GM_PKG, GM_CLS = "/Game/GM/Gamemode/GM_CommunityTest", "GM_CommunityTest_C"
DA_PKG, DA_NAME = "/Game/GM/DATA/DataAsset/DA_CommunityTest", "DA_CommunityTest"

def build_class(ext26all):
    gm = CookedPackage.load(os.path.join(ext26all, "GM__Gamemode__GM_Deathmatch.uasset"))
    uexp = bytearray(open(os.path.join(ext26all, "GM__Gamemode__GM_Deathmatch.uexp"), "rb").read())
    assert gm.package_name == "/Game/GM/Gamemode/GM_Deathmatch"
    names = lambda: [n[0] for n in gm.names]
    # exports are laid out in order in the .uexp (serialize() relies on it)
    for a, b in zip(gm.exports, gm.exports[1:]): assert b["serial_offset"] == a["serial_offset"] + a["serial_size"]
    rename(gm, GM_PKG, "GM_Deathmatch_C", GM_CLS)
    for n in gm.names:
        if n[0] == "Default__GM_Deathmatch_C":
            n[0] = "Default__" + GM_CLS
            from pkgedit import non_case_preserving_hash, case_preserving_hash
            n[1], n[2] = non_case_preserving_hash(n[0]), case_preserving_hash(n[0])
    assert "GM_Deathmatch_C" not in names() and "Default__GM_Deathmatch_C" not in names()
    p_imp = gm.add_import("/Script/CoreUObject", "Package", 0, DA_PKG)
    o_imp = gm.add_import("/Script/Bodycam", "GameModeConfigDataAsset", p_imp, DA_NAME)
    cdo_i = next(i for i, e in enumerate(gm.exports) if names()[e["name_idx"]] == "Default__" + GM_CLS)
    cdo = gm.exports[cdo_i]; off = cdo["serial_offset"] - gm.total_header_size
    old = bytes(uexp[off:off + cdo["serial_size"]])
    assert old == bytes.fromhex("04 02 8d 05 02 02 0a 00 00 00 00 00 00 00"), old.hex()
    new = bytes.fromhex("04 02 07 02 85 05 02 02") + struct.pack("<i", o_imp) + old[6:]
    assert len(new) == 20
    uexp[off:off + cdo["serial_size"]] = new; cdo["serial_size"] = len(new); uexp = bytes(uexp)
    add_dep(gm, cdo_i, o_imp, "create_before_ser")   # GM_TeamDeathMatch pattern: the DA import sits in the CDO's CreateBeforeSerialization group
    uasset = gm.serialize()
    v = verify(uasset, uexp, "GM class")
    vn = [n[0] for n in v.names]
    assert vn[v.exports[0]["name_idx"]] == GM_CLS and vn[v.exports[cdo_i]["name_idx"]] == "Default__" + GM_CLS and v.package_name == GM_PKG
    # decode the new CDO fragments once more, independently
    d = uexp[v.exports[cdo_i]["serial_offset"] - len(uasset):][:20]
    h = [struct.unpack_from("<H", d, i)[0] for i in (0, 2, 4)]
    frag = [(x & 0x7f, bool(x & 0x80), bool(x & 0x100), x >> 9) for x in h]
    assert frag == [(4, False, False, 1), (7, False, False, 1), (5, True, True, 2)], frag
    assert d[6] == 0x02 and d[7] == 0x02 and struct.unpack_from("<i", d, 8)[0] == o_imp and d[12:16] == b"\x0a\x00\x00\x00" and d[16:] == b"\0\0\0\0"
    assert vn[v.imports[-o_imp-1][5]] == DA_NAME and vn[v.imports[-v.imports[-o_imp-1][4]-1][5]] == DA_PKG
    print(f"GM_CommunityTest: header {len(uasset)} B, uexp {len(uexp)} B, CDO -> import {o_imp} {DA_NAME}; property #4 (GameMode enum byte) = {d[7]}")
    return uasset, uexp

def main(ext26all, extm, ext24, extui, t3b, t4, t7dir, outdir):
    os.makedirs(outdir, exist_ok=True)
    cls_uasset, cls_uexp = build_class(ext26all)
    # rebuild the level/table set with the level's World Settings pointing at our class (writes <outdir>/DTTest_v6_P.pak as an intermediate)
    build_level_pak(extm, ext24, extui, t3b, t4, outdir, GM_PKG, GM_CLS)
    r6 = paklib.PakReader(os.path.join(outdir, "DTTest_v6_P.pak"))
    files = {rel: r6.raw_blocks(rel)[1][0] for rel in r6.files}
    files["Bodycam/Content/GM/Gamemode/GM_CommunityTest.uasset"] = cls_uasset
    files["Bodycam/Content/GM/Gamemode/GM_CommunityTest.uexp"] = cls_uexp
    os.remove(os.path.join(outdir, "DTTest_v6_P.pak"))
    pak = os.path.join(outdir, "DTTest_v7_P.pak")
    paklib.write_pak(pak, "../../../", files, seed=0)
    r = paklib.PakReader(pak); assert set(r.files) == set(files)
    for rel, data in files.items():
        en, blocks = r.raw_blocks(rel); assert blocks[0] == data and en["encrypted"] == 0 and paklib.fnv64_path(rel, r.seed) in r.phi
    # the level must reference our class, and our class must reference DA_CommunityTest, which must be in the pak
    lv = files["Bodycam/Content/GM_Maps/Community/DM_CT_BombHouse.umap"]; assert GM_PKG.encode() in lv and GM_CLS.encode() in lv
    assert DA_PKG.encode() in cls_uasset and "Bodycam/Content/GM/DATA/DataAsset/DA_CommunityTest.uasset" in files
    print("OK", pak, os.path.getsize(pak), "B sha256", hashlib.sha256(open(pak, "rb").read()).hexdigest())
    for rel in sorted(files): print("  ", rel, len(files[rel]), "B")

if __name__ == "__main__":
    main(*sys.argv[1:9])
