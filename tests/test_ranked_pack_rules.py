"""Verify the served packs produce the intended native match rules at install time."""
import hashlib
import json
from pathlib import Path
import struct
import sys
from urllib.parse import urlsplit
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools/pak'))
from build_gamemode import BASES, Builder, DA_SIZES, _F, uv


@pytest.mark.parametrize('mode,limit,rounds,seconds,switch', [
    ('BB1', 5, 9, 120, 1), ('BB5', 7, 13, 180, 6),
])
def test_served_pack_installs_native_rules(tmp_path, mode, limit, rounds, seconds, switch):
    catalogue = json.loads((ROOT / 'server/public/catalogue.json').read_text(encoding='utf-8'))
    entry = next(m for m in catalogue['gamemodes'] if m['id'] == mode)
    archive = ROOT / 'server/public/packs' / Path(urlsplit(entry['pack_url']).path).name
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == entry['sha256']
    with zipfile.ZipFile(archive) as z:
        z.extractall(tmp_path / mode)
    pack = tmp_path / mode
    manifest = json.loads((pack / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['rules'] == entry['rules']
    assert manifest['rules']['score_limit'] == limit
    assert manifest['rules']['max_rounds'] == rounds
    if mode == 'BB1':
        loc = json.loads((pack / 'loc.json').read_text(encoding='utf-8'))
        descriptions = loc['entries']['BB1.Description']
        assert set(descriptions) == set(manifest['rulesets']) == {'de', 'en', 'es', 'fr', 'pt', 'ru', 'zh'}
        for lang, description in descriptions.items():
            assert ' 5 ' in description and ' 9' in description
            assert manifest['rulesets'][lang].endswith(description)
        assert descriptions['en'] == manifest['description']
    Builder.check_cook_verdict(str(pack / 'cooked'))
    # Exercise the same config transformation used by build_from_packs, without
    # redistributing or requiring any of the retail game's assets in this test.
    builder = Builder.__new__(Builder)
    builder.cooked = str(pack / 'cooked')
    builder.work = str(tmp_path)
    builder.files = {}
    builder.config_asset(manifest, BASES[mode])
    data = builder.files[f'Bodycam/Content/GM/DATA/DataAsset/DA_{mode}.uexp']
    top, _ = uv.parse(data, DA_SIZES)
    phase, _ = uv.parse(top[0], _F)
    score, _ = uv.parse(top[1], _F)
    team, _ = uv.parse(top[2], _F)
    integer = lambda fields, key: struct.unpack('<i', fields[key])[0]
    assert (integer(score, 0), integer(score, 1)) == (limit, rounds)
    assert struct.unpack('<f', phase[0])[0] == seconds
    assert (integer(team, 0), integer(team, 1), integer(team, 2)) == (5, 10, switch)
