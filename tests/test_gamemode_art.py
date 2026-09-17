"""UTF-8. Run: python -m pytest tests/test_gamemode_art.py."""
import io
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest
from PIL import Image

from hub import mapart
from hub.webui import httpbridge
from tests.test_mapart import texture


@pytest.fixture
def installed_art(tmp_path, monkeypatch):
    mapart.paths.ensure_builder_on_path()
    import pakfile
    reads = []

    class FakePak:
        def __init__(self, path):
            self.f = io.BytesIO()

        def read(self, name):
            reads.append(name)
            return texture()

    monkeypatch.setattr(pakfile, 'PakFile', FakePak)
    monkeypatch.setattr(mapart.paths, 'state_dir', lambda: tmp_path / 'state')
    pak = tmp_path / 'Bodycam/Content/Paks/pakchunk24-Windows.pak'
    pak.parent.mkdir(parents=True)
    pak.write_bytes(b'v1')
    mapart._load.cache_clear()
    yield tmp_path, reads, pak
    mapart._load.cache_clear()


def test_gamemode_route_loads_requested_stock_banners_and_preserves_maps(installed_art):
    game_dir, reads, pak = installed_art
    server, url = httpbridge.start(SimpleNamespace(app=SimpleNamespace(game_dir=game_dir)))
    base = url.rsplit('/', 1)[0]
    try:
        for query in ['gamemode-thumbnail?mode=BB5', 'gamemode-thumbnail?mode=CTF',
                      'map-thumbnail?map=Hospital']:
            with urlopen(base + '/' + query) as response:
                assert response.headers['Content-Type'] == 'image/jpeg'
                assert Image.open(io.BytesIO(response.read())).size == (4, 4)
        assert reads == ['UI/Textures/Banner/T_UI_BannerBodybomb.uexp',
                         'UI/Textures/Banner/T_UI_BannerTeamDeathmatch.uexp',
                         'UI/Textures/Maps/HighresScreenshot00018.uexp']
        for query in ['gamemode-thumbnail', 'gamemode-thumbnail?mode=unknown',
                      'gamemode-thumbnail?mode=../../state.json']:
            with pytest.raises(HTTPError) as error:
                urlopen(base + '/' + query)
            assert error.value.code == 404
        assert len(reads) == 3
        pak.unlink()
        with pytest.raises(HTTPError) as error:
            urlopen(base + '/gamemode-thumbnail?mode=BB5')
        assert error.value.code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_gamemode_cache_refreshes_after_game_update(installed_art):
    game_dir, reads, pak = installed_art
    first = mapart.gamemode_thumbnail(game_dir, 'BB5')
    assert first
    assert mapart.gamemode_thumbnail(game_dir, 'BB5') == first
    mapart._load.cache_clear()
    assert mapart.gamemode_thumbnail(game_dir, 'BB5') == first
    assert len(reads) == 1
    pak.write_bytes(b'game update')
    assert mapart.gamemode_thumbnail(game_dir, 'BB5') == first
    assert len(reads) == 2
