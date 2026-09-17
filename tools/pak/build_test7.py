#!/usr/bin/env python3
"""Test #7 builder — a community mode that travels to ITS OWN level variant whose World Settings pick the GameMode class.

Proven host flow (BodycamGI.TravelToMap, decoded 2026-09-14):  servertravel <Prefix(GI.Gamemode)><DA_<Map>.LevelName>
So a community card with GameMode=DeathMatch and its own maps table -> its own map metadata (LevelName "CT_BombHouse")
-> "servertravel DM_CT_BombHouse" -> our cloned level package -> WorldSettings.DefaultGameMode = <class we choose>.
Engine order: WorldSettings.DefaultGameMode wins over the map-prefix list (only ?game= beats it).

Builds on the Test #4 (v5) files and ADDS:
  1. /Game/GM_Maps/Community/DM_CT_BombHouse   = clone of DM_BombHouse + WorldSettings.DefaultGameMode = <GM class> (Wingman pattern)
  2. /Game/UI/MetaData/DA_CT_BombHouse           = clone of DA_BombHouse with LevelName = CT_BombHouse
  3. /Game/UI/Menus/Play/Cards/Data/DT_UI_CommunityTestMaps = one-row maps table (BombHouse -> DA_CT_BombHouse)
  4. DT_UI_CustomGameModes: the DeathMatch_99 card's MapTable -> DT_UI_CommunityTestMaps
Test #7a uses GM_TeamDeathMatch_C as the level's GameMode (an existing class): hosting the community card must then
play as Team Deathmatch (teams, 75 kills) on BombHouse -> proves level lookup from a ~mods pak + World Settings override.
Usage: python3 build_test7.py <extm_dir> <ext24_dir> <ext_ui_dir> <t3b_dir> <t4_dir> <outdir> [gm_package] [gm_class]
"""
import hashlib, os, struct, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paklib, dtdump
from pkgedit import CookedPackage, case_preserving_hash, non_case_preserving_hash

def rename(pk, new_pkg, old_name, new_name):
    old_pkg = pk.package_name; pk.package_name = new_pkg
    for n in pk.names:
        if n[0] == old_pkg: n[0] = new_pkg
        elif n[0] == old_name: n[0] = new_name
        else: continue
        n[1], n[2] = non_case_preserving_hash(n[0]), case_preserving_hash(n[0])
    pk.guid = hashlib.sha256(new_pkg.encode()).digest()[:16]

def add_dep(pk, export_i, imp, group="create_before_ser"):
    """Insert an import into one export's preload-dependency group; shifts later exports' first_dep."""
    e = pk.exports[export_i]
    order = ["ser_before_ser", "create_before_ser", "ser_before_create", "create_before_create"]
    at = e["first_dep"] + sum(e[g] for g in order[:order.index(group) + 1])
    pk.preload.insert(at, imp); e[group] += 1
    for j, o in enumerate(pk.exports):
        if j != export_i and o["first_dep"] >= at and o is not e: o["first_dep"] += 1

def verify(uasset, uexp, label):
    open("/tmp/_v.uasset", "wb").write(uasset)
    v = CookedPackage.load("/tmp/_v.uasset", lenient=True)
    assert v.serialize() == uasset, label
    assert v.exports[0]["serial_offset"] == len(uasset) or v.exports[0]["serial_offset"] >= len(uasset), label
    end = max(e["serial_offset"] + e["serial_size"] for e in v.exports)
    assert end == len(uasset) + len(uexp) - 4 == v.bulk_start, (label, end, len(uasset), len(uexp), v.bulk_start)
    assert uexp[-4:] == b"\xc1\x83\x2a\x9e", label
    return v

def main(extm, ext24, extui, t3b, t4, outdir, gm_pkg="/Game/GM/Gamemode/GM_TeamDeathMatch", gm_cls="GM_TeamDeathMatch_C"):
    os.makedirs(outdir, exist_ok=True); files = {}

    # ---- 1. level clone: DM_BombHouse -> DM_CT_BombHouse with WorldSettings.DefaultGameMode ----
    lv = CookedPackage.load(os.path.join(extm, "GM_Maps__DeathMatch__DM_BombHouse.umap"), lenient=True)
    lv_uexp = bytearray(open(os.path.join(extm, "GM_Maps__DeathMatch__DM_BombHouse.uexp"), "rb").read())
    assert lv.package_name == "/Game/GM_Maps/DeathMatch/DM_BombHouse"
    rename(lv, "/Game/GM_Maps/Community/DM_CT_BombHouse", "DM_BombHouse", "DM_CT_BombHouse")
    pkg_imp = lv.add_import("/Script/CoreUObject", "Package", 0, gm_pkg)
    cls_imp = lv.add_import("/Script/Engine", "BlueprintGeneratedClass", pkg_imp, gm_cls)
    cdo_imp = lv.add_import(gm_pkg, gm_cls, pkg_imp, "Default__" + gm_cls)
    ws_i = next(i for i, e in enumerate(lv.exports) if lv.names[e["name_idx"]][0] == "WorldSettings")
    ws = lv.exports[ws_i]; assert ws_i == len(lv.exports) - 1, "WorldSettings expected to be the last export"
    off = ws["serial_offset"] - lv.total_header_size; old = bytes(lv_uexp[off:off + ws["serial_size"]])
    assert old[:8] == bytes.fromhex("16 02 23 03 0c 00 00 00"), old[:8].hex()   # skip22,1 value | skip35,last,1 value | NavConfig ref
    new = bytes.fromhex("16 02 0a 02 18 03") + old[4:8] + struct.pack("<i", cls_imp) + old[8:]   # skip22 | skip10 (=#33 DefaultGameMode) | skip24,last
    assert len(new) == len(old) + 6
    lv_uexp[off:off + ws["serial_size"]] = new; ws["serial_size"] = len(new); lv_uexp = bytes(lv_uexp)
    add_dep(lv, ws_i, cls_imp, "create_before_ser")   # Wingman pattern: class import in WorldSettings' CreateBeforeSerialization group
    lv_uasset = lv.serialize()
    v = verify(lv_uasset, lv_uexp, "level")
    assert v.names[v.exports[-1]["name_idx"]][0] == "WorldSettings" and v.exports[-1]["create_before_ser"] == 2
    files["Bodycam/Content/GM_Maps/Community/DM_CT_BombHouse.umap"] = lv_uasset
    files["Bodycam/Content/GM_Maps/Community/DM_CT_BombHouse.uexp"] = lv_uexp
    print(f"level: header {len(lv_uasset)} B (+{len(lv_uasset) - len(lv.src)}), uexp {len(lv_uexp)} B, WorldSettings -> import {cls_imp} {gm_cls}")

    # ---- 2. DA_CT_BombHouse (LevelName = CT_BombHouse) ----
    da = CookedPackage.load(os.path.join(ext24, "UI__MetaData__DA_BombHouse.uasset"))
    da_uexp = bytearray(open(os.path.join(ext24, "UI__MetaData__DA_BombHouse.uexp"), "rb").read())
    rename(da, "/Game/UI/MetaData/DA_CT_BombHouse", "DA_BombHouse", "DA_CT_BombHouse")
    assert da.names[struct.unpack_from("<i", da_uexp, 4)[0]][0] == "BombHouse" and da_uexp[:4] == bytes.fromhex("00 08 02 05")
    struct.pack_into("<ii", da_uexp, 4, da.add_name("CT_BombHouse"), 0); da_uexp = bytes(da_uexp)
    da_uasset = da.serialize(); verify(da_uasset, da_uexp, "DA")
    files["Bodycam/Content/UI/MetaData/DA_CT_BombHouse.uasset"] = da_uasset
    files["Bodycam/Content/UI/MetaData/DA_CT_BombHouse.uexp"] = da_uexp

    # ---- 3. DT_UI_CommunityTestMaps (one row: BombHouse -> DA_CT_BombHouse) ----
    mt = CookedPackage.load(os.path.join(extui, "UI__Menus__Play__Cards__Data__DT_UI_DeathmatchMaps.uasset"))
    mt_src = open(os.path.join(extui, "UI__Menus__Play__Cards__Data__DT_UI_DeathmatchMaps.uexp"), "rb").read()
    rename(mt, "/Game/UI/Menus/Play/Cards/Data/DT_UI_CommunityTestMaps", "DT_UI_DeathmatchMaps", "DT_UI_CommunityTestMaps")
    p_imp = mt.add_import("/Script/CoreUObject", "Package", 0, "/Game/UI/MetaData/DA_CT_BombHouse")
    o_imp = mt.add_import("/Game/MenuSystemPro/Blueprints/UI/Types/PDA_LevelMetaData", "PDA_LevelMetaData_C", p_imp, "DA_CT_BombHouse")
    add_dep(mt, 0, o_imp, "create_before_ser")
    rs = struct.unpack_from("<i", mt_src, 2)[0]; assert mt.names[mt.imports[-rs-1][5]][0] == "BP_MapDefinition"
    assert struct.unpack_from("<i", mt_src, 10)[0] == 16   # row count
    row = struct.pack("<ii", mt.name_index("BombHouse"), 0) + bytes.fromhex("00 05") + b"\x01" + struct.pack("<i", o_imp)   # IsActive=1, Metadata
    mt_uexp = mt_src[:10] + struct.pack("<i", 1) + row + b"\xc1\x83\x2a\x9e"
    mt.exports[0]["serial_size"] = len(mt_uexp) - 4
    mt_uasset = mt.serialize(); verify(mt_uasset, mt_uexp, "maps table")
    files["Bodycam/Content/UI/Menus/Play/Cards/Data/DT_UI_CommunityTestMaps.uasset"] = mt_uasset
    files["Bodycam/Content/UI/Menus/Play/Cards/Data/DT_UI_CommunityTestMaps.uexp"] = mt_uexp

    # ---- 4. DT_UI_CustomGameModes: community card's MapTable -> the new table ----
    ui = CookedPackage.load(os.path.join(t3b, "ui.uasset"))
    ui_uexp = bytearray(open(os.path.join(t3b, "ui.uexp"), "rb").read())
    sa = os.path.join(extui, "UI__Menus__Play__Cards__Data__Struct_GameModeDefinition.uasset"); se = sa[:-7] + ".uexp"
    _, _, _, rows, _ = dtdump.parse_table(os.path.join(t3b, "ui.uasset"), os.path.join(t3b, "ui.uexp"), sa, se)
    name, start, end, row, offs = rows[-1]; assert name == "DeathMatch_99" and row["GameMode"] == 2
    tp_imp = ui.add_import("/Script/CoreUObject", "Package", 0, "/Game/UI/Menus/Play/Cards/Data/DT_UI_CommunityTestMaps")
    to_imp = ui.add_import("/Script/Engine", "DataTable", tp_imp, "DT_UI_CommunityTestMaps")
    add_dep(ui, 0, to_imp, "create_before_ser")
    at = start + offs["MapTable"][0]
    assert ui.names[ui.imports[-struct.unpack_from("<i", ui_uexp, at)[0]-1][5]][0] == "DT_UI_DeathmatchMaps"
    struct.pack_into("<i", ui_uexp, at, to_imp); ui_uexp = bytes(ui_uexp)
    ui_uasset = ui.serialize(); verify(ui_uasset, ui_uexp, "UI table")
    open(os.path.join(outdir, "ui.uasset"), "wb").write(ui_uasset); open(os.path.join(outdir, "ui.uexp"), "wb").write(ui_uexp)
    _, _, _, rows2, _ = dtdump.parse_table(os.path.join(outdir, "ui.uasset"), os.path.join(outdir, "ui.uexp"), sa, se)
    assert rows2[-1][3]["MapTable"].endswith("DataTable DT_UI_CommunityTestMaps") and rows2[0][3]["MapTable"].endswith("DT_UI_DeathmatchMaps"), rows2[-1][3]["MapTable"]
    files["Bodycam/Content/UI/Menus/Play/Cards/Data/DT_UI_CustomGameModes.uasset"] = ui_uasset
    files["Bodycam/Content/UI/Menus/Play/Cards/Data/DT_UI_CustomGameModes.uexp"] = ui_uexp

    # ---- 5. unchanged v5 files: DT_GameModeData, DT_GamemodeInfo, DA_CommunityTest ----
    r5 = paklib.PakReader(os.path.join(t4, "DTTest_v5_P.pak"))
    for rel in r5.files:
        if rel not in files:
            e, blocks = r5.raw_blocks(rel); files[rel] = blocks[0]
    pak = os.path.join(outdir, "DTTest_v6_P.pak")
    paklib.write_pak(pak, "../../../", files, seed=0)
    r = paklib.PakReader(pak); assert set(r.files) == set(files)
    for rel, data in files.items():
        en, blocks = r.raw_blocks(rel); assert blocks[0] == data and en["encrypted"] == 0 and paklib.fnv64_path(rel, r.seed) in r.phi
    print("OK", pak, os.path.getsize(pak), "B sha256", hashlib.sha256(open(pak, "rb").read()).hexdigest())
    for rel in sorted(files): print("  ", rel, len(files[rel]), "B")

if __name__ == "__main__":
    main(*sys.argv[1:9])
