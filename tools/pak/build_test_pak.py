#!/usr/bin/env python3
"""Rebuild the Test #1 patch pak (ModTest_P.pak) from the game's own English string table.

Usage:
    python build_test_pak.py <path to pakchunk0-Windows.pak> <path to oozcli binary> <output dir>

Steps performed:
  1. Read pakchunk0-Windows.pak (unencrypted, Oodle-compressed) and pull the raw compressed block for
     Bodycam/Content/Localization/Game/en/Game.locres.
  2. Decompress it with oozcli (built from tools/pak/oozcli with `cargo build --release`).
  3. Parse the .locres, rename three main-menu labels with a "[MOD TEST]" suffix, re-serialize byte-exact
     except for the changed strings.
  4. Write an uncompressed pak v11 named ModTest_P.pak with mount point ../../../ containing only that file.
  5. Re-read the pak and verify hashes, path-hash index, and payload.

Place the result in <Bodycam>\\Bodycam\\Content\\Paks\\~mods\\ (ask Sam first — see instructions.md §4.1).
"""
import hashlib
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import locres  # noqa: E402
import paklib  # noqa: E402

LOCRES_PATH = "Bodycam/Content/Localization/Game/en/Game.locres"
CHANGES = {  # namespace, key -> new text
    ("ST_UI_MainMenu", "UI.MainMenu.Home"): "Home [MOD TEST]",
    ("ST_UI_MainMenu", "UI.MainMenu.Playlist"): "Playlist [MOD TEST]",
    ("ST_UI_MainMenu", "UI.MainMenu.Exit"): "Exit Game [MOD TEST]",
}


def main(pak0, oozcli, outdir):
    os.makedirs(outdir, exist_ok=True)
    r = paklib.PakReader(pak0)
    entry, blocks = r.raw_blocks(LOCRES_PATH)
    assert entry["comp_idx"] == 1 and entry["nblocks"] == 1, entry
    comp = os.path.join(outdir, "en_Game.locres.oodle")
    plain = os.path.join(outdir, "en_Game.locres")
    open(comp, "wb").write(blocks[0])
    subprocess.check_call([oozcli, comp, str(entry["usize"]), plain])

    original = open(plain, "rb").read()
    l = locres.LocRes.load(plain)
    assert l.dumps() == original, "locres round-trip is not byte-exact; refusing to continue"
    for ns, key, _src, idx, _s in list(l.entries()):
        if (ns, key) in CHANGES:
            l.strings[idx][0] = CHANGES[(ns, key)]
    modified = l.dumps()
    open(os.path.join(outdir, "en_Game_mod.locres"), "wb").write(modified)

    out = os.path.join(outdir, "ModTest_P.pak")
    paklib.write_pak(out, "../../../", {LOCRES_PATH: modified}, seed=0)

    # verify
    v = paklib.PakReader(out)
    assert v.version == 11 and v.mount_point == "../../../" and v.files == {LOCRES_PATH: 0}
    e2, b2 = v.raw_blocks(LOCRES_PATH)
    assert b2[0] == modified and e2["comp_idx"] == 0 and e2["encrypted"] == 0
    assert paklib.fnv64_path(LOCRES_PATH, v.seed) in v.phi
    idx = v.data[v.index_offset:v.index_offset + v.index_size]
    assert v.index_hash == hashlib.sha1(idx).digest()
    print("OK", out, os.path.getsize(out), "bytes, sha256", hashlib.sha256(open(out, "rb").read()).hexdigest())


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(2)
    main(*sys.argv[1:])
