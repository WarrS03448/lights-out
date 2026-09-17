"""CityHash64 (Google CityHash v1.0.3/1.1 64-bit variant as used by Unreal's FNameHash / name batches). Pure Python."""
# Google CityHash copyright and MIT notice: LICENSES/CityHash-MIT.txt.
import struct

k0 = 0xc3a5c85c97cb3127
k1 = 0xb492b66fbe98f273
k2 = 0x9ae16a3b2f90404f
M64 = (1 << 64) - 1

def _f64(b, i): return struct.unpack_from("<Q", b, i)[0]
def _f32(b, i): return struct.unpack_from("<I", b, i)[0]
def _rot(v, s): return v if s == 0 else ((v >> s) | (v << (64 - s))) & M64
def _shift_mix(v): return (v ^ (v >> 47)) & M64

def _hash128to64(lo, hi):
    kMul = 0x9ddfea08eb382d69
    a = ((lo ^ hi) * kMul) & M64
    a ^= a >> 47
    b = ((hi ^ a) * kMul) & M64
    b ^= b >> 47
    return (b * kMul) & M64

def _hash_len16(u, v, mul=None):
    if mul is None: return _hash128to64(u, v)
    a = ((u ^ v) * mul) & M64
    a ^= a >> 47
    b = ((v ^ a) * mul) & M64
    b ^= b >> 47
    return (b * mul) & M64

def _hash_len0to16(s):
    n = len(s)
    if n >= 8:
        mul = (k2 + n * 2) & M64
        a = (_f64(s, 0) + k2) & M64
        b = _f64(s, n - 8)
        c = (_rot(b, 37) * mul + a) & M64
        d = ((_rot(a, 25) + b) * mul) & M64
        return _hash_len16(c, d, mul)
    if n >= 4:
        mul = (k2 + n * 2) & M64
        a = _f32(s, 0)
        return _hash_len16((n + (a << 3)) & M64, _f32(s, n - 4), mul)
    if n > 0:
        a = s[0]; b = s[n >> 1]; c = s[n - 1]
        y = (a + (b << 8)) & 0xffffffff
        z = (n + (c << 2)) & 0xffffffff
        return (_shift_mix((y * k2 ^ z * k0) & M64) * k2) & M64
    return k2

def _hash_len17to32(s):
    n = len(s)
    mul = (k2 + n * 2) & M64
    a = (_f64(s, 0) * k1) & M64
    b = _f64(s, 8)
    c = (_f64(s, n - 8) * mul) & M64
    d = (_f64(s, n - 16) * k2) & M64
    return _hash_len16((_rot((a + b) & M64, 43) + _rot(c, 30) + d) & M64, (a + _rot((b + k2) & M64, 18) + c) & M64, mul)

def _weak_hash_len32_with_seeds(w, x, y, z, a, b):
    a = (a + w) & M64
    b = _rot((b + a + z) & M64, 21)
    c = a
    a = (a + x) & M64
    a = (a + y) & M64
    b = (b + _rot(a, 44)) & M64
    return (a + z) & M64, (b + c) & M64

def _weak_hash_len32_with_seeds_b(s, i, a, b):
    return _weak_hash_len32_with_seeds(_f64(s, i), _f64(s, i + 8), _f64(s, i + 16), _f64(s, i + 24), a, b)

def _hash_len33to64(s):
    n = len(s)
    mul = (k2 + n * 2) & M64
    a = (_f64(s, 0) * k2) & M64
    b = _f64(s, 8)
    c = (_f64(s, n - 24)) & M64
    d = (_f64(s, n - 32)) & M64
    e = (_f64(s, 16) * k2) & M64
    f = (_f64(s, 24) * 9) & M64
    g = _f64(s, n - 8)
    h = (_f64(s, n - 16) * mul) & M64
    u = (_rot((a + g) & M64, 43) + ((_rot(b, 30) + c) & M64) * 9) & M64
    v = (((a + g) ^ d) + f + 1) & M64
    w = ((_bswap64(((u + v) * mul) & M64)) + h) & M64
    x = (_rot((e + f) & M64, 42) + c) & M64
    y = ((_bswap64(((v + w) * mul) & M64) + g) * mul) & M64
    z = (e + f + c) & M64
    a = (_bswap64(((x + z) * mul + y) & M64) + b) & M64
    b = (_shift_mix(((z + a) * mul + d + h) & M64) * mul) & M64
    return (b + x) & M64

def _bswap64(v): return int.from_bytes(v.to_bytes(8, "little"), "big")

def cityhash64(s: bytes) -> int:
    n = len(s)
    if n <= 16: return _hash_len0to16(s)
    if n <= 32: return _hash_len17to32(s)
    if n <= 64: return _hash_len33to64(s)
    x = _f64(s, n - 40)
    y = (_f64(s, n - 16) + _f64(s, n - 56)) & M64
    z = _hash_len16((_f64(s, n - 48) + n) & M64, _f64(s, n - 24))
    v = _weak_hash_len32_with_seeds_b(s, n - 64, n, z)
    w = _weak_hash_len32_with_seeds_b(s, n - 32, (y + k1) & M64, x)
    x = (x * k1 + _f64(s, 0)) & M64
    n = (n - 1) & ~63
    i = 0
    while True:
        x = (_rot((x + y + v[0] + _f64(s, i + 8)) & M64, 37) * k1) & M64
        y = (_rot((y + v[1] + _f64(s, i + 48)) & M64, 42) * k1) & M64
        x ^= w[1]
        y = (y + v[0] + _f64(s, i + 40)) & M64
        z = (_rot((z + w[0]) & M64, 33) * k1) & M64
        v = _weak_hash_len32_with_seeds_b(s, i, (v[1] * k1) & M64, (x + w[0]) & M64)
        w = _weak_hash_len32_with_seeds_b(s, i + 32, (z + w[1]) & M64, (y + _f64(s, i + 16)) & M64)
        z, x = x, z
        i += 64
        n -= 64
        if n == 0: break
    return _hash_len16(_hash_len16(v[0], w[0]) + (_shift_mix(y) * k1 + z) & M64, (_hash_len16(v[1], w[1]) + x) & M64)

def name_batch_hash(name: str) -> int:
    """Hash stored in a UE name batch: CityHash64 of the lower-cased string bytes (ANSI or UTF-16LE)."""
    if all(ord(c) < 128 for c in name): return cityhash64(name.lower().encode("ascii"))
    return cityhash64(name.lower().encode("utf-16-le"))
