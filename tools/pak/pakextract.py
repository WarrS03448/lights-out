#!/usr/bin/env python3
"""Extract files from a (small, fully copied) pak: python3 pakextract.py <pak> <outdir> <relpath> [relpath...]
Writes <outdir>/<relpath with / -> __>. Handles uncompressed and Oodle (multi-block) entries via oozcli."""
import sys, os, subprocess
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from paklib import PakReader
OOZ = os.environ.get("OOZCLI", "/tmp/oozcli/target/release/oozcli")
def extract(r, rel, outdir):
    e, blocks = r.raw_blocks(rel)
    out = os.path.join(outdir, rel.replace("/", "__"))
    if e["comp_idx"] == 0:
        data = blocks[0]
    else:
        data = b""
        for blk in blocks:
            want = min(e["block_size"], e["usize"] - len(data)) if e["block_size"] else e["usize"]
            open(out + ".blk", "wb").write(blk)
            subprocess.check_call([OOZ, out + ".blk", str(want), out + ".dec"])
            data += open(out + ".dec", "rb").read()
        os.remove(out + ".blk"); os.remove(out + ".dec")
    assert len(data) == e["usize"], (rel, len(data), e["usize"])
    open(out, "wb").write(data); return out
if __name__ == "__main__":
    r = PakReader(sys.argv[1]); outdir = sys.argv[2]; os.makedirs(outdir, exist_ok=True)
    for rel in sys.argv[3:]:
        hits = [f for f in r.files if f == rel or f.startswith(rel + ".")] if rel not in r.files else [rel]
        for h in hits: print(extract(r, h, outdir), os.path.getsize(os.path.join(outdir, h.replace("/", "__"))))
