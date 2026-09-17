#!/usr/bin/env python3
"""Seekable reader for Unreal .pak v11 files (unencrypted index) — reads only the index and the entries you ask for,
so multi-GB paks are fine. Decompresses Oodle entries with pyooz (or the `oozcli` helper) and Zlib entries natively.

    pf = PakFile(path)                 # pf.mount_point, pf.files {relpath: encoded-entry offset}
    data = pf.read(rel)                # decoded bytes of one file
    PakSet(paks_dir).read("Bodycam/Content/GM/Gamemode/GM_Deathmatch.uasset")   # search all paks (highest priority first)
"""
import os, struct, subprocess, tempfile, zlib
from paklib import read_fstring, MAGIC

try:                       # pip package "pyooz": Python bindings to the ooz Oodle decompressor (Windows/Linux/macOS wheels; what the hub ships)
    import ooz as _ooz
except ImportError:        # dev fallback: the oozcli helper built from the oozextract crate (tools/pak/oozcli)
    _ooz = None

def oodle_decompress(chunk, want):
    """One Oodle block -> `want` bytes of plain data (pyooz in-process when available, else the oozcli helper)."""
    if _ooz is not None:
        out = _ooz.decompress(chunk, want)
        if len(out) != want: raise ValueError(f"ooz: expected {want} bytes, got {len(out)}")
        return out
    with tempfile.TemporaryDirectory() as td:
        bi = os.path.join(td, "b.in"); bo = os.path.join(td, "b.out"); open(bi, "wb").write(chunk)
        subprocess.run([find_oozcli(), bi, str(want), bo], check=True, capture_output=True)
        return open(bo, "rb").read()

def find_oozcli():
    for c in (os.environ.get("OOZCLI"), os.path.join(os.path.dirname(os.path.abspath(__file__)), "oozcli", "target", "release", "oozcli"),
              os.path.join(os.path.dirname(os.path.abspath(__file__)), "oozcli", "target", "release", "oozcli.exe"), "/tmp/oozcli/target/release/oozcli"):
        if c and os.path.exists(c): return c
    raise FileNotFoundError("oozcli not found — build tools/pak/oozcli (cargo build --release) or set OOZCLI")

class PakFile:
    FOOTER = 221
    def __init__(self, path):
        self.path = path; self.f = open(path, "rb"); self.size = os.path.getsize(path)
        self.f.seek(self.size - self.FOOTER); foot = self.f.read(self.FOOTER)
        pos = foot.rfind(struct.pack("<I", MAGIC)); assert pos >= 0, "not a pak"
        self.version = struct.unpack_from("<I", foot, pos + 4)[0]
        self.index_offset, self.index_size = struct.unpack_from("<qq", foot, pos + 8)
        names = foot[pos + 44:pos + 44 + 160]
        self.compression = [names[i:i + 32].rstrip(b"\0").decode() for i in range(0, 160, 32)]
        self.f.seek(self.index_offset); d = self.f.read(self.index_size); p = 0
        self.mount_point, p = read_fstring(d, p)
        self.num_entries, self.seed = struct.unpack_from("<iQ", d, p); p += 12
        has_phi = struct.unpack_from("<i", d, p)[0]; p += 4
        if has_phi: p += 36
        has_fdi = struct.unpack_from("<i", d, p)[0]; p += 4
        fdi_off = fdi_size = None
        if has_fdi: fdi_off, fdi_size = struct.unpack_from("<qq", d, p); p += 36
        enc_sz = struct.unpack_from("<i", d, p)[0]; p += 4
        self.encoded = d[p:p + enc_sz]
        self.files = {}
        if has_fdi:
            self.f.seek(fdi_off); di = self.f.read(fdi_size); q = 0
            ndirs = struct.unpack_from("<i", di, q)[0]; q += 4
            for _ in range(ndirs):
                dn, q = read_fstring(di, q); nf = struct.unpack_from("<i", di, q)[0]; q += 4
                for _ in range(nf):
                    fn, q = read_fstring(di, q); eo = struct.unpack_from("<i", di, q)[0]; q += 4
                    self.files[(dn if dn != "/" else "") + fn] = eo
        # full paths as the engine sees them (mount point without the ../../../ prefix)
        prefix = self.mount_point
        while prefix.startswith("../"): prefix = prefix[3:]
        self.prefix = prefix
        self.fullpaths = {prefix + rel: rel for rel in self.files}

    def decode_entry(self, eo):
        enc = self.encoded; flags = struct.unpack_from("<I", enc, eo)[0]; r = eo + 4
        e = {"comp_idx": (flags >> 23) & 0x3F, "encrypted": (flags >> 22) & 1, "nblocks": (flags >> 6) & 0xFFFF}
        if (flags & 0x3F) == 0x3F: e["block_size"] = struct.unpack_from("<I", enc, r)[0]; r += 4
        else: e["block_size"] = (flags & 0x3F) << 11
        if (flags >> 31) & 1: e["offset"] = struct.unpack_from("<I", enc, r)[0]; r += 4
        else: e["offset"] = struct.unpack_from("<q", enc, r)[0]; r += 8
        if (flags >> 30) & 1: e["usize"] = struct.unpack_from("<I", enc, r)[0]; r += 4
        else: e["usize"] = struct.unpack_from("<q", enc, r)[0]; r += 8
        if e["comp_idx"] != 0:
            if (flags >> 29) & 1: e["size"] = struct.unpack_from("<I", enc, r)[0]; r += 4
            else: e["size"] = struct.unpack_from("<q", enc, r)[0]; r += 8
        else: e["size"] = e["usize"]
        blocks = []
        if e["nblocks"] > 0:
            header = 53 + 4 + 16 * e["nblocks"]
            if e["nblocks"] == 1 and not e["encrypted"]: blocks.append((header, header + e["size"]))
            else:
                off = header
                for _ in range(e["nblocks"]):
                    bs = struct.unpack_from("<I", enc, r)[0]; r += 4; blocks.append((off, off + bs)); off += bs
        e["blocks"] = blocks
        return e

    def read(self, rel):
        e = self.decode_entry(self.files[rel]); assert not e["encrypted"], f"{rel} is encrypted"
        if e["comp_idx"] == 0:
            self.f.seek(e["offset"] + 53); return self.f.read(e["size"])
        method = self.compression[e["comp_idx"] - 1]
        end = max(b[1] for b in e["blocks"]); self.f.seek(e["offset"]); raw = self.f.read(end)
        out = b""
        for (s, t) in e["blocks"]:
            want = min(e["block_size"], e["usize"] - len(out)) if e["block_size"] else e["usize"]
            chunk = raw[s:t]
            if method == "Zlib": out += zlib.decompress(chunk)
            elif method == "Oodle": out += oodle_decompress(chunk, want)
            else: raise ValueError(f"unsupported compression {method}")
        assert len(out) == e["usize"], (rel, len(out), e["usize"])
        return out

    # The handle stays open for the object's life (that is the point: seekable reads out of a
    # multi-GB pak without loading it). On Windows that also means the file cannot be REPLACED while
    # a PakFile on it is alive - os.replace raises WinError 32 - so any caller that reads a pak and
    # then installs over it has to close first. Use `with PakFile(p) as pf:` when the pak is one we
    # also write, e.g. hub/lobbypak.py swapping ~mods\CommunityLobby_P.pak between matches.
    def close(self):
        f = getattr(self, "f", None)
        if f is not None and not f.closed: f.close()
    def __enter__(self): return self
    def __exit__(self, *exc): self.close(); return False
    def __del__(self):
        try: self.close()
        except Exception: pass

class PakSet:
    """All paks in a folder (plus optional extra paks), searched by engine-visible full path, e.g. 'Bodycam/Content/GM/...'."""
    def __init__(self, paks_dir, extra=()):
        paths = sorted(os.path.join(paks_dir, f) for f in os.listdir(paks_dir) if f.lower().endswith(".pak")) + list(extra)
        self.paks = [PakFile(p) for p in paths]
        self.where = {}
        for pf in self.paks:
            for full, rel in pf.fullpaths.items(): self.where.setdefault(full, (pf, rel))
    def has(self, full): return full in self.where
    def read(self, full):
        if full not in self.where: raise FileNotFoundError(full)
        pf, rel = self.where[full]; return pf.read(rel)
    def find(self, needle): return sorted(f for f in self.where if needle.lower() in f.lower())

if __name__ == "__main__":
    import sys
    ps = PakSet(sys.argv[1]); print(len(ps.paks), "paks,", len(ps.where), "files")
    for n in sys.argv[2:]: print(n, "->", ps.find(n)[:20])
