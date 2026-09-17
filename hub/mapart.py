"""Read-only menu thumbnails from the player's own Bodycam installation.

No game artwork ships with the hub. Names below were verified against each map's
PDA_LevelMetaData ThumbnailImage (not LoadingScreenImage), 2026-09-16.
Unsupported game builds simply retain the UI's neutral placeholder.
"""
import hashlib
import io
import logging
import re
import struct
import threading
from functools import lru_cache
from pathlib import Path

from PIL import Image

from . import paths

TEXTURES = {
    'Airsoft': 'HighresScreenshot00321',
    'BombHouse': 'HighresScreenshot00299',
    'Hospital': 'HighresScreenshot00018',
    'Paintball': 'HighresScreenshot00441',
    'Pool': 'HighresScreenshot00160',
    'Rome': 'HighresScreenshot00276',
    'Russian': 'RussianBuildingScreenShots__1_',
    'CQB': 'HighresScreenshot00026',
    'Backrooms': 'HighresScreenshot00084',
    'Trenches': 'T_UI_Map_Trenches',
}
# Stock mode-card thumbnails, verified in the installed game on 2026-09-17.
# Capture the Flag deliberately uses Team Deathmatch artwork (Sam's choice).
GAMEMODE_TEXTURES = {
    'BB5': 'T_UI_BannerBodybomb',
    'CTF': 'T_UI_BannerTeamDeathmatch',
}
_lock = threading.Lock()
_log = logging.getLogger(__name__)


def decode_texture(data: bytes) -> bytes:
    """Decode the validated UE5 inline, single-mip BC1/BC3 UI texture layout.

    Deliberately reject streaming/multi-mip/new layouts instead of guessing offsets.
    Pillow already ships with the hub and includes the BCn decoder.
    """
    match = re.search(rb'PF_DXT[15]\x00', data[:256])
    if not match or match.start() < 16:
        raise ValueError('unsupported map texture format')
    width, height, depth, length = struct.unpack_from('<4i', data, match.start() - 16)
    if not (0 < width <= 4096 and 0 < height <= 4096 and depth == 1 and length == 8):
        raise ValueError('invalid map texture dimensions')
    start = match.end() + 12
    if data[match.end():start] != struct.pack('<3i', 0, 1, 0):
        raise ValueError('unsupported map texture mip layout')
    bc = 1 if match.group() == b'PF_DXT1\0' else 3
    size = ((width + 3) // 4) * ((height + 3) // 4) * (8 if bc == 1 else 16)
    trailer = struct.pack('<6iI', width, height, 1, 0, 0, 0, 0x9e2a83c1)
    if len(data) != start + size + len(trailer) or data[start + size:] != trailer:
        raise ValueError('unsupported or incomplete map texture payload')
    image = Image.frombytes('RGBA', (width, height), data[start:start + size], 'bcn', bc)
    image.thumbnail((640, 320), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    image.convert('RGB').save(output, 'JPEG', quality=86)
    return output.getvalue()


@lru_cache(maxsize=40)
def _load(pak_path, modified, size, texture, folder='Maps'):
    # The pak fingerprint invalidates both successes and failures after a game update.
    cache_texture = texture if folder == 'Maps' else f'{folder}/{texture}'
    key = hashlib.sha256(f'v1:{pak_path}:{modified}:{size}:{cache_texture}'.encode()).hexdigest()
    try:
        cache = paths.state_dir() / 'map-thumbnails'
        target = cache / (key + '.jpg')
        if target.is_file():
            try:
                data = target.read_bytes()
                with Image.open(io.BytesIO(data)) as image:
                    image.verify()
                return data
            except (OSError, ValueError):
                pass  # a damaged cache is regenerated from the installed texture
        paths.ensure_builder_on_path()
        from pakfile import PakFile
        pak = PakFile(pak_path)
        try:
            data = decode_texture(pak.read(f'UI/Textures/{folder}/{texture}.uexp'))
        finally:
            pak.f.close()
        try:
            cache.mkdir(parents=True, exist_ok=True)
            temp = target.with_suffix('.tmp')
            temp.write_bytes(data)
            temp.replace(target)
        except OSError:
            pass  # the in-memory cache still works on a read-only/full state volume
        return data
    except Exception as exc:  # optional artwork must never break history or captain controls
        _log.debug('Menu thumbnail unavailable for %s: %s', texture, exc)
        return None


def thumbnail(game_dir, map_name):
    """JPEG bytes or None. Only known map keys can request an installed texture."""
    return _thumbnail(game_dir, TEXTURES.get(map_name), 'Maps')


def gamemode_thumbnail(game_dir, mode_id):
    """JPEG bytes or None, selected only from the two approved mode banners."""
    return _thumbnail(game_dir, GAMEMODE_TEXTURES.get(mode_id), 'Banner')


def _thumbnail(game_dir, texture, folder):
    if not game_dir or not texture:
        return None
    pak = Path(game_dir) / 'Bodycam' / 'Content' / 'Paks' / 'pakchunk24-Windows.pak'
    try:
        stat = pak.stat()
        with _lock:
            return _load(str(pak.resolve()), stat.st_mtime_ns, stat.st_size, texture, folder)
    except OSError:
        return None
