#!/usr/bin/env python3
"""Test #4 builder: a self-contained community mode with its OWN config asset.
- Clones DA_GameModeDeathmatch -> new package /Game/GM/DATA/DataAsset/DA_CommunityTest (limit 40 -> 5)
- DT_GameModeData: adds names + 2 imports (package + object) + a CreateBeforeSerialization preload dep,
  and points the DeathMatch_99 row's GameModeConfig at the new asset
- DT_UI_CustomGameModes / DT_GamemodeInfo: same as Test #3b (cloned rows under DeathMatch_99)
Stock Deathmatch keeps DA_GameModeDeathmatch (40). Writes DTTest_v5_P.pak.
Usage: python3 build_test4.py <ext_ui_dir> <ext26_dir> <t3b_outdir> <outdir>
"""
import hashlib, os, struct, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paklib, dtdump
from pkgedit import CookedPackage, case_preserving_hash, non_case_preserving_hash

NEW_PKG = "/Game/GM/DATA/DataAsset/DA_CommunityTest"
NEW_NAME = "DA_CommunityTest"
NEW_LIMIT = 5

def main(ui_dir, gm_dir, t3b_dir, outdir):
    os.makedirs(outdir, exist_ok=True); files = {}

    # ---- 1. clone the DA package ----
    da = CookedPackage.load(os.path.join(gm_dir, "GM__DATA__DataAsset__DA_GameModeDeathmatch.uasset"))
    old_pkg, old_name = da.package_name, da.names[da.exports[0]["name_idx"]][0]
    assert old_pkg == "/Game/GM/DATA/DataAsset/DA_GameModeDeathmatch" and old_name == "DA_GameModeDeathmatch"
    da.package_name = NEW_PKG
    for n in da.names:
        if n[0] == old_pkg: n[0] = NEW_PKG
        elif n[0] == old_name: n[0] = NEW_NAME
        else: continue
        n[1], n[2] = non_case_preserving_hash(n[0]), case_preserving_hash(n[0])
    da.guid = hashlib.sha256(NEW_PKG.encode()).digest()[:16]
    da_uasset = da.serialize()
    da_uexp = bytearray(open(os.path.join(gm_dir, "GM__DATA__DataAsset__DA_GameModeDeathmatch.uexp"), "rb").read())
    assert struct.unpack_from("<i", da_uexp, 10)[0] == 40; struct.pack_into("<i", da_uexp, 10, NEW_LIMIT); da_uexp = bytes(da_uexp)
    assert da.exports[0]["serial_size"] == len(da_uexp) - 4
    files["Bodycam/Content/GM/DATA/DataAsset/DA_CommunityTest.uasset"] = da_uasset
    files["Bodycam/Content/GM/DATA/DataAsset/DA_CommunityTest.uexp"] = da_uexp
    chk = CookedPackage.load_bytes = None
    open(os.path.join(outdir, "DA_CommunityTest.uasset"), "wb").write(da_uasset)
    v = CookedPackage.load(os.path.join(outdir, "DA_CommunityTest.uasset"))
    assert v.package_name == NEW_PKG and v.names[v.exports[0]["name_idx"]][0] == NEW_NAME and v.serialize() == da_uasset

    # ---- 2. DT_GameModeData: start from the Test #3b version (has the DeathMatch_99 row) ----
    gmd = CookedPackage.load(os.path.join(t3b_dir, "gmd.uasset"))
    gmd_uexp = bytearray(open(os.path.join(t3b_dir, "gmd.uexp"), "rb").read())
    pkg_imp = gmd.add_import("/Script/CoreUObject", "Package", 0, NEW_PKG)
    obj_imp = gmd.add_import("/Script/Bodycam", "GameModeConfigDataAsset", pkg_imp, NEW_NAME)
    e = gmd.exports[0]
    insert_at = e["first_dep"] + e["ser_before_ser"] + e["create_before_ser"]   # end of the CreateBeforeSerialization group
    gmd.preload.insert(insert_at, obj_imp); e["create_before_ser"] += 1
    # retarget the cloned row: it currently points at DA_GameModeDeathmatch (import index of that object)
    sa = os.path.join(gm_dir, "GM__DATA__Structure__STR_GameModeData.uasset"); se = os.path.join(gm_dir, "GM__DATA__Structure__STR_GameModeData.uexp")
    _, _, _, rows, _ = dtdump.parse_table(os.path.join(t3b_dir, "gmd.uasset"), os.path.join(t3b_dir, "gmd.uexp"), sa, se)
    name, start, end, row, offs = rows[-1]; assert name == "DeathMatch_99"
    cfg_at = start + offs["GameModeConfig"][0]
    old_ref = struct.unpack_from("<i", gmd_uexp, cfg_at)[0]; assert gmd.names[gmd.imports[-old_ref-1][5]][0] == "DA_GameModeDeathmatch"
    struct.pack_into("<i", gmd_uexp, cfg_at, obj_imp); gmd_uexp = bytes(gmd_uexp)
    gmd_uasset = gmd.serialize()
    files["Bodycam/Content/GM/DATA/DT/DT_GameModeData.uasset"] = gmd_uasset
    files["Bodycam/Content/GM/DATA/DT/DT_GameModeData.uexp"] = gmd_uexp
    open(os.path.join(outdir, "gmd.uasset"), "wb").write(gmd_uasset); open(os.path.join(outdir, "gmd.uexp"), "wb").write(gmd_uexp)
    v = CookedPackage.load(os.path.join(outdir, "gmd.uasset")); assert v.serialize() == gmd_uasset
    assert v.exports[0]["serial_offset"] == len(gmd_uasset) and v.bulk_start == len(gmd_uasset) + len(gmd_uexp) - 4
    _, _, _, rows2, _ = dtdump.parse_table(os.path.join(outdir, "gmd.uasset"), os.path.join(outdir, "gmd.uexp"), sa, se)
    assert rows2[-1][3]["GameModeConfig"].endswith("GameModeConfigDataAsset DA_CommunityTest"), rows2[-1][3]
    assert rows2[0][3]["GameModeConfig"].endswith("DA_GameModeDeathmatch")
    print("DT_GameModeData rows:"); [print("  ", r[0], r[3]) for r in rows2]
    print("preload deps:", v.preload, "export deps:", {k: v.exports[0][k] for k in ("first_dep","ser_before_ser","create_before_ser","ser_before_create","create_before_create")})

    # ---- 3. UI table + GamemodeInfo unchanged from Test #3b ----
    for rel, fn in (("Bodycam/Content/UI/Menus/Play/Cards/Data/DT_UI_CustomGameModes", "ui"), ("Bodycam/Content/GM/DATA/DT/DT_GamemodeInfo", "gmi")):
        for ext in (".uasset", ".uexp"): files[rel + ext] = open(os.path.join(t3b_dir, fn + ext), "rb").read()

    pak = os.path.join(outdir, "DTTest_v5_P.pak")
    paklib.write_pak(pak, "../../../", files, seed=0)
    r = paklib.PakReader(pak); assert set(r.files) == set(files)
    for rel, data in files.items():
        en, blocks = r.raw_blocks(rel); assert blocks[0] == data and en["encrypted"] == 0 and paklib.fnv64_path(rel, r.seed) in r.phi
    print("OK", pak, os.path.getsize(pak), "B sha256", hashlib.sha256(open(pak, "rb").read()).hexdigest())
    for rel in sorted(files): print("  ", rel, len(files[rel]), "B")

if __name__ == "__main__":
    main(*sys.argv[1:5])
