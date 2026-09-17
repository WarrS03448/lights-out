import sys, os, subprocess, re
sys.path.insert(0, '/tmp')
from paklib import PakReader
OOZ = "/tmp/oozcli/target/release/oozcli"
pak, outdir = sys.argv[1], sys.argv[2]
skip = re.compile(sys.argv[3]) if len(sys.argv) > 3 else None
r = PakReader(pak); os.makedirs(outdir, exist_ok=True)
n = 0
for rel in sorted(r.files):
    if not (rel.endswith('.uexp') or rel.endswith('.uasset') or rel.endswith('.umap')): continue
    if skip and skip.search(rel): continue
    out = os.path.join(outdir, rel.replace('/', '__'))
    if os.path.exists(out): n += 1; continue
    e, blocks = r.raw_blocks(rel)
    if e["comp_idx"] == 0: data = blocks[0]
    else:
        data = b""
        for blk in blocks:
            want = min(e["block_size"], e["usize"] - len(data)) if e["block_size"] else e["usize"]
            open(out + ".blk", "wb").write(blk)
            subprocess.check_call([OOZ, out + ".blk", str(want), out + ".dec"], stdout=subprocess.DEVNULL)
            data += open(out + ".dec", "rb").read()
        os.remove(out + ".blk"); os.remove(out + ".dec")
    assert len(data) == e["usize"], rel
    open(out, "wb").write(data); n += 1
print(pak, "extracted", n)
