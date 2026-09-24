"""Run: python -m pytest tests/test_gamemode_network_compatibility.py.

Authored synthetic enum fixture; no retail game assets are required or copied.
"""
from itertools import combinations
from pathlib import Path
import struct
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools/pak"))
import build_gamemode as build
from pkgedit import CookedPackage, EXPORT_FIELDS


def stock_enum(tmp_path):
    pk = CookedPackage()
    values = dict(tag=0x9E2A83C1, legacy_ver=-8, legacy_ue3=0, ver4=522,
                  ver5=1009, lic=0, custom_versions=b"", package_name=build.ENUM_PKG,
                  package_flags=0, guid=bytes(16), generations=[(1, 0)],
                  engine_versions=bytes(28), compression_flags=0, package_source=0,
                  chunk_ids=[], names_from_export_data=0, payload_toc=-1,
                  data_resource_offset=-1, names=[], imports=[], exports=[],
                  depends=bytes(4), ar_data=bytes(4), preload=[])
    pk.__dict__.update(values)
    cls = pk.add_import("/Script/CoreUObject", "Class", 0, "UserDefinedEnum")
    names = [(pk.add_name(f"GameMode::Stock{i}"), 0, i) for i in range(13)]
    names.append((pk.add_name("GameMode::GameMode_MAX"), 0, 13))
    body = b"\x00\x03" + bytes(4) + struct.pack("<i", 0) + bytes(4)
    body += struct.pack("<i", len(names))
    body += b"".join(struct.pack("<iiq", *entry) for entry in names) + b"\x01" + build.TAG
    export = {key: 0 for key in EXPORT_FIELDS}
    export.update(class_idx=cls, name_idx=pk.add_name("GameMode"), serial_size=len(body)-4)
    pk.exports = [export]
    ua, ue = tmp_path / "stock.uasset", tmp_path / "stock.uexp"
    ua.write_bytes(pk.serialize())
    ue.write_bytes(body)
    return ua, ue


def enum_entries(ua, u):
    pk = CookedPackage.load(str(ua))
    p = 6
    displays = struct.unpack_from("<i", u, p)[0]
    p += 4
    for _ in range(displays):
        p += 13
        for _ in range(3):
            length = struct.unpack_from("<i", u, p)[0]
            p += 4 + (length if length >= 0 else -2 * length)
    p += 4
    count = struct.unpack_from("<i", u, p)[0]
    p += 4
    result = []
    for _ in range(count):
        index, _, value = struct.unpack_from("<iiq", u, p)
        p += 16
        result.append((pk.names[index][0], value))
    return result


MODES = [("CTF", 13), ("BB5", 14), ("BB1", 15)]
SETS = [list(group) for n in range(1, 4) for group in combinations(MODES, n)]


@pytest.mark.parametrize("modes", SETS)
def test_all_released_mode_combinations_keep_stock_wire_width(tmp_path, modes):
    stock = stock_enum(tmp_path)
    builder = build.Builder.__new__(build.Builder)
    builder.work = str(tmp_path)
    builder.files = {}
    builder.stock_pkg = lambda path: tuple(str(p) for p in stock)
    builder.enum_asset([{"id": name, "_enum": number} for name, number in modes])
    output = tmp_path / "merged.uasset"
    output.write_bytes(builder.files[build.C + "GM/DATA/Enum/GameMode.uasset"])
    entries = enum_entries(output, builder.files[build.C + "GM/DATA/Enum/GameMode.uexp"])
    assert entries[:13] == enum_entries(stock[0], stock[1].read_bytes())[:13]
    highest = max(value for _, value in entries)
    assert highest.bit_length() == 4, "Installing custom modes changed stock network serialization"
    # Older engine-network compatibility uses ceil(log2(MAX)) instead of MAX+1.
    assert (highest - 1).bit_length() == 4
    for name, value in modes:
        first = next(label for label, number in entries if number == value)
        assert first == f"GameMode::NewEnumerator{value}"
    assert entries[-1][0] == "GameMode::GameMode_MAX"


def test_released_bb1_manifest_and_legacy_pack_have_same_safe_identity():
    import json
    manifest = json.loads((ROOT / "gamemodes/bb1/manifest.json").read_text(encoding="utf-8"))
    assert build.mode_enum(manifest, 0) == 15
    assert build.mode_enum({"id": "BB1", "enum": 16}, 0) == 15
    assert build.mode_enum({"id": "BB5", "enum": 14}, 0) == 14
    assert build.mode_enum({"id": "CTF", "enum": 13}, 0) == 13


@pytest.mark.parametrize("value", [0, 12, 16, 17, 255])
def test_new_modes_cannot_expand_or_reuse_stock_network_values(value):
    with pytest.raises(ValueError, match="compatible"):
        build.mode_enum({"id": "Future", "enum": value}, 0)


@pytest.mark.parametrize("modes", [
    [{"id": "BB1", "_enum": 16}],
    [{"id": "BB1", "_enum": 15}, {"id": "DOM", "_enum": 15}],
])
def test_builder_refuses_incompatible_or_colliding_enum_layout(tmp_path, modes):
    stock = stock_enum(tmp_path)
    builder = build.Builder.__new__(build.Builder)
    builder.work = str(tmp_path)
    builder.files = {}
    builder.stock_pkg = lambda path: tuple(str(p) for p in stock)
    with pytest.raises(ValueError, match="compatible|duplicate"):
        builder.enum_asset(modes)
    assert builder.files == {}
