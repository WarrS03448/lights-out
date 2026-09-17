#!/usr/bin/env python3
"""Scan staged paks: print mount point, entry count, and any files matching a substring.
Usage: python3 scanpaks.py <needle> [pak ...]   (defaults to all staged paks not yet scanned)
Writes a per-pak file list to pakindex/<pakname>.txt so the pak copy can be deleted afterwards."""
import glob, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paklib

needle = sys.argv[1].lower()
paks = sys.argv[2:] or sorted(glob.glob("*.pak"))
os.makedirs("pakindex", exist_ok=True)
for p in paks:
    name = os.path.basename(p)
    out = f"pakindex/{name}.txt"
    try:
        r = paklib.PakReader(p)
    except Exception as ex:
        print(f"{name}: ERROR {ex}")
        continue
    with open(out, "w") as f:
        for rel in sorted(r.files):
            f.write(rel + "\n")
    hits = [rel for rel in r.files if needle in rel.lower()]
    print(f"{name}: mount={r.mount_point} files={len(r.files)} seed={hex(r.seed)}" + (f"  HITS={hits}" if hits else ""))
