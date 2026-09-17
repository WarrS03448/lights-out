#!/usr/bin/env python3
"""Parse a cooked UE5 AssetRegistry.bin (registry version 17, fixed-tag format) into (package, asset, class, chunks).
Usage: python3 assetregistry.py <AssetRegistry.bin> [<out.tsv>]
Notes (learned on Bodycam, 2026-09-14): header = 16-byte version GUID + int32 version + int32 flag; then a name batch
(uint32 Num, uint32 NumStringBytes, uint64 HashAlgo, uint64 hashes[Num], big-endian uint16 headers[Num] (bit15 = wide, low 15 bits = length), strings);
then the tag store (magic 0x12345679 ... 0x87654321; locate the end magic rather than computing sizes); then int32 NumAssets and per asset:
PackagePath, ClassPackage, ClassName, PackageName, AssetName (each a uint32 name index; high bit set => a uint32 number follows), uint64 tag-map handle,
OptionalOuterPath (name), int32 NumChunks + chunk ids, uint32 PackageFlags. Bodycam's cooked registry has no dependency section.
"""
import struct, sys

def parse(path):
    d = open(path, "rb").read()
    p = 20 + 4
    num, nbytes = struct.unpack_from("<II", d, p); p += 8 + 8 + 8 * num
    headers = [struct.unpack_from(">H", d, p + 2 * i)[0] for i in range(num)]; p += 2 * num
    names = []
    for h in headers:
        ln = h & 0x7fff
        if h >> 15: names.append(d[p:p + 2 * ln].decode("utf-16-le")); p += 2 * ln
        else: names.append(d[p:p + ln].decode("latin-1")); p += ln
    end = d.find(struct.pack("<I", 0x87654321), p); assert end > 0
    p = end + 4
    def nm(q):
        v = struct.unpack_from("<I", d, q)[0]; q += 4
        if v & 0x80000000:
            n = struct.unpack_from("<I", d, q)[0]; q += 4
            return names[v & 0x7fffffff] + (f"_{n - 1}" if n else ""), q
        return names[v], q
    n_assets = struct.unpack_from("<i", d, p)[0]; p += 4
    out = []
    for _ in range(n_assets):
        pp, p = nm(p); cp, p = nm(p); cn, p = nm(p); pn, p = nm(p); an, p = nm(p)
        p += 8; _outer, p = nm(p)
        nch = struct.unpack_from("<i", d, p)[0]; p += 4
        chunks = list(struct.unpack_from("<%di" % nch, d, p)); p += 4 * nch
        p += 4
        out.append((pn, an, cn, chunks))
    return names, out

if __name__ == "__main__":
    names, assets = parse(sys.argv[1])
    print(len(names), "names,", len(assets), "assets")
    if len(sys.argv) > 2:
        with open(sys.argv[2], "w") as f:
            f.write("package\tasset\tclass\tchunks\n")
            for pn, an, cn, ch in sorted(set((a[0], a[1], a[2], ",".join(map(str, a[3]))) for a in assets)): f.write(f"{pn}\t{an}\t{cn}\t{ch}\n")
