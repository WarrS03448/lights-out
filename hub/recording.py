"""Recording install journal; no game writes occur on import."""
import hashlib
import json
import os
import shutil
import threading
import uuid
from pathlib import Path
from . import game, paths, version

NAMES = ('CommunityGamemodes_P.pak', 'CommunityLobby_P.pak')
_lock = threading.RLock()

def enabled():
    return bool(getattr(version, 'RECORDING', False))

def _sha(path):
    if not path.is_file():
        return None
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def _write(path, data):
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with tmp.open('w', encoding='utf-8') as stream:
        json.dump(data, stream, indent=2)
        stream.flush(); os.fsync(stream.fileno())
    os.replace(tmp, path)

def _copy(src, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    with src.open('rb') as source, dest.open('wb') as target:
        shutil.copyfileobj(source, target)
        target.flush(); os.fsync(target.fileno())
    if _sha(src) != _sha(dest):
        raise RuntimeError('Recording backup copy did not verify')

def _journal():
    return paths.state_dir()/'recording-active.json'

def _load():
    path = _journal()
    if not path.exists():
        return None
    with path.open(encoding='utf-8') as stream:
        row = json.load(stream)
    if set(row.get('files', {})) != set(NAMES) or not isinstance(row.get('game'), str):
        raise RuntimeError('Invalid recording backup journal; originals were preserved')
    folder = (paths.state_dir()/'recording-backups'/row['id']).resolve()
    if folder.parent != (paths.state_dir()/'recording-backups').resolve():
        raise RuntimeError('Invalid recording backup folder')
    return row

def before_write(game_dir, name, source):
    """Commit original backups and possible next hash BEFORE a managed game-file mutation."""
    if not enabled():
        return
    if name not in NAMES:
        raise RuntimeError('Unmanaged recording file')
    with _lock:
        if game.game_running():
            raise RuntimeError('Close Bodycam before switching recording files')
        root = str(Path(game_dir).resolve())
        if not game.is_game_dir(root):
            raise RuntimeError('Invalid Bodycam installation')
        row = _load()
        if row and row['game'] != root:
            raise RuntimeError('Restore the previous recording installation first')
        mods = Path(game.mods_dir(root, create=False))
        if not row:
            row = {'id':uuid.uuid4().hex, 'game':root, 'files':{}}
            folder = paths.state_dir()/'recording-backups'/row['id']
            folder.mkdir(parents=True, exist_ok=False)
            for item in NAMES:
                current = mods/item
                digest = _sha(current)
                if current.is_symlink():
                    raise RuntimeError('Recording cannot replace a linked game pak')
                if digest:
                    _copy(current,folder/(item+'.original'))
                row['files'][item] = {'original':digest, 'accepted':[digest]}
        for item, record in row['files'].items():
            if _sha(mods/item) not in record['accepted']:
                raise RuntimeError('Game pak changed outside this recording session; originals preserved')
        digest = _sha(Path(source)) if source is not None else None
        if source is not None and digest is None:
            raise RuntimeError('Missing replacement recording pak')
        accepted = row['files'][name]['accepted']
        if digest not in accepted:
            accepted.append(digest)
        _write(_journal(),row)

def restore():
    """Restore only verified originals; unexpected edits cause a refusal, never data loss."""
    if not enabled():
        return
    with _lock:
        row = _load()
        if not row:
            return
        if game.game_running():
            raise RuntimeError('Close Bodycam before restoring the public setup')
        mods = Path(game.mods_dir(row['game'], create=False))
        folder = paths.state_dir()/'recording-backups'/row['id']
        for name, record in row['files'].items():
            if record['original'] and _sha(folder/(name+'.original')) != record['original']:
                raise RuntimeError('Recording backup is damaged; no game files changed')
            if (mods/name).is_symlink() or _sha(mods/name) not in record['accepted']:
                raise RuntimeError('Game pak changed outside this recording session; originals preserved')
        for name, record in row['files'].items():
            target = mods/name
            if _sha(target) == record['original']:
                continue
            if target.exists() and name != 'CommunityLobby_P.pak':
                _copy(target,folder/(name+'.recording'))
            if record['original']:
                tmp = target.with_name(target.name+'.recording-restore.tmp')
                _copy(folder/(name+'.original'),tmp)
                os.replace(tmp,target)
            elif target.exists():
                # This is a file created by this recording session, with no public original.
                # Its bytes are already copied and hash-verified in the durable archive above.
                target.unlink()
        if any(_sha(mods/name) != record['original'] for name,record in row['files'].items()):
            raise RuntimeError('Public setup restore did not verify; backup journal kept')
        from . import state
        state.update_fields({'installed':{}, 'pak_sha256':None})
        os.replace(_journal(),folder/'restored.json')

def resume_or_restore():
    """An active game must be rejoined; only restore on startup when it has stopped."""
    if not enabled():
        return
    with _lock:
        row=_load()
        if not row:
            return
        if not game.game_running():
            restore()
            return
        mods=Path(game.mods_dir(row['game'],create=False))
        folder=paths.state_dir()/'recording-backups'/row['id']
        for name,record in row['files'].items():
            if record['original'] and _sha(folder/(name+'.original')) != record['original']:
                raise RuntimeError('Recording backup is damaged; originals preserved')
            if (mods/name).is_symlink() or _sha(mods/name) not in record['accepted']:
                raise RuntimeError('Game pak changed outside this recording session; originals preserved')

def ensure_start():
    try:
        resume_or_restore()
        return True
    except Exception as exc:
        notify('Recording recovery needs attention',str(exc))
        return False

def ensure_exit():
    if not enabled():
        return True
    try:
        restore()
        return True
    except Exception as exc:
        notify('Recording setup needs attention',str(exc)+'\n\nUse Restore Public Lights Out after closing Bodycam. Your original files are backed up.')
        return False

def notify(title,text):
    if os.name == 'nt':
        import ctypes
        ctypes.windll.user32.MessageBoxW(None,str(text),str(title),0x40)
    else:
        print(title+': '+text)
