"""Read version identity from PE section bytes, never an external MUI resource.

Run via hub.update_trust with the verified file locked. Deliberately accepts only
the standard single RT_VERSION resource used by our signed Windows releases.
"""
import struct


def _slice(data, offset, size):
    if offset < 0 or size < 0 or offset + size > len(data):
        raise ValueError("invalid PE resource bounds")
    return data[offset:offset + size]


def _unpack(fmt, data, offset):
    return struct.unpack(fmt, _slice(data, offset, struct.calcsize(fmt)))


def _version_resource(path):
    with open(path, "rb") as file:
        header = file.read(64)
        if header[:2] != b"MZ":
            raise ValueError("not a PE file")
        pe, = _unpack("<I", header, 60)
        if not 64 <= pe <= 1048576:
            raise ValueError("invalid PE header")
        file.seek(pe)
        coff = file.read(24)
        if coff[:4] != b"PE\0\0":
            raise ValueError("not a PE image")
        count, = _unpack("<H", coff, 6)
        optional_size, = _unpack("<H", coff, 20)
        if not 1 <= count <= 96 or not 120 <= optional_size <= 4096:
            raise ValueError("invalid PE sections")
        optional = file.read(optional_size)
        magic, = _unpack("<H", optional, 0)
        directory = {0x10b: 96, 0x20b: 112}.get(magic)
        if directory is None or _unpack("<I", optional, directory - 4)[0] < 3:
            raise ValueError("missing PE resources")
        rva, size = _unpack("<II", optional, directory + 16)
        if not 1 <= size <= 16777216:
            raise ValueError("invalid resource size")
        sections = file.read(40 * count)
        for index in range(count):
            _, address, raw_size, raw = _unpack("<IIII", sections, 40 * index + 8)
            if address <= rva and rva + size <= address + raw_size:
                file.seek(raw + rva - address)
                resource = file.read(size)
                if len(resource) != size:
                    raise ValueError("truncated resources")
                break
        else:
            raise ValueError("resources must reside entirely in a PE section")

    def entries(offset):
        named, ids = _unpack("<HH", resource, offset + 12)
        if named + ids > 4096:
            raise ValueError("too many resource entries")
        return [_unpack("<II", resource, offset + 16 + 8 * i) for i in range(named + ids)]

    types = [target for name, target in entries(0) if name == 16]
    if len(types) != 1 or not types[0] & 0x80000000:
        raise ValueError("missing RT_VERSION")
    names = entries(types[0] & 0x7fffffff)
    if len(names) != 1 or not names[0][1] & 0x80000000:
        raise ValueError("ambiguous version resource")
    languages = entries(names[0][1] & 0x7fffffff)
    if len(languages) != 1 or languages[0][1] & 0x80000000:
        raise ValueError("ambiguous version language")
    location, length = _unpack("<II", resource, languages[0][1])
    return _slice(resource, location - rva, length)


def read_version(path):
    data = _version_resource(path)

    def block(offset, limit):
        length, value_length, kind = _unpack("<HHH", data, offset)
        end = offset + length
        if length < 8 or end > limit or kind not in (0, 1):
            raise ValueError("invalid version block")
        cursor = offset + 6
        while cursor + 2 <= end and data[cursor:cursor + 2] != b"\0\0":
            cursor += 2
        if cursor + 2 > end:
            raise ValueError("unterminated version key")
        key = data[offset + 6:cursor].decode("utf-16le")
        start = (cursor + 5) & ~3
        value_end = start + value_length * (2 if kind else 1)
        if value_end > end:
            raise ValueError("invalid version value")
        return key, data[start:value_end], (value_end + 3) & ~3, end

    key, fixed, cursor, end = block(0, len(data))
    if key != "VS_VERSION_INFO" or len(fixed) != 52 or _unpack("<I", fixed, 0)[0] != 0xfeef04bd:
        raise ValueError("missing fixed version")
    high, low = _unpack("<II", fixed, 8)
    identities = {"ProductName": [], "FileDescription": []}
    while cursor < end:
        key, _, child, next_block = block(cursor, end)
        if key == "StringFileInfo":
            while child < next_block:
                _, _, string, table_end = block(child, next_block)
                while string < table_end:
                    name, value, _, string_end = block(string, table_end)
                    if name in identities:
                        identities[name].append(value.decode("utf-16le").rstrip("\0").strip())
                    string = (string_end + 3) & ~3
                child = (table_end + 3) & ~3
        cursor = (next_block + 3) & ~3
    if any(not values or len(set(values)) != 1 for values in identities.values()):
        raise ValueError("missing or ambiguous executable identity")
    return {"product": identities["ProductName"][0], "description": identities["FileDescription"][0],
            "version": [high >> 16, high & 65535, low >> 16, low & 65535]}
