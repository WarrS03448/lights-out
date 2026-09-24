"""BB1 single-site maps, assembled from each player's installed game.

Called by build_gamemode, never ships extracted map bytes. Fingerprints pin the
reviewed game build. The six-byte empty CurveFloat payload was independently
cooked with UE 5.5.4; map loading and planting were checked in private gameplay.
"""
from pathlib import Path
import copy
import hashlib
import struct
from kismet import Pkg

C = "Bodycam/Content/"
GROUPS = ["ser_before_ser", "create_before_ser", "ser_before_create", "create_before_create"]
CASES = {
    "BombHouse": ("BombHouse", "BombZone_C_0",
        "41b2cb7509b227b71aa0f1bc281cda59debece33a56a8c08684346afe70c9a8e",
        "1a6516a9877d61e1599e12c0fdc68fef1d635572fa693196992983a71bfa5f17"),
    "Airsoft": ("AirSoft", "BombZone_C_1",
        "c8d94105e777f0990350720da01824853cc6af6c4fdfb913bfbc4c889705de19",
        "fe653ccca8ca350f9a91d37a480631490aa7dbec455f3c406045fc94f08ed838"),
}


def verify_source(key, header, payload):
    if key not in CASES:
        raise ValueError("Unapproved 1v1 map")
    if (sha(header), sha(payload)) != CASES[key][2:]:
        raise ValueError("The installed game version does not match the reviewed 1v1 maps. No map was installed.")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def materialize(files, full, folder):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / full.replace("/", "__")
    path.write_bytes(files[full])
    path.with_suffix(".uexp").write_bytes(files[full.rsplit(".", 1)[0] + ".uexp"])
    return Pkg(str(path), str(path.with_suffix(".uexp")))


def actor_list(pkg):
    ids = [i for i, e in enumerate(pkg.pk.exports) if pkg.class_name(e) == "Level"]
    assert len(ids) == 1, "one persistent level required"
    i = ids[0]
    data = pkg.export_data(i)
    # This is deliberately a pinned-build probe, not a general UE parser.
    assert data[:6] == bytes.fromhex("01 02 0e 02 07 03")
    count = struct.unpack_from("<i", data, 34)[0]
    refs = list(struct.unpack_from(f"<{count}i", data, 38))
    assert refs[0] > 0 and pkg.class_name(pkg.pk.exports[refs[0] - 1]) == "WorldSettings"
    assert all(0 <= ref <= len(pkg.pk.exports) for ref in refs)
    assert data[38 + count * 4:38 + count * 4 + 11] == b"\x07\0\0\0unreal\0"
    return i, refs


def dependency_groups(pk):
    result = []
    for export in pk.exports:
        offset = export["first_dep"]
        groups = {}
        for key in GROUPS:
            count = export[key]
            assert count >= 0
            groups[key] = pk.preload[offset:offset + count] if count else []
            assert len(groups[key]) == count
            offset += count
        result.append(groups)
    return result


def set_dependencies(pk, groups):
    pk.preload = []
    for export, parts in zip(pk.exports, groups):
        export["first_dep"] = len(pk.preload)
        for group in GROUPS:
            export[group] = len(parts[group])
            pk.preload.extend(parts[group])


def read_depends(pk):
    result, offset = [], 0
    for _ in pk.exports:
        count = struct.unpack_from("<i", pk.depends, offset)[0]
        assert count >= 0
        offset += 4
        result.append(list(struct.unpack_from(f"<{count}i", pk.depends, offset)))
        offset += count * 4
    assert offset == len(pk.depends)
    return result


def clone_layer(source, new_pkg, remove_name):
    pk = copy.deepcopy(source.pk)
    assert pk.serialize() == pk.src, "source must round-trip exactly"
    level_i, actors = actor_list(source)
    sites = [i + 1 for i, e in enumerate(pk.exports) if source.class_name(e) == "BombZone_C"]
    assert len(sites) == 2 and all(i in actors for i in sites)
    remove = next(i for i in sites if source.ref(i) == remove_name)
    removed = {remove}
    while True:
        owned = {i + 1 for i, e in enumerate(pk.exports) if e["outer_idx"] in removed}
        if owned <= removed:
            break
        removed |= owned
    assert len(removed) == 6, "expected exactly one site and five owned components"
    # An orphan BombZone can remain globally discoverable. Convert all six slots
    # into inert CurveFloat placeholders instead of relying on client/server flags.
    # Every unrelated export index remains stable, including bulk-data owners.
    inert_payload = bytes.fromhex("02 01 00 00 00 00")
    object_package = pk.add_import("/Script/CoreUObject", "Package", 0, "/Script/Engine")
    object_class = pk.add_import("/Script/CoreUObject", "Class", object_package, "CurveFloat")
    object_default = pk.add_import("/Script/Engine", "CurveFloat", object_package, "Default__CurveFloat")
    chunks = [source.export_data(i) for i in range(len(pk.exports))]
    groups = dependency_groups(pk)
    depends = read_depends(pk)
    for i, export in enumerate(pk.exports):
        if i + 1 in removed:
            export.update(class_idx=object_class, super_idx=0, template_idx=object_default,
                          outer_idx=0, name_idx=pk.add_name(f"BB1_RemovedSiteSlot{i + 1}X"), name_num=0,
                          object_flags=0, forced_export=0, not_for_client=0, not_for_server=0,
                          inherited_instance=0, not_always_loaded=0, is_asset=0,
                          generate_public_hash=0)
            chunks[i] = inert_payload
            groups[i] = {key: [] for key in GROUPS}
            groups[i]["ser_before_create"] = [object_class, object_default]
            depends[i] = []
        else:
            for field in ("class_idx", "super_idx", "template_idx", "outer_idx"):
                assert export[field] not in removed, (i, field, export[field])
            for key in GROUPS:
                hits = set(groups[i][key]) & removed
                assert not hits or (i == level_i and hits == {remove}), (i, key, hits)
                groups[i][key] = [ref for ref in groups[i][key] if ref not in removed]
            hits = set(depends[i]) & removed
            assert not hits or i == level_i, (i, hits)
            depends[i] = [ref for ref in depends[i] if ref not in removed]
    keep_actors = [ref for ref in actors if ref != remove]
    level_data = chunks[level_i]
    chunks[level_i] = level_data[:34] + struct.pack("<i", len(keep_actors)) + struct.pack(
        f"<{len(keep_actors)}i", *keep_actors) + level_data[38 + 4 * len(actors):]
    for export, chunk in zip(pk.exports, chunks):
        export["serial_size"] = len(chunk)
    set_dependencies(pk, groups)
    pk.depends = b"".join(struct.pack("<i", len(refs)) + struct.pack(f"<{len(refs)}i", *refs) for refs in depends)
    from build_gamemode import rename
    rename(pk, new_pkg, source.pk.package_name.rsplit("/", 1)[1], new_pkg.rsplit("/", 1)[1])
    # Export payload footer, trailer and bulk records are preserved exactly.
    original_end = max(e["serial_offset"] - source.hdr + e["serial_size"] for e in source.pk.exports)
    payload = b"".join(chunks) + source.b[original_end:]
    return pk.serialize(), payload, sorted(removed), sites


def assert_layer(source, clone, removed, sites):
    pk = clone.pk
    level_i, actors = actor_list(clone)
    assert len(pk.exports) == len(source.pk.exports)
    assert [i + 1 for i, e in enumerate(pk.exports) if clone.class_name(e) == "BombZone_C"] == [i for i in sites if i not in removed]
    assert not set(actors) & set(removed)
    assert sum(clone.class_name(pk.exports[i - 1]) == "BombZone_C" for i in actors if i) == 1
    assert not set(pk.preload) & set(removed)
    for i, export in enumerate(pk.exports):
        if i + 1 in removed:
            assert clone.class_name(export) == "CurveFloat" and export["outer_idx"] == 0
            assert clone.export_data(i) == bytes.fromhex("02 01 00 00 00 00")
        elif i != level_i:
            assert clone.export_data(i) == source.export_data(i), (i, "unrelated export changed")
        for field in ("class_idx", "template_idx", "super_idx", "outer_idx"):
            assert -len(pk.imports) <= export[field] <= len(pk.exports)
    assert pk.trailer == source.pk.trailer, "bulk resource owners/offsets unchanged"
    assert pk.serialize() == pk.src, "output round trip"
    dependency_groups(pk)
    read_depends(pk)



def apply_map(builder, key, level_name):
    """Add one validated BB1 layer and retarget only its BB1 wrapper."""
    stem, remove_name, *_ = CASES[key]
    source_pkg = f"/Game/Map/{stem}/{stem}_BP"
    full = C + source_pkg[len('/Game/'):] + '.umap'
    # Bypass the persistent stock cache: a game update must fail the pinned hash.
    source_files = {full: builder.ps.read(full),
                    full[:-5] + '.uexp': builder.ps.read(full[:-5] + '.uexp')}
    verify_source(key, source_files[full], source_files[full[:-5] + '.uexp'])
    inspect = Path(builder.work) / 'single-site'
    source = materialize(source_files, full, inspect / 'source')
    new_name = f"BB1_{stem}_OneSite_BP"
    new_pkg = f"/Game/GM_Maps/Community/BB1/SingleSite/{new_name}"
    new_full = C + new_pkg[len('/Game/'):] + '.umap'
    header, payload, removed, sites = clone_layer(source, new_pkg, remove_name)
    layer_files = {new_full: header, new_full[:-5] + '.uexp': payload}
    clone = materialize(layer_files, new_full, inspect / 'checked')
    assert_layer(source, clone, removed, sites)
    wrapper_full = f"{C}GM_Maps/Community/BB1/BB1_{level_name}.umap"
    wrapper = materialize(builder.files, wrapper_full, inspect / 'wrapper')
    wp = wrapper.pk
    result = bytearray(wrapper.b)
    matches = 0
    for i, export in enumerate(wp.exports):
        if wrapper.class_name(export) != 'LevelStreamingAlwaysLoaded':
            continue
        data = wrapper.export_data(i)
        package_idx = struct.unpack_from('<i', data, 4)[0]
        if wrapper.names[package_idx] != source_pkg:
            continue
        if data[:4] != bytes.fromhex('00 02 0e 03') or wrapper.names[struct.unpack_from('<i', data, 12)[0]] != f'{stem}_BP':
            raise ValueError('1v1 map wrapper format changed')
        offset = export['serial_offset'] - wrapper.hdr
        struct.pack_into('<ii', result, offset + 4, wp.add_name(new_pkg), 0)
        struct.pack_into('<ii', result, offset + 12, wp.add_name(new_name), 0)
        matches += 1
    if matches != 1:
        raise ValueError('1v1 map requires exactly one gameplay streaming layer')
    builder.files.update(layer_files)
    builder.files[wrapper_full] = wp.serialize()
    builder.files[wrapper_full[:-5] + '.uexp'] = bytes(result)
    builder.registry_adds.append((source_pkg, new_pkg))
