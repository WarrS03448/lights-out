"""The native Linux client's update-trust rules, for publish.py (docs/linux-release-runbook.md).

publish.py must refuse any catalogue.linux.update record that an installed Linux client would
refuse, because such a record deploys and verifies green while every client silently answers
"untrusted" and never offers the update. So the rules are not re-described here: every definition
in the "copied" section below is the client's hub/update_trust_linux.py (branch
claude/pub-updater-core, as of 8081ed4), statement for statement, except PINNED_KEYS.

client_rules_problem() holds that promise at release time: publish.py compares the copied section,
definition by definition (ast, docstrings ignored), with the update_trust_linux.py inside the player
ZIP it publishes and inside the ZIP installed clients came from. When the client's rules change,
copy its definitions here again.

This file's own section:

  offer_manifest()        check_offer without the "newer than the running version" step.
  pinned_keys(source)     PINNED_KEYS read from a client's update_trust_linux.py with ast. A ZIP's
                          code is never imported or run to learn which keys it trusts.
  client_rules_problem()  the comparison above.

This module never signs anything.
"""
import ast
import base64
import binascii
import copy
import hashlib
import json
import re

# ---------------------------------------------------------------- copied from hub/update_trust_linux.py
DOMAIN = b'lights-out/linux-update/v1\0'
MAX_MANIFEST = 16 * 1024
MAX_ARCHIVE = 100_000_000
MAX_SOURCE = 1_000_000_000

FIXED = {'schema': 1, 'product': 'Lights Out', 'role': 'linux-native-update', 'platform': 'linux',
         'arch': 'x86_64', 'channel': 'public-beta'}
KEYS = frozenset(FIXED) | {'version', 'display_version', 'release', 'folder', 'archive', 'source',
                           'bundle_sha256', 'payload_sha256', 'launcher_sha256', 'installed_bytes'}
FOLDER = 'LightsOut-Linux-Native-{release}'
VERSION = re.compile(r'(0|[1-9][0-9]{0,4})(?:\.(0|[1-9][0-9]{0,4})){2,3}')
HEX = re.compile(r'[0-9a-f]{64}')


class UpdateRefused(ValueError):
    """reason: 'untrusted' (no verified offer) or 'not_newer' (verified, not an update)."""
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


_P = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _add(a, b):
    x1, y1, z1, t1 = a
    x2, y2, z2, t2 = b
    e1, f1 = (y1 - x1) * (y2 - x2) % _P, (y1 + x1) * (y2 + x2) % _P
    c, d = 2 * t1 * t2 * _D % _P, 2 * z1 * z2 % _P
    e, f, g, h = f1 - e1, d - c, d + c, f1 + e1
    return e * f % _P, g * h % _P, f * g % _P, e * h % _P


def _multiply(scalar, point):
    result = (0, 1, 1, 0)
    while scalar > 0:
        if scalar & 1: result = _add(result, point)
        point = _add(point, point)
        scalar >>= 1
    return result


def _same(a, b):
    return ((a[0] * b[2] - b[0] * a[2]) % _P == 0 and (a[1] * b[2] - b[1] * a[2]) % _P == 0)


def _recover_x(y, sign):
    if y >= _P: return None                   # a non-canonical y
    square = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if square == 0:
        return None if sign else 0            # "negative zero" is not an encoding
    x = pow(square, (_P + 3) // 8, _P)
    if (x * x - square) % _P: x = x * _SQRT_M1 % _P
    if (x * x - square) % _P: return None
    return _P - x if (x & 1) != sign else x


def _decode(raw):
    """A point from its 32-byte encoding, or None for anything that is not a canonical one."""
    if type(raw) is not bytes or len(raw) != 32: return None
    y = int.from_bytes(raw, 'little')
    sign, y = y >> 255, y & ((1 << 255) - 1)
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % _P)


def _encode(point):
    inverse = pow(point[2], _P - 2, _P)
    x, y = point[0] * inverse % _P, point[1] * inverse % _P
    return (y | ((x & 1) << 255)).to_bytes(32, 'little')


_BASE_Y = 4 * pow(5, _P - 2, _P) % _P
_BASE_X = _recover_x(_BASE_Y, 0)
_BASE = (_BASE_X, _BASE_Y, 1, _BASE_X * _BASE_Y % _P)


def _challenge(*parts):
    return int.from_bytes(hashlib.sha512(b''.join(parts)).digest(), 'little') % _L


def verify(public, message, signature):
    """RFC 8032 Ed25519 verification, strict and cofactorless. Never raises for bad input."""
    if type(signature) is not bytes or len(signature) != 64 or type(message) is not bytes: return False
    key, commitment = _decode(public), _decode(signature[:32])
    if key is None or commitment is None: return False
    s = int.from_bytes(signature[32:], 'little')
    if s >= _L: return False
    k = _challenge(signature[:32], public, message)
    return _same(_multiply(s, _BASE), _add(commitment, _multiply(k, key)))


def key_id(public):
    """The pinned name of a public key: the first 16 hex digits of its SHA-256."""
    if type(public) is not bytes or len(public) != 32: raise ValueError('An Ed25519 public key is 32 bytes')
    return hashlib.sha256(public).hexdigest()[:16]


def canonical(value):
    """The only byte form a manifest is signed and accepted in."""
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')


def _unique(pairs):
    value = {}
    for name, item in pairs:
        if name in value: raise ValueError('Duplicate manifest key')
        value[name] = item
    return value


def _refuse(text): raise ValueError('Unsupported JSON number ' + text[:16])


def _base64(text, limit):
    # Bounded before decoding; validate refuses anything outside the alphabet (and whitespace).
    if type(text) is not str or len(text) > 4 * ((limit + 2) // 3): raise UpdateRefused('untrusted')
    try: raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError): raise UpdateRefused('untrusted') from None
    # One encoding per byte string: no stray padding bits, no missing padding.
    if len(raw) > limit or base64.b64encode(raw).decode('ascii') != text: raise UpdateRefused('untrusted')
    return raw


def _integer(value, low, high):
    return type(value) is int and low <= value <= high


def _file(value, name, maximum):
    return (type(value) is dict and set(value) == {'name', 'size', 'sha256'} and value['name'] == name
            and _integer(value['size'], 1, maximum) and type(value['sha256']) is str
            and HEX.fullmatch(value['sha256']) is not None)


def version_parts(text):
    """'X.Y.Z' or 'X.Y.Z.N' -> four ints (each at most 65535); None for anything else."""
    if type(text) is not str or VERSION.fullmatch(text) is None: return None
    parts = [int(part) for part in text.split('.')]
    if any(part > 65535 for part in parts): return None
    return tuple(parts + [0] * (4 - len(parts)))


def parse_manifest(raw):
    """The manifest dict from its canonical bytes; UpdateRefused('untrusted') for anything else."""
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_MANIFEST: raise UpdateRefused('untrusted')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique, parse_float=_refuse,
                           parse_constant=_refuse)
    except (UnicodeError, ValueError, RecursionError):
        raise UpdateRefused('untrusted') from None
    if type(value) is not dict or set(value) != KEYS or canonical(value) != raw:
        raise UpdateRefused('untrusted')
    release = value['release']
    parts = version_parts(value['display_version'])
    folder = FOLDER.format(release=release) if type(release) is str else None
    if (any(type(value[name]) is not type(fixed) or value[name] != fixed for name, fixed in FIXED.items())
            or parts is None or release != value['display_version']
            or type(value['version']) is not list or len(value['version']) != 4
            or any(not _integer(part, 0, 65535) for part in value['version'])
            or tuple(value['version']) != parts or value['folder'] != folder
            or not _file(value['archive'], folder + '-x86_64.zip', MAX_ARCHIVE)
            or not _file(value['source'], folder + '-source.zip', MAX_SOURCE)
            or any(type(value[name]) is not str or HEX.fullmatch(value[name]) is None
                   for name in ('bundle_sha256', 'payload_sha256', 'launcher_sha256'))
            or not _integer(value['installed_bytes'], 1, MAX_ARCHIVE)):
        raise UpdateRefused('untrusted')
    return value


def verified_manifest(update, pinned):
    """The manifest of a {key_id, manifest, signature} record signed by a key in `pinned`.

    Sizes and encodings are checked before any signature work, and the JSON is parsed only
    after the signature verified. Every refusal is UpdateRefused('untrusted')."""
    if type(update) is not dict or set(update) != {'key_id', 'manifest', 'signature'}:
        raise UpdateRefused('untrusted')
    name = update['key_id']
    if type(name) is not str: raise UpdateRefused('untrusted')      # a pinned name is key_id(key)
    raw = _base64(update['manifest'], MAX_MANIFEST)
    signature = _base64(update['signature'], 64)
    public = pinned.get(name) if type(pinned) is dict else None
    if (type(public) is not bytes or len(public) != 32 or key_id(public) != name
            or not verify(public, DOMAIN + raw, signature)):
        raise UpdateRefused('untrusted')
    return parse_manifest(raw)


def check_offer(entry, current):
    """The verified manifest when catalogue["linux"] offers a signed release newer than `current`
    (a HUB_VERSION string), else UpdateRefused. The catalogue's own version, size and SHA-256 are
    only hints: they must equal what the pinned key signed."""
    if (type(entry) is not dict or entry.get('kind') != 'linux-native-zip' or entry.get('arch') != 'x86_64'):
        raise UpdateRefused('untrusted')
    manifest = verified_manifest(entry.get('update'), PINNED_KEYS)
    archive = manifest['archive']
    if (type(entry.get('version')) is not str or entry['version'] != manifest['display_version']
            or type(entry.get('size')) is not int or entry['size'] != archive['size']
            or type(entry.get('sha256')) is not str or entry['sha256'] != archive['sha256']):
        raise UpdateRefused('untrusted')
    from .version import version_tuple
    if tuple(manifest['version']) <= version_tuple(current):
        raise UpdateRefused('not_newer')
    return manifest


# check_offer is kept only so the copy is the client's whole rule set; publish.py never calls it (it
# reads each ZIP's own PINNED_KEYS instead, and offer_manifest below is check_offer without its last
# step). This table is therefore always empty here.
PINNED_KEYS = {}


# ---------------------------------------------------------------- this file's own
def offer_manifest(entry, pinned):
    """The client's check_offer(entry, current) without its last step: the verified manifest when a
    client pinning `pinned` would trust catalogue["linux"] = entry, else UpdateRefused('untrusted').
    Whether the offer is newer than an installed version is publish.py's own version check."""
    if (type(entry) is not dict or entry.get('kind') != 'linux-native-zip' or entry.get('arch') != 'x86_64'):
        raise UpdateRefused('untrusted')
    manifest = verified_manifest(entry.get('update'), pinned)
    archive = manifest['archive']
    if (type(entry.get('version')) is not str or entry['version'] != manifest['display_version']
            or type(entry.get('size')) is not int or entry['size'] != archive['size']
            or type(entry.get('sha256')) is not str or entry['sha256'] != archive['sha256']):
        raise UpdateRefused('untrusted')
    return manifest


def pinned_keys(source):
    """PINNED_KEYS from the text of a client's hub/update_trust_linux.py, without running it: one
    top-level `PINNED_KEYS = {...}` whose entries are 'key_id': bytes.fromhex('<64 hex>') (or a bytes
    literal), each key_id naming its own key. ValueError for anything else."""
    found = [node.value for node in ast.parse(source).body if isinstance(node, ast.Assign)
             and any(isinstance(target, ast.Name) and target.id == 'PINNED_KEYS' for target in node.targets)]
    if len(found) != 1:
        raise ValueError('PINNED_KEYS must be assigned exactly once')
    table = found[0]
    if not isinstance(table, ast.Dict) or None in table.keys:
        raise ValueError('PINNED_KEYS is not a plain dict')
    keys = {}
    for name, value in zip(table.keys, table.values):
        if not (isinstance(name, ast.Constant) and type(name.value) is str):
            raise ValueError('a pinned key_id is not a string')
        if isinstance(value, ast.Constant) and type(value.value) is bytes:
            public = value.value
        elif (isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute) and value.func.attr == 'fromhex'
              and isinstance(value.func.value, ast.Name) and value.func.value.id == 'bytes'
              and len(value.args) == 1 and not value.keywords and isinstance(value.args[0], ast.Constant)
              and type(value.args[0].value) is str):
            public = bytes.fromhex(value.args[0].value)
        else:
            raise ValueError('a pinned public key is not bytes.fromhex(...) of a literal')
        if len(public) != 32 or key_id(public) != name.value or name.value in keys:
            raise ValueError(f'the pinned key_id {name.value!r} does not name its key')
        keys[name.value] = public
    return keys


def _definitions(source, before_line=None):
    """{name: ast.dump} of a module's top-level assignments, functions and classes (docstrings left
    out), without PINNED_KEYS; only those starting before `before_line` when it is given."""
    out = {}
    for node in ast.parse(source).body:
        if before_line is not None and node.lineno >= before_line:
            break
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                body = body[1:]
            node = copy.copy(node)
            node.body = body
            out[node.name] = ast.dump(node)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out[target.id] = ast.dump(node.value)
    out.pop('PINNED_KEYS', None)
    return out


def _copied():
    with open(__file__, encoding='utf-8') as stream:
        source = stream.read()
    end = next(number for number, line in enumerate(source.splitlines(), 1)
               if line.startswith('# ---') and line.endswith(" this file's own"))
    return _definitions(source, before_line=end)


COPIED = _copied()


def client_rules_problem(source):
    """None when a client's update_trust_linux.py (its source text) has exactly the definitions in the
    copied section above (PINNED_KEYS aside); otherwise which names differ."""
    try:
        theirs = _definitions(source)
    except (SyntaxError, ValueError) as error:
        return f'it does not parse ({error})'
    changed = sorted(name for name in set(COPIED) | set(theirs) if COPIED.get(name) != theirs.get(name))
    if not changed:
        return None
    return f"its {', '.join(changed[:8])}{' and more' if len(changed) > 8 else ''} differ from tools/release/linux_update_trust.py"
