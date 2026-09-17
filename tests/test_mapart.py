"""UTF-8. Local map artwork decoding and safe fallback checks."""
import io
import struct

import pytest
from PIL import Image

from hub import mapart


def texture(bc=1):
    # One red 4x4 block in the installed game's inline single-mip layout.
    pixel_format = b'PF_DXT1\0' if bc == 1 else b'PF_DXT5\0'
    alpha = b'' if bc == 1 else b'\xff\xff' + b'\0' * 6
    return (b'\0' * 77 + struct.pack('<4i', 4, 4, 1, 8) + pixel_format
            + struct.pack('<3i', 0, 1, 0) + alpha + struct.pack('<HHI', 0xf800, 0, 0)
            + struct.pack('<6iI', 4, 4, 1, 0, 0, 0, 0x9e2a83c1))


@pytest.mark.parametrize('bc', [1, 3])
def test_decodes_real_pixel_format(bc):
    image = Image.open(io.BytesIO(mapart.decode_texture(texture(bc))))
    assert image.size == (4, 4)
    r, g, b = image.convert('RGB').getpixel((0, 0))
    assert r > 240 and g < 15 and b < 15


@pytest.mark.parametrize('data', [b'', texture()[:-1], texture().replace(b'PF_DXT1', b'PF_BC99'),
                                      texture().replace(struct.pack('<i', 4), struct.pack('<i', 99999), 1)])
def test_rejects_unsupported_or_damaged_layout(data):
    with pytest.raises(ValueError):
        mapart.decode_texture(data)


def test_no_game_or_unknown_map_is_harmless(tmp_path):
    assert mapart.thumbnail(None, 'Hospital') is None
    assert mapart.thumbnail(tmp_path, 'Hospital') is None
    assert mapart.thumbnail(tmp_path, '../../state.json') is None


def test_current_ranked_pool_has_art():
    from hub.competitive import DEFAULT_MAPS
    assert set(DEFAULT_MAPS) <= mapart.TEXTURES.keys()


def test_cached_art_recovers_and_refreshes_after_game_update(tmp_path, monkeypatch):
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
    first = mapart.thumbnail(tmp_path, 'Hospital')
    assert first
    assert mapart.thumbnail(tmp_path, 'Hospital') == first
    assert len(reads) == 1
    mapart._load.cache_clear()
    assert mapart.thumbnail(tmp_path, 'Hospital') == first
    assert len(reads) == 1  # disk cache survives restarting the hub
    next((tmp_path / 'state/map-thumbnails').glob('*.jpg')).write_bytes(b'broken')
    mapart._load.cache_clear()
    assert mapart.thumbnail(tmp_path, 'Hospital') == first
    assert len(reads) == 2
    pak.write_bytes(b'updated game pak')
    assert mapart.thumbnail(tmp_path, 'Hospital') == first
    assert len(reads) == 3
    mapart._load.cache_clear()
