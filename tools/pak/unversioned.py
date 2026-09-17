"""Sparse (un)packing of Unreal *unversioned* property fragments, for exports whose property sizes we know.

    entries, end = parse(data, sizes)      # sizes: {prop_index: int | callable(data, pos) -> size}; returns {idx: bytes | None (zero)}
    blob = pack(entries)                   # rebuilds fragment headers + zero mask + values

Fragment header (uint16): SkipNum = v & 0x7f, HasZeros = v & 0x80, IsLast = v & 0x100, ValueNum = v >> 9.
Zero mask (only if any fragment HasZeros): one bit per value of every HasZeros fragment, packed as uint8 (<= 8 bits),
uint16 (<= 16) or uint32 words.  Values follow in property order; zero-flagged values are not stored.
"""
import struct

def parse(data, sizes):
    p = 0; frags = []
    while True:
        h = struct.unpack_from("<H", data, p)[0]; p += 2
        frags.append((h & 0x7f, bool(h & 0x80), h >> 9))
        if h & 0x100: break
    nz = sum(v for s, z, v in frags if z)
    mask = 0
    if nz:
        if nz <= 8: mask = data[p]; p += 1
        elif nz <= 16: mask = struct.unpack_from("<H", data, p)[0]; p += 2
        else:
            words = (nz + 31) // 32; mask = int.from_bytes(data[p:p + 4 * words], "little"); p += 4 * words
    entries = {}; idx = 0; bit = 0
    for skip, z, n in frags:
        idx += skip
        for _ in range(n):
            zero = False
            if z: zero = bool(mask >> bit & 1); bit += 1
            if zero: entries[idx] = None
            else:
                sz = sizes[idx]; sz = sz(data, p) if callable(sz) else sz
                entries[idx] = bytes(data[p:p + sz]); p += sz
            idx += 1
    return entries, p

def pack(entries):
    idxs = sorted(entries); frags = []   # [skip, [idx...]]
    prev = 0
    for i in idxs:
        if frags and i == prev + 1 and len(frags[-1][1]) < 127: frags[-1][1].append(i)
        else:
            skip = i - prev - (1 if frags else 0)
            while skip > 127: frags.append([127, []]); skip -= 127   # empty fragments to skip far
            frags.append([skip, [i]])
        prev = i
    hdr = b""; maskbits = []; vals = b""
    for k, (skip, ids) in enumerate(frags):
        z = any(entries[i] is None for i in ids)
        h = skip | (0x80 if z else 0) | (0x100 if k == len(frags) - 1 else 0) | (len(ids) << 9)
        hdr += struct.pack("<H", h)
        for i in ids:
            if z: maskbits.append(entries[i] is None)
            if entries[i] is not None: vals += entries[i]
    mask = b""
    if maskbits:
        m = sum(1 << b for b, on in enumerate(maskbits) if on); n = len(maskbits)
        mask = bytes([m]) if n <= 8 else struct.pack("<H", m) if n <= 16 else m.to_bytes(4 * ((n + 31) // 32), "little")
    return hdr + mask + vals

if __name__ == "__main__":
    # self-test on the stock config assets and GM CDOs
    tests = {
        "DM DA":  "00 07 03 03 00 00 c0 40 00 03 28 00 00 00 01 03 07 00 00 00 00 00 00 00",
        "TDM DA": "00 07 03 03 00 00 c0 40 00 03 4b 00 00 00 00 05 05 00 00 00 0a 00 00 00 00 00 00 00",
        "BB DA":  "00 07 80 04 01 03 02 00 00 34 43 00 00 c0 40 00 05 0a 00 00 00 0a 00 00 00 00 05 05 00 00 00 0a 00 00 00 00 00 00 00",
        "DM CDO": "04 02 8d 05 02 02 0a 00 00 00 00 00 00 00",
        "TDM CDO": "04 02 06 04 05 05 08 03 00 00 00 09 00 00 00 00 00 00 00 08 00 00 00 00 00 00 00 07 00 00 00 00 00 00 00 f0 ff ff ff 0a 00 00 00 03 00 00 00 00 00 00 00",
    }
    tagc = lambda d, p: 4 + 8 * struct.unpack_from("<i", d, p)[0]
    def nested(sizes):  # a nested unversioned struct value: parse it to learn its size
        def f(d, p):
            _, end = parse(d[p:], sizes); return end
        return f
    A = {0: 4, 1: 4, 2: 4, 3: 4, 4: 4, 5: 4}; B = {0: 4, 1: 4}; Cc = {0: 4, 1: 4}
    DA_SIZES = {0: nested(A), 1: nested(B), 2: nested(Cc)}
    CDO_SIZES = {4: 1, 11: tagc, 12: 4, 18: 4, 19: 4}
    for name, hx in tests.items():
        d = bytes.fromhex(hx); sizes = DA_SIZES if "DA" in name else CDO_SIZES
        e, end = parse(d, sizes); rebuilt = pack(e) + d[end:]
        print(f"{name:8s} round trip {'OK ' if rebuilt == d else 'BAD'} entries={{{', '.join(f'{k}:{(v.hex() if v else None)}' for k, v in e.items())}}} trailer={d[end:].hex()}")
        if "DA" in name:
            for k, v in e.items():
                inner, _ = parse(v, {0: 4, 1: 4, 2: 4, 3: 4, 4: 4, 5: 4}); print("      struct", k, {i: (struct.unpack('<i', x)[0], struct.unpack('<f', x)[0]) if x else None for i, x in inner.items()})
