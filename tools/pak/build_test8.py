#!/usr/bin/env python3
"""Test #8 (diagnostic) builder — bisect the v7 'stuck on waiting for connection' result.

v8 = v7 + an OVERRIDE of the stock level GM_Maps/DeathMatch/DM_BombHouse (same package name, exact-path override — a
proven mechanism) whose World Settings name GM_CommunityTest_C. So:
  * hosting stock 'Deathmatch [DT TEST]' -> BombHouse travels to the stock short name DM_BombHouse (lookup proven),
    loads OUR level -> our class -> DA_CommunityTest: a 5-kill end proves the World Settings edit + the class clone.
  * hosting COMMUNITY TEST MODE -> BombHouse still travels to the NEW short name DM_CT_BombHouse: if that alone
    stays stuck, short-name lookup of a new level is the culprit.
Usage: python3 build_test8.py <extm> <t7b_dir> <outdir>
"""
import hashlib, os, struct, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paklib
from pkgedit import CookedPackage
from build_test7 import add_dep, verify

GM_PKG, GM_CLS = "/Game/GM/Gamemode/GM_CommunityTest", "GM_CommunityTest_C"

def patch_world_settings(extm, gm_pkg, gm_cls):
    lv = CookedPackage.load(os.path.join(extm, "GM_Maps__DeathMatch__DM_BombHouse.umap"), lenient=True)
    lv_uexp = bytearray(open(os.path.join(extm, "GM_Maps__DeathMatch__DM_BombHouse.uexp"), "rb").read())
    assert lv.package_name == "/Game/GM_Maps/DeathMatch/DM_BombHouse"
    pkg_imp = lv.add_import("/Script/CoreUObject", "Package", 0, gm_pkg)
    cls_imp = lv.add_import("/Script/Engine", "BlueprintGeneratedClass", pkg_imp, gm_cls)
    lv.add_import(gm_pkg, gm_cls, pkg_imp, "Default__" + gm_cls)
    ws_i = len(lv.exports) - 1; ws = lv.exports[ws_i]; assert lv.names[ws["name_idx"]][0] == "WorldSettings"
    off = ws["serial_offset"] - lv.total_header_size; old = bytes(lv_uexp[off:off + ws["serial_size"]])
    assert old[:8] == bytes.fromhex("16 02 23 03 0c 00 00 00"), old[:8].hex()
    new = bytes.fromhex("16 02 0a 02 18 03") + old[4:8] + struct.pack("<i", cls_imp) + old[8:]
    lv_uexp[off:off + ws["serial_size"]] = new; ws["serial_size"] = len(new); lv_uexp = bytes(lv_uexp)
    add_dep(lv, ws_i, cls_imp, "create_before_ser")
    lv_uasset = lv.serialize(); verify(lv_uasset, lv_uexp, "stock-path level override")
    return lv_uasset, lv_uexp

def main(extm, t7b, outdir):
    os.makedirs(outdir, exist_ok=True)
    r7 = paklib.PakReader(os.path.join(t7b, "DTTest_v7_P.pak"))
    files = {rel: r7.raw_blocks(rel)[1][0] for rel in r7.files}
    ua, ue = patch_world_settings(extm, GM_PKG, GM_CLS)
    files["Bodycam/Content/GM_Maps/DeathMatch/DM_BombHouse.umap"] = ua
    files["Bodycam/Content/GM_Maps/DeathMatch/DM_BombHouse.uexp"] = ue
    pak = os.path.join(outdir, "DTTest_v8_P.pak")
    paklib.write_pak(pak, "../../../", files, seed=0)
    r = paklib.PakReader(pak); assert set(r.files) == set(files)
    for rel, data in files.items():
        en, blocks = r.raw_blocks(rel); assert blocks[0] == data and paklib.fnv64_path(rel, r.seed) in r.phi
    print("OK", pak, os.path.getsize(pak), "B sha256", hashlib.sha256(open(pak, "rb").read()).hexdigest())
    for rel in sorted(files): print("  ", rel, len(files[rel]), "B")

if __name__ == "__main__":
    main(*sys.argv[1:4])
