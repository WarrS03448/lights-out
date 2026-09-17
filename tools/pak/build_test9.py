#!/usr/bin/env python3
"""Test #9 builder — why can't the host find a NEW level by short name? (v8 proved level edit + class + config work.)

Two changes vs v7, both mirroring how the stock content paks are laid out:
  * the pak is mounted at ../../../Bodycam/Content/ (like every stock content pak) instead of ../../../ (pakchunk0 style)
  * two community levels to compare in one launch:
      map "Bomb House A"  -> LevelName CT_BombHouse  -> GM_Maps/DeathMatch/DM_CT_BombHouse   (the STOCK folder)
      map "Bomb House B"  -> LevelName CT2_BombHouse -> GM_Maps/Community/DM_CT2_BombHouse   (our own folder)
Both levels: World Settings -> GM_CommunityTest_C (5-kill config). The stock-path override from v8 is dropped.
Usage: python3 build_test9.py <extm> <ext24> <ext_ui> <t3b> <t7b_dir> <outdir>
"""
import hashlib, os, struct, sys, uuid
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paklib, dtdump
from pkgedit import CookedPackage
from build_test7 import rename, add_dep, verify

GM_PKG, GM_CLS = "/Game/GM/Gamemode/GM_CommunityTest", "GM_CommunityTest_C"
MOUNT = "../../../Bodycam/Content/"

def clone_level(extm, new_pkg, new_name, gm_pkg, gm_cls):
    lv = CookedPackage.load(os.path.join(extm, "GM_Maps__DeathMatch__DM_BombHouse.umap"), lenient=True)
    lv_uexp = bytearray(open(os.path.join(extm, "GM_Maps__DeathMatch__DM_BombHouse.uexp"), "rb").read())
    assert lv.package_name == "/Game/GM_Maps/DeathMatch/DM_BombHouse"
    rename(lv, new_pkg, "DM_BombHouse", new_name)
    pkg_imp = lv.add_import("/Script/CoreUObject", "Package", 0, gm_pkg)
    cls_imp = lv.add_import("/Script/Engine", "BlueprintGeneratedClass", pkg_imp, gm_cls)
    lv.add_import(gm_pkg, gm_cls, pkg_imp, "Default__" + gm_cls)
    ws_i = len(lv.exports) - 1; ws = lv.exports[ws_i]; assert lv.names[ws["name_idx"]][0] == "WorldSettings"
    off = ws["serial_offset"] - lv.total_header_size; old = bytes(lv_uexp[off:off + ws["serial_size"]])
    assert old[:8] == bytes.fromhex("16 02 23 03 0c 00 00 00"), old[:8].hex()
    new = bytes.fromhex("16 02 0a 02 18 03") + old[4:8] + struct.pack("<i", cls_imp) + old[8:]
    lv_uexp[off:off + ws["serial_size"]] = new; ws["serial_size"] = len(new); lv_uexp = bytes(lv_uexp)
    add_dep(lv, ws_i, cls_imp, "create_before_ser")
    lv_uasset = lv.serialize(); v = verify(lv_uasset, lv_uexp, new_name)
    assert v.package_name == new_pkg and v.names[v.exports[16]["name_idx"]][0] == new_name
    return lv_uasset, lv_uexp

def base_text(src):
    key = uuid.uuid4().hex.upper().encode() + b"\0"; s = src.encode("ascii") + b"\0"
    return struct.pack("<I", 0) + b"\x00" + struct.pack("<i", 1) + b"\0" + struct.pack("<i", len(key)) + key + struct.pack("<i", len(s)) + s

def clone_metadata(ext24, new_pkg, new_name, level_name, display_name):
    da = CookedPackage.load(os.path.join(ext24, "UI__MetaData__DA_BombHouse.uasset"))
    u = bytearray(open(os.path.join(ext24, "UI__MetaData__DA_BombHouse.uexp"), "rb").read())
    rename(da, new_pkg, "DA_BombHouse", new_name)
    assert da.names[struct.unpack_from("<i", u, 4)[0]][0] == "BombHouse" and u[:4] == bytes.fromhex("00 08 02 05")
    struct.pack_into("<ii", u, 4, da.add_name(level_name), 0)
    # Texts[0]: StringTable(ST_Menu, "BombHouse") -> inline text, so the two test maps can be told apart in the list
    assert u[52:56] == b"\x02\x00\x00\x00" and u[56:61] == bytes.fromhex("00 00 00 00 0b"), u[52:61].hex()
    key_len = struct.unpack_from("<i", u, 69)[0]; old_end = 73 + key_len   # 56 flags(4)+hist(1)+table FName(8) = 69, key FString
    assert u[73:old_end] == b"BombHouse\0", u[73:old_end]
    u = bytes(u[:56]) + base_text(display_name) + bytes(u[old_end:])
    da.exports[0]["serial_size"] = len(u) - 4
    ua = da.serialize(); verify(ua, u, new_name)
    return ua, u

def main(extm, ext24, extui, t3b, t7b, outdir):
    os.makedirs(outdir, exist_ok=True)
    r7 = paklib.PakReader(os.path.join(t7b, "DTTest_v7_P.pak"))
    files = {}
    for rel in r7.files:
        if "GM_Maps/Community/DM_CT_BombHouse" in rel or "DA_CT_BombHouse" in rel or "DT_UI_CommunityTestMaps" in rel: continue
        assert rel.startswith("Bodycam/Content/"); files[rel[len("Bodycam/Content/"):]] = r7.raw_blocks(rel)[1][0]
    # levels
    a_ua, a_ue = clone_level(extm, "/Game/GM_Maps/DeathMatch/DM_CT_BombHouse", "DM_CT_BombHouse", GM_PKG, GM_CLS)
    b_ua, b_ue = clone_level(extm, "/Game/GM_Maps/Community/DM_CT2_BombHouse", "DM_CT2_BombHouse", GM_PKG, GM_CLS)
    files["GM_Maps/DeathMatch/DM_CT_BombHouse.umap"] = a_ua; files["GM_Maps/DeathMatch/DM_CT_BombHouse.uexp"] = a_ue
    files["GM_Maps/Community/DM_CT2_BombHouse.umap"] = b_ua; files["GM_Maps/Community/DM_CT2_BombHouse.uexp"] = b_ue
    # metadata
    da_a = clone_metadata(ext24, "/Game/UI/MetaData/DA_CT_BombHouse", "DA_CT_BombHouse", "CT_BombHouse", "Bomb House A (stock folder)")
    da_b = clone_metadata(ext24, "/Game/UI/MetaData/DA_CT2_BombHouse", "DA_CT2_BombHouse", "CT2_BombHouse", "Bomb House B (own folder)")
    files["UI/MetaData/DA_CT_BombHouse.uasset"], files["UI/MetaData/DA_CT_BombHouse.uexp"] = da_a
    files["UI/MetaData/DA_CT2_BombHouse.uasset"], files["UI/MetaData/DA_CT2_BombHouse.uexp"] = da_b
    # maps table with two rows
    mt = CookedPackage.load(os.path.join(extui, "UI__Menus__Play__Cards__Data__DT_UI_DeathmatchMaps.uasset"))
    mt_src = open(os.path.join(extui, "UI__Menus__Play__Cards__Data__DT_UI_DeathmatchMaps.uexp"), "rb").read()
    rename(mt, "/Game/UI/Menus/Play/Cards/Data/DT_UI_CommunityTestMaps", "DT_UI_DeathmatchMaps", "DT_UI_CommunityTestMaps")
    rows = b""
    for key, pkg, name in (("BombHouseA", "/Game/UI/MetaData/DA_CT_BombHouse", "DA_CT_BombHouse"), ("BombHouseB", "/Game/UI/MetaData/DA_CT2_BombHouse", "DA_CT2_BombHouse")):
        p_imp = mt.add_import("/Script/CoreUObject", "Package", 0, pkg)
        o_imp = mt.add_import("/Game/MenuSystemPro/Blueprints/UI/Types/PDA_LevelMetaData", "PDA_LevelMetaData_C", p_imp, name)
        add_dep(mt, 0, o_imp, "create_before_ser")
        rows += struct.pack("<ii", mt.add_name(key), 0) + bytes.fromhex("00 05") + b"\x01" + struct.pack("<i", o_imp)
    mt_uexp = mt_src[:10] + struct.pack("<i", 2) + rows + b"\xc1\x83\x2a\x9e"
    mt.exports[0]["serial_size"] = len(mt_uexp) - 4
    mt_uasset = mt.serialize(); verify(mt_uasset, mt_uexp, "maps table")
    files["UI/Menus/Play/Cards/Data/DT_UI_CommunityTestMaps.uasset"] = mt_uasset
    files["UI/Menus/Play/Cards/Data/DT_UI_CommunityTestMaps.uexp"] = mt_uexp
    # the UI table from v7 already points the community card at DT_UI_CommunityTestMaps (same package path) — keep it
    ui_rel = "UI/Menus/Play/Cards/Data/DT_UI_CustomGameModes"
    assert b"DT_UI_CommunityTestMaps" in files[ui_rel + ".uasset"]

    pak = os.path.join(outdir, "DTTest_v9_P.pak")
    paklib.write_pak(pak, MOUNT, files, seed=0)
    r = paklib.PakReader(pak); assert set(r.files) == set(files) and r.mount_point == MOUNT
    for rel, data in files.items():
        en, blocks = r.raw_blocks(rel); assert blocks[0] == data and paklib.fnv64_path(rel, r.seed) in r.phi
    print("OK", pak, os.path.getsize(pak), "B sha256", hashlib.sha256(open(pak, "rb").read()).hexdigest(), "mount", MOUNT)
    for rel in sorted(files): print("  ", rel, len(files[rel]), "B")

if __name__ == "__main__":
    main(*sys.argv[1:7])
