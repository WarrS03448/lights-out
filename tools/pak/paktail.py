#!/usr/bin/env python3
"""Read a pak's index from just its TAIL (last N bytes) plus the full file size — for paks too big to copy.
Usage: python3 paktail.py <pak.tail> <full_size> [needle]
Library: PakTailIndex(tail_bytes, full_size) -> .mount_point, .files {rel: entry_offset}, .decode_entry(eo), .raw_span(rel) -> (abs_offset, length)
"""
import struct, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from paklib import read_fstring, MAGIC

class PakTailIndex:
    def __init__(self, tail, full_size):
        self.t = tail; self.base = full_size - len(tail); self.full_size = full_size
        d = tail; pos = len(d) - 512 + d[-512:].rfind(struct.pack("<I", MAGIC))
        self.version = struct.unpack_from("<I", d, pos + 4)[0]
        self.index_offset, self.index_size = struct.unpack_from("<qq", d, pos + 8)
        assert self.index_offset >= self.base, "index not within tail — enlarge the tail"
        p = self.index_offset - self.base
        self.mount_point, p = read_fstring(d, p)
        self.num_entries, self.seed = struct.unpack_from("<iQ", d, p); p += 12
        has_phi = struct.unpack_from("<i", d, p)[0]; p += 4
        if has_phi: p += 36
        has_fdi = struct.unpack_from("<i", d, p)[0]; p += 4
        fdi_off = None
        if has_fdi:
            fdi_off, fdi_size = struct.unpack_from("<qq", d, p); p += 36
        enc_sz = struct.unpack_from("<i", d, p)[0]; p += 4
        self.encoded = d[p:p + enc_sz]
        self.files = {}
        if has_fdi:
            q = fdi_off - self.base; assert q >= 0, "directory index not within tail"
            ndirs = struct.unpack_from("<i", d, q)[0]; q += 4
            for _ in range(ndirs):
                dn, q = read_fstring(d, q); nf = struct.unpack_from("<i", d, q)[0]; q += 4
                for _ in range(nf):
                    fn, q = read_fstring(d, q); eo = struct.unpack_from("<i", d, q)[0]; q += 4
                    self.files[(dn if dn != "/" else "") + fn] = eo

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

    def raw_span(self, rel):
        """Absolute (offset, length) covering the entry's on-disk header + payload."""
        e = self.decode_entry(self.files[rel])
        end = max(b[1] for b in e["blocks"]) if e["blocks"] else 53 + e["size"]
        return e["offset"], end, e

if __name__ == "__main__":
    tail = open(sys.argv[1], "rb").read(); size = int(sys.argv[2])
    ix = PakTailIndex(tail, size)
    print(f"{os.path.basename(sys.argv[1])}: mount={ix.mount_point} files={len(ix.files)}")
    if len(sys.argv) > 3:
        for rel in sorted(ix.files):
            if sys.argv[3].lower() in rel.lower(): print("  ", rel, ix.raw_span(rel)[:2])
