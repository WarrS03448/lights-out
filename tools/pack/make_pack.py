#!/usr/bin/env python3
"""make_pack.py — package ONE community gamemode for the hub's catalogue.

Usage: python3 make_pack.py --manifest gamemodes/ctf/manifest.json --cooked mirror/cooked --version 1.0.0 --out packs/ [--base-url https://host/packs]

Writes  <out>/<ID>-<version>/            the pack folder the hub consumes (manifest.json + cooked/ + cooked/blueprints_summary.txt)
        <out>/<ID>-<version>.zip         the same, zipped (what the catalogue links to)
        <out>/<ID>-<version>.json        the catalogue entry (id, title, version, pack_url, sha256, size, description)

Only OUR cooked packages go in: GM_<ID>, DA_<ID> and the mode's own folder (BASES[base]["extra_dirs"]). The game's assets (levels,
tables, the registry) are never packaged — the hub clones them from the player's own paks at install time. The pack also carries
the Blueprint run's RESULT: OK verdict, which build_from_packs() re-checks before it builds anything.
"""
import argparse, hashlib, json, os, shutil, sys, zipfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pak"))
from build_gamemode import BASES, Builder

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True); ap.add_argument("--cooked", required=True); ap.add_argument("--version", required=True)
    ap.add_argument("--out", required=True); ap.add_argument("--base-url", default="https://REPLACE-ME/packs")
    a = ap.parse_args()
    # manifests are UTF-8 (7-language titles/rulesets) — never the Windows code page
    m = json.load(open(a.manifest, encoding="utf-8")); mid = m["id"]; base = BASES[m.get("base", "DeathMatch")]
    if base.get("kind") != "cooked": sys.exit(f"{mid}: only cooked bases are packaged (base {m.get('base')})")
    Builder.check_cook_verdict(a.cooked)                       # refuse a cook that did not end with RESULT: OK
    name = f"{mid}-{a.version}"; pack = os.path.join(a.out, name)
    if os.path.isdir(pack): shutil.rmtree(pack)
    cooked_src = os.path.join(a.cooked, "Bodycam", "Content"); cooked_dst = os.path.join(pack, "cooked", "Bodycam", "Content")
    wanted = []
    for pkg in (base["class_pkg"], base["config_pkg"]):
        rel = pkg[len("/Game/"):]
        for ext in (".uasset", ".uexp"): wanted.append(rel + ext)
    for d in base.get("extra_dirs", []):
        root = os.path.join(cooked_src, *d.split("/"))
        for dp, dn, fn in os.walk(root):
            for f in fn: wanted.append(os.path.relpath(os.path.join(dp, f), cooked_src).replace(os.sep, "/"))
    for rel in wanted:
        src = os.path.join(cooked_src, *rel.split("/"))
        if not os.path.exists(src): sys.exit(f"missing cooked file: {src}")
        dst = os.path.join(cooked_dst, *rel.split("/")); os.makedirs(os.path.dirname(dst), exist_ok=True); shutil.copy2(src, dst)
    # The full local summary has already been validated above. Ship the verdict without
    # developer paths or build-machine details; retain the complete original locally.
    with open(os.path.join(pack, "cooked", "blueprints_summary.txt"), "w", encoding="utf-8") as verdict:
        verdict.write("Community gamemode Blueprint build\nRESULT: OK\n")
    # ---- localization: gamemodes/<id>/loc.json (7 cultures) travels with the pack; the hub merges it into the player's Game.locres
    loc_src = os.path.join(os.path.dirname(os.path.abspath(a.manifest)), "loc.json")
    if os.path.exists(loc_src):
        loc = json.load(open(loc_src, encoding="utf-8"))
        from build_gamemode import LOC_NAMESPACE, CULTURES
        if loc.get("namespace") != LOC_NAMESPACE: sys.exit(f"loc.json: namespace must be {LOC_NAMESPACE!r}")
        entries = loc.get("entries") or {}
        problems = []
        for key, texts in entries.items():
            if not key.startswith(mid + "."): problems.append(f"key {key!r} must start with {mid + '.'!r}")
            if not texts.get("en"): problems.append(f"{key}: no 'en' source text")
            for cul in CULTURES:
                if not texts.get(cul): problems.append(f"{key}: missing {cul} (English will be shown)")
            for cul in texts:
                if cul not in CULTURES: problems.append(f"{key}: {cul!r} is not a culture the game ships {CULTURES}")
        # the 'en' text IS the source string baked into the row / the Blueprint: any drift means no translation shows
        if entries.get(f"{mid}.Title", {}).get("en") != m.get("title"): problems.append(f"{mid}.Title 'en' != manifest title {m.get('title')!r}")
        if entries.get(f"{mid}.Description", {}).get("en") != m.get("description", ""): problems.append(f"{mid}.Description 'en' != manifest description")
        cooked_blob = b"".join(open(os.path.join(cooked_src, *rel.split("/")), "rb").read() for rel in wanted)
        for key, texts in entries.items():
            if key in (f"{mid}.Title", f"{mid}.Description"): continue          # consumed by the mode row, not by a Blueprint
            if key.encode("utf-8") not in cooked_blob:
                problems.append(f"{key}: not referenced by any cooked Blueprint of this pack (FindTextInLocalizationTable key missing?)")
            if texts["en"].encode("utf-8") not in cooked_blob and texts["en"].encode("utf-16-le") not in cooked_blob:
                problems.append(f"{key}: its 'en' source text {texts['en']!r} is not in the cooked Blueprints (source string drift)")
        hard = [x for x in problems if "missing" not in x or "not referenced" in x]
        for x in problems: print(("ERROR " if x in hard else "warning ") + x)
        if hard: sys.exit("loc.json problems - not packing")
        shutil.copy2(loc_src, os.path.join(pack, "loc.json"))
        print(f"loc.json: {len(entries)} keys x {len(CULTURES)} cultures")
    else:
        print("note: no loc.json next to the manifest - the in-game text stays English only")
    m = dict(m); m["version"] = a.version
    json.dump(m, open(os.path.join(pack, "manifest.json"), "w", encoding="utf-8"), indent=2)
    zpath = os.path.join(a.out, name + ".zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        # reproducible: fixed timestamps + sorted order, so identical content gives an identical sha256
        for dp, dn, fn in sorted(os.walk(pack)):
            for f in sorted(fn):
                p = os.path.join(dp, f)
                zi = zipfile.ZipInfo(os.path.relpath(p, pack).replace(os.sep, "/"), date_time=(2020, 1, 1, 0, 0, 0))
                zi.compress_type = zipfile.ZIP_DEFLATED; zi.external_attr = 0o644 << 16
                with open(p, "rb") as fh: z.writestr(zi, fh.read())
    data = open(zpath, "rb").read()
    # display_name = the hub's list (English), display_names = the same per language (the hub picks its language's),
    # title = the in-game mode card (localized separately through the pack's loc files)
    entry = {"id": mid, "title": m.get("display_name", m.get("title", mid)), "titles": dict(m.get("display_names") or {}),
             "version": a.version, "description": m.get("description", ""),
             "pack_url": f"{a.base_url.rstrip('/')}/{name}.zip", "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
             "files": sorted(wanted), "localized": os.path.exists(os.path.join(pack, "loc.json")),
             # rulesets = the hub's "Ruleset" pop-up (notepad icon on the gamemode row), per language; plain text with line breaks
             "rulesets": dict(m.get("rulesets") or {}), "rules": dict(m.get("rules") or {})}
    if entry["rulesets"] and not entry["rulesets"].get("en"): sys.exit("manifest rulesets: an 'en' text is required")
    json.dump(entry, open(os.path.join(a.out, name + ".json"), "w", encoding="utf-8"), indent=2)
    print(f"pack {name}: {len(wanted)} cooked file(s), zip {len(data)} B, sha256 {entry['sha256']}")
    for w in sorted(wanted): print("  ", w)

if __name__ == "__main__":
    main()
