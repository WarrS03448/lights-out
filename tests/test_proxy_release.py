"""Run: python -m pytest tests/test_proxy_release.py (no game launch or install)."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys
from urllib.parse import urlsplit
import zipfile
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools/release'))
import publish

LEGACY = 'lightsout.up.railway.app'
PUBLIC = 'play.lightsoutranked.com'
EXPECTED = {'GM_CHJoin.uexp': 1, 'GM_CHLobby.uexp': 3, 'GM_BB5.uexp': 16,
            'BP_BB5MigrationRequest.uexp': 1, 'BP_BB5StartRequest.uexp': 1,
            'BP_BB5TeamRequest.uexp': 9, 'BP_CHCombatRequest.uexp': 1,
            'BP_BB5Recovery.uexp': 2, 'BP_BB5RestoreRequest.uexp': 1}


def shipped_tree(tmp_path):
    shutil.copytree(ROOT / 'hub/lobbyseed', tmp_path / 'seed')
    catalogue = json.loads((ROOT / 'server/public/catalogue.json').read_text(encoding='utf-8'))
    entry = next(g for g in catalogue['gamemodes'] if g['id'] == 'BB5')
    pack = ROOT / 'server/public' / urlsplit(entry['pack_url']).path.lstrip('/')
    assert hashlib.sha256(pack.read_bytes()).hexdigest() == entry['sha256']
    with zipfile.ZipFile(pack) as archive:
        archive.extractall(tmp_path / 'pack')
    return tmp_path


def test_actual_shipped_endpoints_change_only_inline_string_bytes(tmp_path):
    import endpoint_assets
    tree = shipped_tree(tmp_path)
    before = {p.relative_to(tree): p.read_bytes() for p in tree.rglob('*') if p.is_file()}
    # Works before and after the migration; reverse transformation is independently checked.
    source = PUBLIC if PUBLIC.encode() in b''.join(before.values()) else LEGACY
    target = 'lorecord1.up.railway.app'
    proof = []
    endpoint_assets.retarget_tree(tree, source, target, proof)
    assert {Path(p['asset']).name: p['endpoints'] for p in proof} == EXPECTED
    assert len(proof) == 9
    for rel, old in before.items():
        new = (tree / rel).read_bytes()
        assert len(new) == len(old)
        assert new == old.replace(source.encode(), target.encode())
        assert new.replace(target.encode(), source.encode()) == old
        if rel.suffix == '.uasset':
            assert new == old
    assert sum(p['endpoints'] for p in proof) == 35
    with pytest.raises(ValueError, match='No endpoint'):
        endpoint_assets.retarget_tree(tree, source, target, [])


def test_endpoint_transform_rejects_unsafe_or_unaccounted_input(tmp_path):
    import endpoint_assets
    for source, target in [(LEGACY, LEGACY), (LEGACY, 'short.example'), (LEGACY, '\u00e9' * 24)]:
        with pytest.raises(ValueError):
            endpoint_assets.retarget_bytes(b'https://' + LEGACY.encode(), source, target)
    with pytest.raises(ValueError, match='already'):
        endpoint_assets.retarget_bytes((LEGACY + PUBLIC).encode(), LEGACY, PUBLIC)
    path = tmp_path / 'unexpected.txt'
    path.write_text(LEGACY, encoding='utf-8')
    with pytest.raises(ValueError, match='outside'):
        endpoint_assets.retarget_tree(tmp_path, LEGACY, PUBLIC, [])
    assert path.read_text(encoding='utf-8') == LEGACY


def test_publisher_migrates_only_owned_catalogue_urls(tmp_path, monkeypatch):
    path = tmp_path / 'catalogue.json'
    monkeypatch.setattr(publish, 'CATALOGUE', str(path))
    c = {'hub': {'download_url': 'https://' + LEGACY + '/hub/a.exe?x=1',
                 'page_url': 'https://' + LEGACY + '/'},
         'gamemodes': [{'pack_url': 'https://' + LEGACY + '/packs/a.zip'},
                       {'pack_url': 'https://other.example/packs/b.zip'}],
         'description': 'Text about https://' + LEGACY}
    publish.save_catalogue(c)
    actual = json.loads(path.read_text(encoding='utf-8'))
    assert publish.origin() == 'https://' + PUBLIC
    assert actual['hub']['download_url'] == 'https://' + PUBLIC + '/hub/a.exe?x=1'
    assert actual['hub']['page_url'] == 'https://lightsoutranked.com/'
    assert actual['gamemodes'][0]['pack_url'] == 'https://' + PUBLIC + '/packs/a.zip'
    assert actual['gamemodes'][1]['pack_url'] == c['gamemodes'][1]['pack_url']
    assert actual['description'] == c['description']


def test_runtime_and_regeneration_sources_use_owned_origin():
    for name in ('bb5_graphs.py', 'lobby_graphs.py'):
        first = (ROOT / 'mirror/Bodycam/Scripts' / name).read_bytes()
        # The public source exports one regeneration copy; deployment mirrors stay private.
        assert LEGACY.encode() not in first
        assert PUBLIC.encode() in first
    spec = importlib.util.spec_from_file_location('proxy_version', ROOT / 'hub/version.py')
    version = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(version)
    assert version.DEFAULT_API_BASE == 'https://' + PUBLIC


def test_publisher_endpoint_release_preserves_pack_contents_and_ctf(tmp_path, monkeypatch):
    # The authored baseline fixture keeps this regression independent of private Git history.
    baseline_pack = ROOT / 'tests/fixtures/proxy/BB5-1.0.28.zip'
    assert hashlib.sha256(baseline_pack.read_bytes()).hexdigest() == '7891a9b49edcda712e8a03bf09dd0213ca94afe3cd5afb961c5bf85f91e9ba0f'
    (tmp_path / 'server/public/packs').mkdir(parents=True)
    shutil.copy2(baseline_pack, tmp_path / 'server/public/packs' / baseline_pack.name)
    shutil.copy2(ROOT / 'server/public/packs/CTF-1.0.4.zip', tmp_path / 'server/public/packs/CTF-1.0.4.zip')
    catalogue = json.loads((ROOT / 'server/public/catalogue.json').read_text(encoding='utf-8'))
    entry = next(g for g in catalogue['gamemodes'] if g['id'] == 'BB5')
    entry.update(version='1.0.28', sha256=hashlib.sha256(baseline_pack.read_bytes()).hexdigest(),
                 size=baseline_pack.stat().st_size, pack_url='https://' + LEGACY + '/packs/BB5-1.0.28.zip')
    (tmp_path / 'server/public/catalogue.json').write_text(json.dumps(catalogue), encoding='utf-8')
    with zipfile.ZipFile(baseline_pack) as archive:
        manifest = archive.read('manifest.json')
    (tmp_path / 'gamemodes/bb5').mkdir(parents=True)
    (tmp_path / 'gamemodes/bb5/manifest.json').write_bytes(manifest)
    shutil.copytree(ROOT / 'tests/fixtures/proxy/lobbyseed', tmp_path / 'hub/lobbyseed')
    for path in (tmp_path / 'hub/lobbyseed').rglob('*.uexp'):
        path.write_bytes(path.read_bytes().replace(PUBLIC.encode(), LEGACY.encode()))
    monkeypatch.setattr(publish, 'ROOT', str(tmp_path))
    monkeypatch.setattr(publish, 'PUBLIC', str(tmp_path / 'server/public'))
    monkeypatch.setattr(publish, 'CATALOGUE', str(tmp_path / 'server/public/catalogue.json'))
    ctf = (tmp_path / 'server/public/packs/CTF-1.0.4.zip').read_bytes()
    with zipfile.ZipFile(tmp_path / 'server/public/packs/BB5-1.0.28.zip') as z:
        before = {n: z.read(n) for n in z.namelist()}
    original = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    with monkeypatch.context() as fail:
        fail.setattr(publish, 'save_catalogue', lambda _: (_ for _ in ()).throw(OSError('disk failure')))
        with pytest.raises(OSError, match='disk failure'):
            publish.publish_proxy_endpoints(SimpleNamespace(version='1.0.29'))
    assert {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()} == original
    publish.publish_proxy_endpoints(SimpleNamespace(version='1.0.29'))
    c = publish.load_catalogue()
    entry = next(g for g in c['gamemodes'] if g['id'] == 'BB5')
    archive_path = tmp_path / 'server/public/packs/BB5-1.0.29.zip'
    assert entry['sha256'] == hashlib.sha256(archive_path.read_bytes()).hexdigest()
    assert entry['pack_url'] == 'https://' + PUBLIC + '/packs/BB5-1.0.29.zip'
    with zipfile.ZipFile(archive_path) as z:
        assert set(z.namelist()) == set(before)
        for name, raw in before.items():
            if name == 'manifest.json':
                manifest = json.loads(raw)
                manifest['version'] = '1.0.29'
                assert json.loads(z.read(name)) == manifest
            else:
                assert z.read(name) == raw.replace(LEGACY.encode(), PUBLIC.encode())
    assert (tmp_path / 'server/public/packs/CTF-1.0.4.zip').read_bytes() == ctf
    with pytest.raises(ValueError, match='baseline'):
        publish.publish_proxy_endpoints(SimpleNamespace(version='1.0.30'))


def test_deploy_from_worktree_pushes_explicit_target_without_switching_checkout(tmp_path, monkeypatch):
    (tmp_path / '.git').write_text('gitdir: /example/main.git/worktrees/release', encoding='utf-8')
    monkeypatch.setattr(publish, 'ROOT', str(tmp_path))
    commands=[]
    monkeypatch.setattr(publish, 'run', lambda command, **_: commands.append(command) or 0)
    monkeypatch.setattr(publish.subprocess, 'run', lambda command, **_: SimpleNamespace(stdout=''))
    publish.deploy('Release test', target_branch='main')
    assert ['git','push','origin','HEAD:refs/heads/main'] in commands
    assert not any('checkout' in command for command in commands)
