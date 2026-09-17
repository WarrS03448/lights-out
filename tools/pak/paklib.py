#!/usr/bin/env python3
"""Minimal Unreal .pak (version 11, unencrypted) reader/writer used for the Bodycam ~mods test.

Usage (library):
    from paklib import PakReader, write_pak
    r = PakReader("pakchunk0-Windows.pak"); r.list(); r.raw_entry("Bodycam/Content/...")
    write_pak("out_P.pak", "../../../", {"Bodycam/Content/x.locres": data})
"""
import hashlib
import struct

MAGIC = 0x5A6F12E1
FOOTER_SIZE_V11 = 221  # 16 guid + 1 encIdx + 4 magic + 4 ver + 8 off + 8 size + 20 hash + 160 compression names


def read_fstring(b, p):
    n = struct.unpack_from("<i", b, p)[0]
    p += 4
    if n < 0:
        raw = b[p:p + (-n) * 2]
        p += (-n) * 2
        return raw.decode("utf-16-le").rstrip("\x00"), p
    raw = b[p:p + n]
    p += n
    return raw.decode("utf-8").rstrip("\x00"), p


def write_fstring(s):
    if s == "":
        return struct.pack("<i", 0)
    if all(ord(c) < 128 for c in s):
        data = s.encode("ascii") + b"\x00"
        return struct.pack("<i", len(data)) + data
    data = s.encode("utf-16-le") + b"\x00\x00"
    return struct.pack("<i", -(len(data) // 2)) + data


def fnv64_path(path, seed):
    """UE FPakFile::HashPath for pak version >= 11: FNV-1 64 over lowercase UTF-16LE, offset basis + seed."""
    OFFSET = 0xCBF29CE484222325
    PRIME = 0x00000100000001B3
    h = (OFFSET + seed) & 0xFFFFFFFFFFFFFFFF
    for byte in path.lower().encode("utf-16-le"):
        h ^= byte
        h = (h * PRIME) & 0xFFFFFFFFFFFFFFFF
    return h


class PakReader:
    def __init__(self, path):
        self.path = path
        self.data = open(path, "rb").read()
        d = self.data
        tail = d[-512:]
        pos = len(d) - 512 + tail.rfind(struct.pack("<I", MAGIC))
        self.footer_pos = pos
        self.version = struct.unpack_from("<I", d, pos + 4)[0]
        self.index_offset, self.index_size = struct.unpack_from("<qq", d, pos + 8)
        self.index_hash = d[pos + 24:pos + 44]
        names = d[pos + 44:pos + 44 + 160]
        self.compression = [names[i:i + 32].rstrip(b"\x00").decode() for i in range(0, 160, 32)]
        self.encrypted_index = d[pos - 1]
        self._parse_index()

    def _parse_index(self):
        d = self.data
        p = self.index_offset
        self.mount_point, p = read_fstring(d, p)
        self.num_entries, self.seed = struct.unpack_from("<iQ", d, p)
        p += 12
        has_phi = struct.unpack_from("<i", d, p)[0]
        p += 4
        self.phi = None
        if has_phi:
            self.phi_offset, self.phi_size = struct.unpack_from("<qq", d, p)
            self.phi_hash = d[p + 16:p + 36]
            p += 36
        has_fdi = struct.unpack_from("<i", d, p)[0]
        p += 4
        if has_fdi:
            self.fdi_offset, self.fdi_size = struct.unpack_from("<qq", d, p)
            self.fdi_hash = d[p + 16:p + 36]
            p += 36
        enc_sz = struct.unpack_from("<i", d, p)[0]
        p += 4
        self.encoded = d[p:p + enc_sz]
        p += enc_sz
        self.num_unencoded = struct.unpack_from("<i", d, p)[0]
        self.index_end = p + 4
        # path hash index
        if has_phi:
            q = self.phi_offset
            n = struct.unpack_from("<i", d, q)[0]
            q += 4
            self.phi = {}
            for _ in range(n):
                h, eo = struct.unpack_from("<Qi", d, q)
                q += 12
                self.phi[h] = eo
            self.pruned_dirs = struct.unpack_from("<i", d, q)[0]
        # full directory index
        self.files = {}
        self.dirs = []
        if has_fdi:
            q = self.fdi_offset
            ndirs = struct.unpack_from("<i", d, q)[0]
            q += 4
            for _ in range(ndirs):
                dn, q = read_fstring(d, q)
                nf = struct.unpack_from("<i", d, q)[0]
                q += 4
                self.dirs.append((dn, nf))
                for _ in range(nf):
                    fn, q = read_fstring(d, q)
                    eo = struct.unpack_from("<i", d, q)[0]
                    q += 4
                    full = (dn if dn != "/" else "") + fn
                    self.files[full] = eo

    def decode_entry(self, eo):
        enc = self.encoded
        flags = struct.unpack_from("<I", enc, eo)[0]
        r = eo + 4
        e = {"flags": flags}
        e["comp_idx"] = (flags >> 23) & 0x3F
        e["encrypted"] = (flags >> 22) & 1
        e["nblocks"] = (flags >> 6) & 0xFFFF
        if (flags & 0x3F) == 0x3F:
            e["block_size"] = struct.unpack_from("<I", enc, r)[0]
            r += 4
        else:
            e["block_size"] = (flags & 0x3F) << 11
        if (flags >> 31) & 1:
            e["offset"] = struct.unpack_from("<I", enc, r)[0]; r += 4
        else:
            e["offset"] = struct.unpack_from("<q", enc, r)[0]; r += 8
        if (flags >> 30) & 1:
            e["usize"] = struct.unpack_from("<I", enc, r)[0]; r += 4
        else:
            e["usize"] = struct.unpack_from("<q", enc, r)[0]; r += 8
        if e["comp_idx"] != 0:
            if (flags >> 29) & 1:
                e["size"] = struct.unpack_from("<I", enc, r)[0]; r += 4
            else:
                e["size"] = struct.unpack_from("<q", enc, r)[0]; r += 8
        else:
            e["size"] = e["usize"]
        blocks = []
        if e["nblocks"] > 0:
            header = 8 + 8 + 8 + 4 + 20 + 4 + 16 * e["nblocks"] + 1 + 4
            if e["nblocks"] == 1 and not e["encrypted"]:
                blocks.append((header, header + e["size"]))
            else:
                off = header
                for _ in range(e["nblocks"]):
                    bs = struct.unpack_from("<I", enc, r)[0]; r += 4
                    blocks.append((off, off + bs))
                    off += bs if not e["encrypted"] else (bs + 15) // 16 * 16
        e["blocks"] = blocks  # relative to entry offset
        return e

    def raw_blocks(self, relpath):
        """Return (entry, [compressed block bytes...]) for a file."""
        eo = self.files[relpath]
        e = self.decode_entry(eo)
        base = e["offset"]
        out = [self.data[base + s:base + t] for (s, t) in e["blocks"]]
        if not out:  # uncompressed: header then data
            header = 8 + 8 + 8 + 4 + 20 + 1 + 4
            out = [self.data[base + header:base + header + e["size"]]]
        return e, out


def _encode_entry(offset, size, usize=None, method=0, block_sizes=(), block_size=0):
    """Encoded FPakEntry (unencrypted). method 0 = stored; otherwise 1-based index into the footer's method list,
    with the compressed block sizes (blocks are contiguous right after the data header)."""
    if usize is None: usize = size
    flags = (method & 0x3F) << 23 | (len(block_sizes) & 0xFFFF) << 6
    if offset <= 0xFFFFFFFF: flags |= 1 << 31
    if usize <= 0xFFFFFFFF: flags |= 1 << 30
    if size <= 0xFFFFFFFF: flags |= 1 << 29
    explicit_bs = False
    if method:
        if block_size % 2048 == 0 and (block_size >> 11) < 0x3F: flags |= (block_size >> 11) & 0x3F
        else: flags |= 0x3F; explicit_bs = True
    out = struct.pack("<I", flags)
    if explicit_bs: out += struct.pack("<I", block_size)
    out += struct.pack("<I", offset) if offset <= 0xFFFFFFFF else struct.pack("<q", offset)
    out += struct.pack("<I", usize) if usize <= 0xFFFFFFFF else struct.pack("<q", usize)
    if method:
        out += struct.pack("<I", size) if size <= 0xFFFFFFFF else struct.pack("<q", size)
        if len(block_sizes) > 1:
            for bs in block_sizes: out += struct.pack("<I", bs)
    return out


def _data_header(size, sha1, usize=None, method=0, block_sizes=(), block_size=0):
    """FPakEntry serialized in front of the file data (Offset written as 0, like UnrealPak)."""
    if usize is None: usize = size
    out = struct.pack("<qqqI", 0, size, usize, method) + sha1
    if method:
        out += struct.pack("<i", len(block_sizes))
        start = 53 + 4 + 16 * len(block_sizes)
        for bs in block_sizes:
            out += struct.pack("<qq", start, start + bs); start += bs
    out += struct.pack("<BI", 0, block_size if method else 0)
    return out


def zlib_blocks(data, block_size=1 << 20, progress=None):
    """progress(bytes_of_data_compressed_so_far) after each block, for a caller drawing a bar."""
    import zlib
    out = []
    for i in range(0, len(data), block_size):
        out.append(zlib.compress(data[i:i + block_size], 9))
        if progress: progress(min(i + block_size, len(data)))
    return out


def write_pak(out_path, mount_point, files, seed=0, compress=(), block_size=1 << 20, progress=None):
    """files: dict relpath (no leading slash) -> bytes. Writes a v11 pak; entries listed in `compress` are stored as
    Zlib blocks (method index 1, name "Zlib" in the footer), everything else uncompressed.

    progress(done_bytes, total_bytes) is called as the entries are encoded, counted in UNCOMPRESSED
    bytes because that is what the time is proportional to (one 68 MB registry outweighs the other
    89 files put together, so per-file granularity alone would stall a progress bar)."""
    body = bytearray()
    encoded = bytearray()
    entry_offsets = {}
    compress = set(compress)
    total = sum(len(d) for d in files.values())
    done = 0
    if progress: progress(0, total)
    for rel in sorted(files):
        data = files[rel]
        entry_offsets[rel] = len(encoded)
        if rel in compress:
            at = done
            blocks = zlib_blocks(data, block_size,
                                 progress=(lambda n, _at=at: progress(_at + n, total)) if progress else None)
            payload = b"".join(blocks); sizes = [len(b) for b in blocks]
            sha = hashlib.sha1(payload).digest()
            encoded += _encode_entry(len(body), len(payload), len(data), 1, sizes, block_size)
            body += _data_header(len(payload), sha, len(data), 1, sizes, block_size)
            body += payload
        else:
            sha = hashlib.sha1(data).digest()
            encoded += _encode_entry(len(body), len(data))
            body += _data_header(len(data), sha)
            body += data
        done += len(data)
        if progress: progress(done, total)
    # directory index with all parent directories
    dirs = {"/": {}}
    for rel in files:
        parts = rel.split("/")
        for i in range(1, len(parts)):
            dirs.setdefault("/".join(parts[:i]) + "/", {})
        d = "/".join(parts[:-1]) + "/" if len(parts) > 1 else "/"
        dirs[d][parts[-1]] = entry_offsets[rel]
    fdi = bytearray(struct.pack("<i", len(dirs)))
    for dn in sorted(dirs):
        fdi += write_fstring(dn)
        fdi += struct.pack("<i", len(dirs[dn]))
        for fn in sorted(dirs[dn]):
            fdi += write_fstring(fn) + struct.pack("<i", dirs[dn][fn])
    phi = bytearray(struct.pack("<i", len(files)))
    for rel in sorted(files):
        phi += struct.pack("<Qi", fnv64_path(rel, seed), entry_offsets[rel])
    phi += fdi  # pruned directory index: ship the complete directory index (nothing pruned)
    index_offset = len(body)
    # primary index size is fixed given lengths, compute offsets
    fixed = (len(write_fstring(mount_point)) + 4 + 8 + 4 + 36 + 4 + 36 + 4 + len(encoded) + 4)
    phi_offset = index_offset + fixed
    fdi_offset = phi_offset + len(phi)
    index = bytearray()
    index += write_fstring(mount_point)
    index += struct.pack("<iQ", len(files), seed)
    index += struct.pack("<i", 1) + struct.pack("<qq", phi_offset, len(phi)) + hashlib.sha1(phi).digest()
    index += struct.pack("<i", 1) + struct.pack("<qq", fdi_offset, len(fdi)) + hashlib.sha1(fdi).digest()
    index += struct.pack("<i", len(encoded)) + encoded
    index += struct.pack("<i", 0)
    assert len(index) == fixed
    footer = bytes(16) + bytes([0]) + struct.pack("<IiqQ", MAGIC, 11, index_offset, len(index))
    methods = b"Zlib".ljust(32, b"\0") + bytes(128) if compress else bytes(160)
    footer += hashlib.sha1(index).digest() + methods
    assert len(footer) == FOOTER_SIZE_V11
    with open(out_path, "wb") as f:
        f.write(body)
        f.write(index)
        f.write(phi)
        f.write(fdi)
        f.write(footer)
    return out_path
