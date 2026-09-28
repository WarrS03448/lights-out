#!/usr/bin/env python3
"""publish.py — the ONE way changes reach live Lights Out (Sam, 2026-09-14: "any updates we make get
published to the actual hub that's downloadable and we also use that hub for testing").

    publish.py hub  [--bump patch|minor|major | --version X.Y.Z | --same-version] [--no-deploy]
        bump hub/version.py, run the tests, build the onedir app (PyInstaller -> dist/LightsOut/LightsOut.exe),
        wrap it with Inno Setup (ISCC hub/installer.iss -> dist/LightsOut-Setup-<v>.exe), put the installer in
        server/public/hub/, write hub.version / download_url / sha256 / size / kind="inno-setup" into the catalogue,
        deploy, verify. (Installed hubs then update silently on their next start; hubs older than 1.1.0 download the
        installer and open its wizard once.) Needs Inno Setup 6: winget install -e --id JRSoftware.InnoSetup
    publish.py pack <ID> [--bump patch|minor|major | --version X.Y.Z] [--cooked DIR] [--no-deploy]
        re-pack gamemode <ID> from its cook (default mirror/cooked) at the manifest's version (bumped if asked),
        put the zip in server/public/packs/, write its catalogue entry, deploy, verify.
        (Installed older versions show "update available" in the hub.)
    publish.py linux --dir DIR [--no-deploy] [--required] [--source-url URL] [--allow-behind-windows]
                     [--break-in-app-updates]
        publish a native Linux beta build made on the Linux VM (docs/linux-release-runbook.md). DIR holds the
        player ZIP, the source ZIP, build_bundle's report and sign_update's signed update record for ONE
        version; every hash, size and version in them must agree, and the record must pass the Linux
        client's own checks under the keys pinned in the new ZIP AND in the published one (the keys
        installed clients hold), before anything is written. Copies the ZIPs into server/public/hub/,
        writes the top-level catalogue.linux entry, deploys, verifies. Deploying pushes the current branch,
        so without --no-deploy it must run on main. It never builds or signs anything and never touches
        hub/version.py or catalogue.hub (the Windows release).
    publish.py deploy        commit the release artefacts and push to GitHub (Railway builds from the repo),
                             then verify.
    publish.py verify        the live catalogue must equal server/public/catalogue.json, every pack_url, the
                             hub download_url and (when published) the Linux ZIP and source ZIP must answer
                             with the right size and sha256, the live Linux update record must pass the
                             client's checks under the keys the served Linux ZIP pins, /api/health must be OK.
    publish.py status        local vs live versions (hub, packs and Linux).
    publish.py proxy-endpoints --version X.Y.Z
        one-time, disassembly-verified migration of the shipped BB5/lobby URL constants.
        Produces local release artifacts only; build the signed hub and deploy afterward.
    --push                   accepted and ignored. `deploy` IS the push now; --push used to `git add -A`,
                             which would sweep another session's in-flight work into a release commit.

Nothing here is edited by hand any more: the catalogue, the installer in public/hub and the zips in public/packs
are all written by this script. Double-click wrappers: release\\*.bat.
"""
import argparse
import ast
import base64
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit
from pathlib import Path
import zipfile
import zlib

import linux_update_trust as trust              # the Linux client's own update-trust rules (tools/release)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
SERVER = os.path.join(ROOT, "server")
PUBLIC = os.path.join(SERVER, "public")
CATALOGUE = os.path.join(PUBLIC, "catalogue.json")
VERSION_PY = os.path.join(ROOT, "hub", "version.py")
WIN = os.name == "nt"
PUBLIC_ORIGIN = 'https://play.lightsoutranked.com'
WEBSITE_ORIGIN = 'https://lightsoutranked.com'
OWNED_ORIGINS = {PUBLIC_ORIGIN, WEBSITE_ORIGIN, 'https://www.lightsoutranked.com',
                 'https://lightsout.up.railway.app'}


# ---------------------------------------------------------------- small helpers
def say(msg=""):
    print(msg, flush=True)


def die(msg):
    say(f"\nPUBLISH FAILED: {msg}")
    sys.exit(1)


def venv_python():
    p = os.path.join(ROOT, ".venv", "Scripts" if WIN else "bin", "python.exe" if WIN else "python")
    return p if os.path.exists(p) else sys.executable


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def load_catalogue():
    with open(CATALOGUE, encoding="utf-8") as f:
        return json.load(f)


def save_catalogue(c):
    def owned_url(value, target):
        if not isinstance(value, str):
            return value
        parts = urlsplit(value)
        if parts.scheme + '://' + parts.netloc in OWNED_ORIGINS:
            return target + urlunsplit(('', '', parts.path, parts.query, parts.fragment))
        return value
    hub = c.get('hub', {})
    if 'download_url' in hub:
        hub['download_url'] = owned_url(hub['download_url'], PUBLIC_ORIGIN)
    if 'page_url' in hub:
        hub['page_url'] = owned_url(hub['page_url'], WEBSITE_ORIGIN)
    linux = c.get('linux')
    if isinstance(linux, dict):
        for field in ('download_url', 'source_url'):
            if field in linux:
                linux[field] = owned_url(linux[field], PUBLIC_ORIGIN)
        if 'page_url' in linux:
            linux['page_url'] = owned_url(linux['page_url'], WEBSITE_ORIGIN)
    for mode in c.get('gamemodes', []):
        if 'pack_url' in mode:
            mode['pack_url'] = owned_url(mode['pack_url'], PUBLIC_ORIGIN)
    with open(CATALOGUE, "w", encoding="utf-8") as f:
        json.dump(c, f, indent=2, ensure_ascii=False)
        f.write("\n")


def origin():
    """Stable owned connection origin, independent of old published download URLs."""
    return PUBLIC_ORIGIN


def bump_version(v, how):
    parts = [int(x) for x in v.split(".")]
    while len(parts) < 3:
        parts.append(0)
    i = {"major": 0, "minor": 1, "patch": 2}[how]
    parts[i] += 1
    for j in range(i + 1, 3):
        parts[j] = 0
    return ".".join(str(x) for x in parts)


def version_newer(a, b):
    def parts(v):
        return [int(x) if x.isdigit() else -1 for x in str(v).split(".")]
    pa, pb = parts(a), parts(b)
    n = max(len(pa), len(pb))
    pa += [0] * (n - len(pa)); pb += [0] * (n - len(pb))
    return pa > pb


def run(cmd, cwd=None, log=None, check=True):
    say("  $ " + " ".join(cmd))
    if log:
        with open(log, "w", encoding="utf-8", errors="replace") as f:
            r = subprocess.run(cmd, cwd=cwd, stdout=f, stderr=subprocess.STDOUT)
    else:
        r = subprocess.run(cmd, cwd=cwd)
    if check and r.returncode != 0:
        die(f"command failed ({r.returncode}): {' '.join(cmd)}" + (f" — see {log}" if log else ""))
    return r.returncode


def http(url, method="GET", timeout=30):
    req = urllib.request.Request(url, method=method, headers={"User-Agent": "LightsOut-publish/1", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, dict(resp.headers), (resp.read() if method == "GET" else b"")


# ---------------------------------------------------------------- hub
def read_hub_version():
    s = open(VERSION_PY, encoding="utf-8").read()
    m = re.search(r'^HUB_VERSION\s*=\s*"([^"]+)"', s, re.M)
    if not m:
        die("HUB_VERSION not found in hub/version.py")
    return m.group(1)


def write_hub_version(v):
    s = open(VERSION_PY, encoding="utf-8").read()
    s2 = re.sub(r'^HUB_VERSION\s*=\s*"[^"]+"', f'HUB_VERSION = "{v}"', s, count=1, flags=re.M)
    if s2 == s:
        die("could not rewrite HUB_VERSION")
    open(VERSION_PY, "w", encoding="utf-8").write(s2)


DIST = os.path.join(ROOT, "dist")


def onedir_exe():
    """dist/LightsOut/LightsOut.exe — the PyInstaller --onedir output (hub/hub.spec). Windows-only, like the
    rest of the hub release (find_iscc() refuses to run anywhere else)."""
    return os.path.join(DIST, "LightsOut", "LightsOut.exe")


def installer_path(version):
    """dist/LightsOut-Setup-<v>.exe — what hub/installer.iss produces (OutputBaseFilename); its basename is served."""
    return os.path.join(DIST, f"LightsOut-Setup-{version}.exe")


def check_version(v):
    if not re.fullmatch(r"\d+\.\d+\.\d+", str(v)):
        die(f"--version must be X.Y.Z (got {v!r}; Inno Setup's VersionInfoVersion is numeric)")
    return v


def find_iscc():
    """Inno Setup 6's command-line compiler (per-machine or per-user install)."""
    if not WIN:
        die("the hub installer is built with Inno Setup, which only runs on Windows — release from the Windows build PC")
    candidates = []
    for env in ("ProgramFiles(x86)", "ProgramFiles"):
        base = os.environ.get(env)
        if base:
            candidates.append(os.path.join(base, "Inno Setup 6", "ISCC.exe"))
    if os.environ.get("LOCALAPPDATA"):
        candidates.append(os.path.join(os.environ["LOCALAPPDATA"], "Programs", "Inno Setup 6", "ISCC.exe"))
    for c in candidates:
        if os.path.isfile(c):
            return c
    on_path = shutil.which("ISCC")
    if on_path:
        return on_path
    die("Inno Setup 6 (ISCC.exe) was not found — install it with: winget install -e --id JRSoftware.InnoSetup")


def _clear(path):
    """Delete a stale build output; if Windows keeps it locked, rename it out of the way (allowed for running exes);
    if even that fails, stop with a message instead of a traceback."""
    if not os.path.lexists(path):
        return
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
        return
    except OSError:
        pass
    try:
        os.replace(path, f"{path}.old.{int(time.time())}")
    except OSError as e:
        die(f"{os.path.relpath(path, ROOT)} is locked and could not be deleted or renamed ({e}) — close the hub and any "
            "Explorer window in dist\\ and try again")


def check_installer_signing_evidence(setup, directory):
    """Never release a signed internal component accepted by older update gates."""
    binaries = sorted(Path(directory).iterdir())
    if len(binaries) != 1 or binaries[0].name != Path(setup).name or not binaries[0].is_file():
        die("Inno signing evidence must contain only the final installer; internal signing is unsafe for older clients")
    if sha256_of(binaries[0]) != sha256_of(setup):
        die("The final installer differs from the verified signing input")


def build_hub(version, iscc):
    """PyInstaller --onedir -> self-check -> Inno Setup installer (compiled with `iscc`). Returns the installer path."""
    py = venv_python()
    signing = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
               "-File", os.path.join(ROOT, "tools", "release", "sign.ps1")]
    run(signing + ["-CheckConfiguration"], cwd=ROOT)
    say("Installing the build requirements…")
    run([py, "-m", "pip", "install", "-q", "-r", os.path.join("hub", "requirements.txt")], cwd=ROOT)
    say("Running the hub tests…")
    run([py, os.path.join("tests", "test_hub.py")], cwd=ROOT)
    say("Building the hub folder (PyInstaller --onedir)…")
    # _clear can rename a locked build output. Never terminate installed hubs:
    # a developer may be playing a competitive match while another build runs.
    exe = onedir_exe()
    setup = installer_path(version)
    stale = {os.path.dirname(exe), setup}
    if os.path.isdir(DIST):                             # old one-file builds and old installers must not accumulate
        stale.update(os.path.join(DIST, n) for n in os.listdir(DIST)
                     if n.lower().startswith("communityhub-") and n.lower().endswith(".exe"))
    for path in sorted(stale):
        _clear(path)
    run([py, "-m", "PyInstaller", "--noconfirm", "--clean", os.path.join("hub", "hub.spec")], cwd=ROOT,
        log=os.path.join(ROOT, "hub", "build.log"))
    if not os.path.isfile(exe):
        die(f"the build did not produce {exe} — see hub/build.log")
    say("Signing and verifying the application…")
    run(signing + ["-Path", exe], cwd=ROOT)
    say("Self-check…")
    # The self-check runs the freshly built exe and verifies the frozen bundle is sound (the pak
    # builder + ooz import, AND the web UI screens register — a frozen-only regression that once
    # shipped a dead UI). Two outcomes are treated very differently:
    #   * The exe LAUNCHED but the check failed (rc != 0): a real defect in THIS build — FAIL the
    #     release. This is the gate that stops a broken frozen build from ever reaching a user.
    #   * The exe could not be LAUNCHED at all (OSError, e.g. Smart App Control WinError 4551): a
    #     property of the build machine, not the build — warn and continue.
    try:
        rc = run([exe, "--selfcheck", "--quiet", "--verify-signature"], cwd=ROOT, check=False)
        if rc != 0:
            die(f"self-check FAILED (exit {rc}) — the frozen build is broken; see "
                f"%LOCALAPPDATA%\\CommunityHub\\logs\\selfcheck.txt. NOT releasing.")
        say("  self-check OK")
    except OSError as e:
        say(f"  WARNING: could not run the self-check on this machine ({e}); the build exists — continuing.")
    say("Building the installer (Inno Setup)…")
    # Remove evidence/cache from earlier builds, including internally signed
    # candidates that must never reach older clients. Inno internal signing is off.
    signed_dir = os.path.join(DIST, "signed-uninstaller")
    _clear(signed_dir)
    evidence_dir = os.path.join(DIST, "signing-evidence")
    _clear(evidence_dir)
    os.makedirs(evidence_dir, exist_ok=True)
    # Inno's $f is already quoted. Escape literal dollars in the fixed command;
    # no user-controlled $p arguments are accepted by this named signing tool.
    callback = subprocess.list2cmdline(signing + ["-EvidenceDirectory", evidence_dir, "-Path"]).replace("$", "$$") + " $f"
    run([iscc, "/Qp", f"/DHubVersion={version}", f"/DHubFileVersion={check_version(version)}.0",
         "/DHubSignedRelease=1", "/Slightsout=" + callback,
         os.path.join("hub", "installer.iss")], cwd=ROOT,
        log=os.path.join(ROOT, "hub", "installer.log"))
    if not os.path.isfile(setup):
        die(f"Inno Setup did not produce {setup} — see hub/installer.log")
    say("Verifying the signed installer and compiler signing evidence…")
    check_installer_signing_evidence(setup, evidence_dir)
    run(signing + ["-VerifyOnly", "-Path", setup], cwd=ROOT)
    say(f"  {os.path.relpath(setup, ROOT)} ({os.path.getsize(setup)} B)")
    return setup


def publish_hub(args):
    old = read_hub_version()
    if args.version:
        new = check_version(args.version)
    elif args.bump:
        new = check_version(bump_version(old, args.bump))
    elif args.same_version:
        new = check_version(old)
    else:
        die("say how the hub version changes: --bump patch|minor|major, --version X.Y.Z, or --same-version "
            "(an unchanged version means no player is told to update)")
    iscc = find_iscc()                                  # before anything is written or built: no Inno, no release
    if new != old:
        write_hub_version(new)
        say(f"hub version {old} -> {new}")
    setup = build_hub(new, iscc)
    name = os.path.basename(setup)
    hub_dir = os.path.join(PUBLIC, "hub")
    os.makedirs(hub_dir, exist_ok=True)
    target = os.path.join(hub_dir, name)
    shutil.copy2(setup, target)
    for other in os.listdir(hub_dir):                     # only the current installer is served
        p = os.path.join(hub_dir, other)
        if p != target and other.lower().endswith(".exe"):
            os.remove(p)
            say(f"removed old {other}")
    c = load_catalogue()
    c.setdefault("hub", {})
    c["hub"]["version"] = new
    c["hub"]["download_url"] = f"{origin()}/hub/{name}"
    c["hub"].setdefault("page_url", origin() + "/")
    c["hub"]["sha256"] = sha256_of(target)
    c["hub"]["size"] = os.path.getsize(target)
    c["hub"]["kind"] = "inno-setup"                       # hub >= 1.1.0 runs it /VERYSILENT; 1.0.9 ignores this and opens the wizard
    save_catalogue(c)
    say(f"catalogue: hub {new}, {name} ({c['hub']['size']} B, sha256 {c['hub']['sha256'][:12]}…, kind inno-setup)")
    linux = c.get("linux")                               # never changed here; only reported
    lt = linux_version_tuple(linux.get("version")) if isinstance(linux, dict) else None
    ht = linux_version_tuple(new)
    if lt is not None and ht is not None and lt < ht:
        say(f"NOTE: the published Linux beta is {linux['version']}, older than this hub. Unless the per-platform "
            f"gate is live, Linux players cannot queue until a Linux build at {new} is published "
            "(docs/linux-release-runbook.md).")
    return new


# ---------------------------------------------------------------- linux (native beta)
# catalogue.linux is a TOP-LEVEL key and never lives inside "hub". Windows clients read catalogue.hub and
# ignore keys they do not know, so a Linux release cannot make a Windows client update, and a Windows
# release (publish_hub) cannot change the Linux entry. The entry is the contract that the Linux in-app
# updater, the website's /hub/download/linux routes and source_manifest.py all read:
#
#   {version, kind: "linux-native-zip", arch: "x86_64", download_url, size, sha256,
#    source_url, source_size, source_sha256, page_url, required, update: {key_id, manifest, signature}}
#
# Nothing is built or signed here. The ZIPs, build_bundle's report and sign_update's record come from the
# Linux VM (docs/linux-release-runbook.md); this only refuses a set that disagrees with itself, or whose
# update record an installed client would refuse (linux_update_trust.py holds the client's own checks and
# verifies the Ed25519 signature under the keys each ZIP pins), and then copies the bytes that were checked.
LINUX_PREFIX = "LightsOut-Linux-Native-"
LINUX_KIND = "linux-native-zip"
LINUX_ARCH = "x86_64"
LINUX_LIMIT = 100_000_000                       # the player download's hard limit (build_bundle.LIMIT)
LINUX_SOURCE_LIMIT = trust.MAX_SOURCE           # the largest source ZIP the client accepts in a signed record
LINUX_FILE_RE = re.compile(re.escape(LINUX_PREFIX) + r"(.+?)(-x86_64\.zip|-source\.zip|\.update\.json|\.json)")
LINUX_ROLES = {"-x86_64.zip": "player", "-source.zip": "source", ".json": "report", ".update.json": "update"}
LINUX_TRUST_MEMBER = "support/runtime/hub/update_trust_linux.py"   # the runtime's PINNED_KEYS
LINUX_KEY_ID_RE = re.compile(r"[0-9a-f]{16}")     # trust.key_id(): the only names a client pins
LINUX_REQUIRED_MEMBERS = ("support/start-setup", "support/native_setup.py", "support/bundle.json",
                          "support/native-launcher", "support/runtime/payload.json",
                          "support/runtime/hub/version.py")          # and LINUX_TRUST_MEMBER, read below
UPDATE_MANIFEST_KEYS = trust.KEYS
MAX_UPDATE_RECORD = 64 * 1024
MAX_UPDATE_MANIFEST = trust.MAX_MANIFEST        # the client refuses a larger signed manifest
PLATFORM_GATE = "hub_platform_gate_v1"          # server/live.cjs announces it once Linux is gated on its own
# Everything a damaged, truncated, encrypted or oddly compressed ZIP can raise while it is read.
ZIP_ERRORS = (zipfile.BadZipFile, zlib.error, OSError, EOFError, NotImplementedError, RuntimeError)


class Refused(Exception):
    """A Linux release that must not be published. publish_linux() turns it into die()."""


def linux_version_tuple(v):
    """X.Y.Z or X.Y.Z.N as four ints (X.Y.Z counts as X.Y.Z.0), each at most 65535, as the Linux client
    reads a version; None for anything else."""
    return trust.version_parts(v)


def _unique_keys(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key!r}")
        out[key] = value
    return out


def _no_float(text):
    raise ValueError(f"the number {text[:16]} is not an integer (the Linux client refuses floats)")


def _strict_json(raw, what, *, integers_only=False):
    """JSON with no duplicate keys; with integers_only, also no float, NaN or Infinity (the client's rule
    for the signed manifest)."""
    extra = dict(parse_float=_no_float, parse_constant=_no_float) if integers_only else {}
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_keys, **extra)
    except ValueError as e:                             # JSONDecodeError and UnicodeDecodeError are ValueErrors
        raise Refused(f"{what} is not valid JSON ({e})")


def _b64(value, what):
    if not isinstance(value, str):
        raise Refused(f"{what} is not a base64 string")
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError):
        raise Refused(f"{what} is not valid base64")


def _is_int(value):
    return type(value) is int


def _is_sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def linux_release_files(directory):
    """(version, {role: Path}) for the one release in DIR: exactly one player ZIP, source ZIP, report and
    update record, all naming the same version. Files without the LightsOut-Linux-Native- prefix are ignored."""
    folder = Path(directory)
    if not folder.is_dir():
        raise Refused(f"--dir {directory} is not a folder")
    found, versions = {}, set()
    for path in sorted(folder.iterdir()):
        if not path.name.startswith(LINUX_PREFIX):
            continue
        m = LINUX_FILE_RE.fullmatch(path.name)
        if not m:
            raise Refused(f"{path.name} is not one of the four Linux release files")
        if path.is_symlink() or not path.is_file():
            raise Refused(f"{path.name} is not a regular file")
        role = LINUX_ROLES[m.group(2)]
        if role in found:
            raise Refused(f"{folder} holds more than one {role} file: {found[role].name} and {path.name}")
        found[role] = path
        versions.add(m.group(1))
    missing = [role for role in ("player", "source", "report", "update") if role not in found]
    if missing:
        raise Refused(f"{folder} lacks the {', '.join(missing)} file(s); it needs {LINUX_PREFIX}<V>-x86_64.zip, "
                      f"{LINUX_PREFIX}<V>-source.zip, {LINUX_PREFIX}<V>.json and {LINUX_PREFIX}<V>.update.json")
    if len(versions) != 1:
        raise Refused("the release files name different versions: " + ", ".join(sorted(versions)))
    version = versions.pop()
    if linux_version_tuple(version) is None:
        raise Refused(f"version {version!r} is not X.Y.Z or X.Y.Z.N (each part at most 65535)")
    return version, found


def _hub_version_in(source):
    """HUB_VERSION from a hub/version.py, read without executing it."""
    try:
        tree = ast.parse(source.decode("utf-8"))
    except (SyntaxError, ValueError):
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "HUB_VERSION" for t in node.targets):
            try:
                value = ast.literal_eval(node.value)
            except ValueError:
                return None
            return value if isinstance(value, str) else None
    return None


def _check_members(path, archive, top=None):
    """Every member of a published ZIP is a plain file or folder at a plain relative path, inside `top`/
    when one is given. Players extract these ZIPs with their own tools, and Info-ZIP restores a symlink
    member as a symlink, so a link, FIFO or device member, or one that climbs out, never gets past here.
    build_bundle and build_source_bundle write only Unix (create_system 3) S_IFREG and S_IFDIR members."""
    names = archive.namelist()
    if len(names) != len(set(names)):
        raise Refused(f"{path.name} has duplicate member names")
    for info in archive.infolist():
        name = info.filename
        parts = name.rstrip("/").split("/")
        # On Windows zipfile turns a stored backslash into "/" in .filename; .orig_filename keeps the bytes.
        if ((top is not None and parts[0] != top) or "\\" in info.orig_filename
                or any(p in ("", ".", "..") for p in parts)):
            where = f"outside {top}/" if top is not None else "outside the archive"
            raise Refused(f"{path.name} has a member {where}: {name!r}")
        folder = name.endswith("/")
        kind = stat.S_IFMT(info.external_attr >> 16)
        if info.create_system != 3 or kind != (stat.S_IFDIR if folder else stat.S_IFREG):
            raise Refused(f"{path.name} has a member that is not a plain file or folder: {name!r} "
                          f"(system {info.create_system}, type {oct(kind)})")
    return names


def _pinned_keys_in(archive, top, what):
    """(PINNED_KEYS, rules) of the runtime in an open player ZIP, read with ast (never imported or run).
    rules is None when its update_trust_linux.py checks updates exactly as linux_update_trust.py's copy
    does, else what differs: checking a record with that copy is then not checking what this client checks."""
    try:
        source = archive.read(f"{top}/{LINUX_TRUST_MEMBER}").decode("utf-8")
        keys = trust.pinned_keys(source)
    except KeyError:
        raise Refused(f"{what} lacks {LINUX_TRUST_MEMBER}, so it trusts no update key")
    except (SyntaxError, ValueError) as e:                # UnicodeDecodeError is a ValueError
        raise Refused(f"{what}'s {LINUX_TRUST_MEMBER} has no readable PINNED_KEYS table ({e})")
    if not keys:
        raise Refused(f"{what} pins no update key, so no update could ever be verified against it")
    return keys, trust.client_rules_problem(source)


def inspect_player_zip(path, version):
    """The player ZIP's own claims: one top folder of plain files and folders, the setup members,
    bundle.json, the runtime's HUB_VERSION and the update keys it pins. Returns the sha256 of
    support/bundle.json, the bundle itself and {key_id: public key}."""
    top = LINUX_PREFIX + version
    try:
        with zipfile.ZipFile(path) as archive:
            names = _check_members(path, archive, top)
            missing = [m for m in LINUX_REQUIRED_MEMBERS if f"{top}/{m}" not in names]
            if missing:
                raise Refused(f"{path.name} lacks {', '.join(missing)}")
            damaged = archive.testzip()
            if damaged is not None:
                raise Refused(f"{path.name} is damaged at {damaged}")
            bundle_raw = archive.read(f"{top}/support/bundle.json")
            runtime_version = _hub_version_in(archive.read(f"{top}/support/runtime/hub/version.py"))
            pinned, rules = _pinned_keys_in(archive, top, path.name)
    except ZIP_ERRORS as e:
        raise Refused(f"{path.name} is not a readable ZIP ({e})")
    if rules:
        raise Refused(f"{path.name}'s {LINUX_TRUST_MEMBER} checks updates by other rules than publish.py: "
                      f"{rules}. Copy the client's definitions into tools/release/linux_update_trust.py first")
    if runtime_version != version:
        raise Refused(f"the runtime inside {path.name} is HUB_VERSION {runtime_version!r}, not {version}")
    bundle = _strict_json(bundle_raw, f"{path.name}'s support/bundle.json")
    if not isinstance(bundle, dict) or bundle.get("release") != version:
        raise Refused(f"{path.name}'s support/bundle.json is not release {version}")
    return hashlib.sha256(bundle_raw).hexdigest(), bundle, pinned


def inspect_source_zip(path, version):
    """The corresponding-source ZIP: readable, plain files and folders only, and its SOURCE-MANIFEST.json
    is this release's. (That manifest also names the private commit the source was exported from,
    without private-only files.)"""
    try:
        with zipfile.ZipFile(path) as archive:
            _check_members(path, archive)
            damaged = archive.testzip()
            if damaged is not None:
                raise Refused(f"{path.name} is damaged at {damaged}")
            raw = archive.read("SOURCE-MANIFEST.json")
    except KeyError:
        raise Refused(f"{path.name} has no SOURCE-MANIFEST.json")
    except ZIP_ERRORS as e:
        raise Refused(f"{path.name} is not a readable ZIP ({e})")
    manifest = _strict_json(raw, f"{path.name}'s SOURCE-MANIFEST.json")
    if not isinstance(manifest, dict) or manifest.get("release") != version:
        raise Refused(f"{path.name}'s SOURCE-MANIFEST.json is not release {version}")


def read_update_record(path):
    """sign_update's {key_id, manifest, signature}: the manifest decoded, in the client's shape. The
    key_id is 16 lowercase hex digits, the signature 64 bytes, and the manifest is canonical JSON
    (sorted keys, no spaces, integers only, no duplicate keys) with exactly the signed keys: the
    Linux client refuses anything else, whatever its signature. check_linux_release then verifies the
    signature itself under the keys the new ZIP and the published ZIP pin."""
    raw = path.read_bytes()
    if len(raw) > MAX_UPDATE_RECORD:
        raise Refused(f"{path.name} is larger than {MAX_UPDATE_RECORD} bytes")
    record = _strict_json(raw, path.name)
    if not isinstance(record, dict) or set(record) != {"key_id", "manifest", "signature"}:
        raise Refused(f"{path.name} must hold exactly key_id, manifest and signature")
    if not isinstance(record["key_id"], str) or not LINUX_KEY_ID_RE.fullmatch(record["key_id"]):
        raise Refused(f"{path.name} has an invalid key_id {str(record['key_id'])[:40]!r}; the Linux client "
                      "accepts only the 16 lowercase hex digits sign_update prints")
    if len(_b64(record["signature"], f"{path.name}'s signature")) != 64:
        raise Refused(f"{path.name}'s signature is not 64 bytes")
    manifest_raw = _b64(record["manifest"], f"{path.name}'s manifest")
    if len(manifest_raw) > MAX_UPDATE_MANIFEST:
        raise Refused(f"{path.name}'s manifest is larger than {MAX_UPDATE_MANIFEST} bytes")
    manifest = _strict_json(manifest_raw, f"{path.name}'s manifest", integers_only=True)
    if not isinstance(manifest, dict) or set(manifest) != UPDATE_MANIFEST_KEYS:
        raise Refused(f"{path.name}'s manifest does not have exactly the signed update keys")
    if trust.canonical(manifest) != manifest_raw:
        raise Refused(f"{path.name}'s manifest is not in canonical form (sorted keys, no spaces, UTF-8); "
                      "the Linux client only accepts the exact bytes sign_update writes")
    return record, manifest, manifest_raw


def update_trust_problem(entry, manifest_raw, pinned, whose):
    """Why a Linux client that pins `pinned` would refuse catalogue["linux"] = entry, or None when it
    would trust it. The last word is the client's own check (linux_update_trust.offer_manifest)."""
    update = entry["update"]
    name = update["key_id"]
    if name not in pinned:
        return (f"it is signed with key {name}, and {whose} pins only "
                f"{', '.join(sorted(pinned))}")
    signature = base64.b64decode(update["signature"])
    if not trust.verify(pinned[name], trust.DOMAIN + manifest_raw, signature):
        return f"its signature does not verify under key {name}, which {whose} pins"
    try:
        trust.offer_manifest(entry, pinned)
    except trust.UpdateRefused:
        return (f"{whose} refuses it by the client's own rules (canonical base64, integer ranges, and the "
                "entry's version, size and sha256 equal to the signed ones)")
    return None


def _published_linux_zip(current):
    """The player ZIP catalogue.linux serves now. It is in server/public/hub/ and carries the update keys
    that every client installed from it holds."""
    url = current.get("download_url")
    name = urlsplit(url).path.rsplit("/", 1)[-1] if isinstance(url, str) else ""
    path = Path(PUBLIC, "hub", name)
    if not path.is_file() or sha256_of(path) != current.get("sha256"):     # the sha256 names the exact file
        raise Refused(f"the published Linux ZIP {name or '?'} is not in server/public/hub/ with the catalogue's "
                      "sha256, so the keys installed clients trust cannot be read")
    return path


def _git_blobs_ever(relative):
    """Every blob that `relative` has had in this checkout's history (empty without git)."""
    if not shutil.which("git") or not os.path.exists(os.path.join(ROOT, ".git")):
        return set()
    r = subprocess.run(["git", "log", "--format=", "--raw", "--no-abbrev", "--no-renames", "HEAD", "--", relative],
                       cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    blobs = set()
    for line in (r.stdout or "").splitlines():
        fields = line.split()
        if line.startswith(":") and len(fields) >= 4:
            blobs.update(b for b in fields[2:4] if re.fullmatch(r"[0-9a-f]{40,64}", b) and set(b) != {"0"})
    return blobs


def _git_blob_of(path):
    r = subprocess.run(["git", "hash-object", "--no-filters", "--", str(path)], cwd=ROOT,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return (r.stdout or "").strip()


def check_served_name(path, expected_sha):
    """/hub/*.zip is served with a one-year immutable cache: a name that was ever served must keep its
    bytes, or browsers and edges keep handing out the old file under the new catalogue hash."""
    target = os.path.join(PUBLIC, "hub", path.name)
    if os.path.lexists(target):
        if os.path.islink(target) or not os.path.isfile(target) or sha256_of(target) != expected_sha:
            raise Refused(f"server/public/hub/{path.name} already exists with different bytes or as a link; "
                          "a served name is cached for a year, so publish a new version instead")
    blobs = _git_blobs_ever(f"server/public/hub/{path.name}")
    if blobs and _git_blob_of(path) not in blobs:
        raise Refused(f"server/public/hub/{path.name} was published before with different bytes (git history); "
                      "a served name is cached for a year, so publish a new version instead")


def _check_source_url(url, source_name):
    parts = urlsplit(url)
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment):
        raise Refused("--source-url must be a plain https URL")
    if parts.scheme + "://" + parts.netloc in OWNED_ORIGINS:
        raise Refused("--source-url names this site; leave it out and the source ZIP is served from /hub/")
    if parts.path.rsplit("/", 1)[-1] != source_name:
        raise Refused(f"--source-url must end in /{source_name}, the file it serves")


def check_linux_release(directory, catalogue, *, source_url=None, allow_behind_windows=False, required=False,
                        break_in_app_updates=False):
    """Every refusal, before anything is written. Returns (version, catalogue entry, [(file, sha256) to copy])."""
    version, files = linux_release_files(directory)
    vt = linux_version_tuple(version)
    player, source = files["player"], files["source"]
    size, sha = os.path.getsize(player), sha256_of(player)
    source_size, source_sha = os.path.getsize(source), sha256_of(source)
    if not 0 < size <= LINUX_LIMIT:
        raise Refused(f"{player.name} is {size} bytes; the download limit is {LINUX_LIMIT}")
    if source_size > LINUX_SOURCE_LIMIT:
        raise Refused(f"{source.name} is {source_size} bytes; the Linux client accepts at most {LINUX_SOURCE_LIMIT}")

    report = _strict_json(files["report"].read_bytes(), files["report"].name)
    if not isinstance(report, dict):
        raise Refused(f"{files['report'].name} is not a build report")
    if report.get("release") != version or report.get("hub_version") != version:
        raise Refused(f"the report's release ({report.get('release')!r}) and hub_version "
                      f"({report.get('hub_version')!r}) must both be {version}")
    if report.get("publishable") is not True:
        raise Refused("the report is not publishable (a --dev build, or built before publishable existed)")
    if (report.get("archive") != player.name or report.get("download_bytes") != size
            or report.get("download_sha256") != sha):
        raise Refused(f"{player.name}'s size or sha256 differs from the report")
    if report.get("source") != {"name": source.name, "bytes": source_size, "sha256": source_sha}:
        raise Refused(f"{source.name}'s size or sha256 differs from the report")
    installed = report.get("installed_bytes")
    if not _is_int(installed) or not 0 < installed <= LINUX_LIMIT:
        raise Refused("the report's installed_bytes is missing or above the 100,000,000-byte limit")
    launcher = report.get("launcher")
    launcher_sha = launcher.get("sha256") if isinstance(launcher, dict) else None

    record, manifest, manifest_raw = read_update_record(files["update"])
    expected = {"schema": 1, "product": "Lights Out", "role": "linux-native-update", "platform": "linux",
                "arch": LINUX_ARCH, "channel": "public-beta", "display_version": version, "release": version,
                "folder": LINUX_PREFIX + version}
    for key, want in expected.items():
        if manifest.get(key) != want or type(manifest.get(key)) is not type(want):
            raise Refused(f"the signed manifest's {key} is {manifest.get(key)!r}, not {want!r}")
    got_version = manifest.get("version")
    if (not isinstance(got_version, list) or len(got_version) != 4 or not all(_is_int(x) for x in got_version)
            or tuple(got_version) != vt):
        raise Refused(f"the signed manifest's version is {got_version!r}, not {list(vt)}")
    if manifest.get("archive") != {"name": player.name, "size": size, "sha256": sha}:
        raise Refused(f"{player.name}'s size or sha256 differs from the signed manifest")
    if manifest.get("source") != {"name": source.name, "size": source_size, "sha256": source_sha}:
        raise Refused(f"{source.name}'s size or sha256 differs from the signed manifest")
    if manifest.get("installed_bytes") != installed or not _is_int(manifest.get("installed_bytes")):
        raise Refused("the signed manifest's installed_bytes differs from the report")
    if not _is_sha(manifest.get("payload_sha256")) or manifest["payload_sha256"] != report.get("runtime_payload_sha256"):
        raise Refused("the signed manifest's payload_sha256 is not a SHA-256 or differs from the report")
    if not _is_sha(manifest.get("launcher_sha256")) or manifest["launcher_sha256"] != launcher_sha:
        raise Refused("the signed manifest's launcher_sha256 is not a SHA-256 or differs from the report")

    bundle_sha, bundle, new_keys = inspect_player_zip(player, version)
    if manifest.get("bundle_sha256") != bundle_sha:
        raise Refused(f"the signed manifest's bundle_sha256 is not {player.name}'s support/bundle.json")
    runtime = bundle.get("runtime") if isinstance(bundle.get("runtime"), dict) else {}
    if runtime.get("payload_sha256") != manifest["payload_sha256"]:
        raise Refused(f"{player.name}'s bundle.json names another runtime payload than the report")
    inspect_source_zip(source, version)

    current = catalogue.get("linux")
    if current is not None:
        current_version = current.get("version") if isinstance(current, dict) else None
        ct = linux_version_tuple(current_version)
        if ct is None:
            raise Refused(f"the catalogue's linux.version {current_version!r} is not X.Y.Z or X.Y.Z.N")
        if not vt > ct:
            raise Refused(f"{version} is not newer than the published Linux {current_version}; installed "
                          "clients only update to a strictly newer version")
    hub_version = (catalogue.get("hub") or {}).get("version")
    ht = linux_version_tuple(hub_version)
    if ht is None:
        raise Refused(f"the catalogue's hub.version {hub_version!r} is unreadable")
    if vt < ht:
        if not allow_behind_windows:
            raise Refused(f"{version} is older than the Windows hub {hub_version}; the queue gate would lock Linux "
                          "players out. Build Linux at the Windows version, or pass --allow-behind-windows once "
                          "the per-platform gate is live")
        live_js = os.path.join(ROOT, "server", "live.cjs")
        try:
            gated = PLATFORM_GATE in Path(live_js).read_text(encoding="utf-8")
        except OSError:
            gated = False
        if not gated:
            raise Refused(f"--allow-behind-windows needs the per-platform gate, and server/live.cjs does not "
                          f"announce {PLATFORM_GATE}")

    copies = [(player, sha)]
    if source_url is None:
        copies.append((source, source_sha))
    else:
        _check_source_url(source_url, source.name)
    for path, digest in copies:
        check_served_name(path, digest)

    entry = {
        "version": version, "kind": LINUX_KIND, "arch": LINUX_ARCH,
        "download_url": f"{PUBLIC_ORIGIN}/hub/{player.name}", "size": size, "sha256": sha,
        "source_url": source_url or f"{PUBLIC_ORIGIN}/hub/{source.name}",
        "source_size": source_size, "source_sha256": source_sha,
        "page_url": WEBSITE_ORIGIN + "/", "required": bool(required),
        "update": {"key_id": record["key_id"], "manifest": record["manifest"], "signature": record["signature"]},
    }

    # The record must be one a client would offer. Clients installed from the PUBLISHED ZIP verify it
    # with the keys that ZIP pins; clients installed from the new ZIP will need its keys for the next
    # update. On a first release only the new ZIP exists.
    problem = update_trust_problem(entry, manifest_raw, new_keys, player.name)
    if problem:
        raise Refused(f"the Linux client would never offer this update: {problem}")
    if current is not None:
        published = _published_linux_zip(current)
        whose = f"the published {published.name}"
        try:
            with zipfile.ZipFile(published) as archive:
                old_keys, old_rules = _pinned_keys_in(archive, LINUX_PREFIX + current_version, whose)
            if old_rules:
                problem = (f"{whose} checks updates by other rules than publish.py ({old_rules}), so this check "
                           "cannot stand in for those clients")
            else:
                problem = update_trust_problem(entry, manifest_raw, old_keys, whose)
        except Refused as e:                            # no readable key table: those clients trust nothing
            problem = str(e)
        except ZIP_ERRORS as e:
            raise Refused(f"{whose} is not a readable ZIP ({e})")
        if problem and not break_in_app_updates:
            raise Refused(f"clients installed from Linux {current_version} cannot be shown to offer this update: "
                          f"{problem}. Sign it with a key both ZIPs pin (docs/linux-update-signing.md, rotation). "
                          "Only if those clients can never take a new release in-app (every key they pin is lost), "
                          "pass --break-in-app-updates: they may then have to download the new ZIP by hand")
        if problem:
            say(f"WARNING: clients installed from Linux {current_version} may not update in-app ({problem}); "
                "they may have to download the new ZIP by hand")
    return version, entry, copies


def publish_linux(args):
    """Check the Linux release in args.dir, copy its ZIPs into server/public/hub and write catalogue.linux.
    Never touches hub/version.py, catalogue.hub or the Windows signing tool."""
    c = load_catalogue()
    try:
        version, entry, copies = check_linux_release(
            args.dir, c, source_url=args.source_url, allow_behind_windows=args.allow_behind_windows,
            required=args.required, break_in_app_updates=args.break_in_app_updates)
    except Refused as e:
        die(f"Linux release refused: {e}")
    hub_dir = os.path.join(PUBLIC, "hub")
    os.makedirs(hub_dir, exist_ok=True)
    with open(CATALOGUE, "rb") as f:
        catalogue_before = f.read()
    created = []
    try:
        for path, digest in copies:
            target = os.path.join(hub_dir, path.name)
            if os.path.exists(target):                  # check_served_name proved these are the same bytes
                continue
            partial = target + ".partial"
            shutil.copyfile(path, partial)
            if sha256_of(partial) != digest:
                os.remove(partial)
                die(f"the copy of {path.name} does not match the file that was checked")
            os.replace(partial, target)
            created.append(target)
            say(f"copied {path.name} into server/public/hub")
        c["linux"] = entry
        save_catalogue(c)
    except BaseException:
        for target in created:
            os.remove(target)
        for path, _ in copies:
            partial = os.path.join(hub_dir, path.name + ".partial")
            if os.path.exists(partial):
                os.remove(partial)
        with open(CATALOGUE, "wb") as f:
            f.write(catalogue_before)
        raise
    keep = {path.name for path, _ in copies}
    for other in sorted(os.listdir(hub_dir)):           # only the current Linux ZIPs are served
        if other.startswith(LINUX_PREFIX) and other not in keep:
            os.remove(os.path.join(hub_dir, other))
            say(f"removed old {other}")
    say(f"catalogue: linux {version}, {copies[0][0].name} ({entry['size']} B, sha256 {entry['sha256'][:12]}…), "
        f"source {entry['source_url']} ({entry['source_size']} B), required {entry['required']}")
    return version


# ---------------------------------------------------------------- packs
def publish_proxy_endpoints(args):
    """Migrate the verified shipped cook, never a stale mirror or game-owned asset."""
    from endpoint_assets import retarget_tree, zip_tree, verify_proof
    old_host = 'lightsout.up.railway.app'
    new_host = urlsplit(PUBLIC_ORIGIN).hostname
    c = load_catalogue()
    entry = next(g for g in c['gamemodes'] if g['id'] == 'BB5')
    source = Path(PUBLIC) / 'packs/BB5-1.0.28.zip'
    baseline_sha = '7891a9b49edcda712e8a03bf09dd0213ca94afe3cd5afb961c5bf85f91e9ba0f'
    if entry['version'] != '1.0.28' or entry['sha256'] != baseline_sha or not source.is_file() or sha256_of(source) != baseline_sha:
        raise ValueError('Proxy migration requires the reviewed BB5 1.0.28 baseline')
    version = check_version(args.version)
    if not version_newer(version, entry['version']):
        raise ValueError('Proxy pack version must be newer than the baseline')
    seed = Path(ROOT) / 'hub/lobbyseed'
    seed_hashes = {'GM_CHJoin.uexp': '11446f353699c79c22e426cc3b6f2b990aff00a2eacdea61acc4c7bdaae08b4d',
                   'GM_CHLobby.uexp': '3b23e760f86e34a4150e09df6cf2fde4d7f8edc41c16a1a0fbd7f3c9d350609e'}
    for name, digest in seed_hashes.items():
        matches = list(seed.rglob(name))
        if len(matches) != 1 or sha256_of(matches[0]) != digest:
            raise ValueError('Proxy migration requires the reviewed lobby baseline: ' + name)
    mpath = find_manifest('BB5')
    with tempfile.TemporaryDirectory(prefix='lightsout-proxy-') as temporary:
        stage = Path(temporary)
        shutil.copytree(seed, stage / 'seed')
        pack = stage / 'pack'
        pack.mkdir()
        with zipfile.ZipFile(source) as archive:
            for info in archive.infolist():
                target = (pack / info.filename).resolve()
                if not target.is_relative_to(pack.resolve()) or '\\' in info.filename or ':' in info.filename:
                    raise ValueError('Unsafe path in baseline archive')
                if not info.is_dir():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.read(info))
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pak'))
        from build_gamemode import Builder
        Builder.check_cook_verdict(str(pack / 'cooked'))
        Builder.check_cook_verdict(str(stage / 'seed'))
        proof = []
        retarget_tree(stage / 'seed', old_host, new_host, proof)
        retarget_tree(pack, old_host, new_host, proof)
        verify_proof(proof, migration=True)
        manifest_path = pack / 'manifest.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        manifest['version'] = version
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        output = stage / ('BB5-' + version + '.zip')
        zip_tree(pack, output)
        entry.update(version=version, pack_url=PUBLIC_ORIGIN + '/packs/' + output.name,
                     sha256=sha256_of(output), size=output.stat().st_size)
        evidence = {'source_host': old_host, 'target_host': new_host, 'assets': proof,
                    'source_pack_sha256': baseline_sha, 'target_pack_sha256': entry['sha256'],
                    'method': 'Equal-width inline string substitution; disassembly and byte reversibility verified. No recook.'}
        proof_path = Path(ROOT) / 'docs/releases/2026-09-22-proxy-endpoints.json'
        touched = [next(seed.rglob(name)) for name in seed_hashes]
        touched += [Path(mpath), source.parent / output.name, proof_path, Path(CATALOGUE), source]
        root = Path(ROOT).resolve()
        if any(not path.resolve().is_relative_to(root) for path in touched):
            raise ValueError('Migration output is outside the release workspace')
        backup = {path: path.read_bytes() if path.exists() else None for path in touched}
        try:
            # All semantic checks succeeded before any project source or release artifact is changed.
            for name in seed_hashes:
                changed = next((stage / 'seed').rglob(name))
                shutil.copy2(changed, seed / changed.relative_to(stage / 'seed'))
            source_manifest = json.loads(Path(mpath).read_text(encoding='utf-8'))
            source_manifest['version'] = version
            Path(mpath).write_text(json.dumps(source_manifest, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
            shutil.copy2(output, source.parent / output.name)
            proof_path.parent.mkdir(parents=True, exist_ok=True)
            proof_path.write_text(json.dumps(evidence, indent=2) + '\n', encoding='utf-8')
            save_catalogue(c)
            source.unlink()
        except BaseException:
            for path, data in backup.items():
                if data is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(data)
            raise

    say(f'Migrated 32 endpoint constants in 7 authored assets; BB5 {version}, {entry["size"]} bytes')
    return version


def find_manifest(mode_id):
    gm = os.path.join(ROOT, "gamemodes")
    for d in sorted(os.listdir(gm)):
        p = os.path.join(gm, d, "manifest.json")
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                if json.load(f).get("id") == mode_id:
                    return p
    die(f"no gamemodes/*/manifest.json with id {mode_id!r}")


def publish_pack(args):
    mpath = find_manifest(args.id)
    with open(mpath, encoding="utf-8") as f:
        m = json.load(f)
    old = str(m.get("version", "1.0.0"))
    new = args.version or (bump_version(old, args.bump) if args.bump else old)
    if new != old:
        m["version"] = new
        with open(mpath, "w", encoding="utf-8") as f:
            json.dump(m, f, indent=2, ensure_ascii=False)
            f.write("\n")
        say(f"{args.id} version {old} -> {new}")
    elif "version" not in m:
        m["version"] = new
        with open(mpath, "w", encoding="utf-8") as f:
            json.dump(m, f, indent=2, ensure_ascii=False)
            f.write("\n")
    cooked = os.path.abspath(args.cooked or os.path.join(ROOT, "mirror", "cooked"))
    if not os.path.isfile(os.path.join(cooked, "blueprints_summary.txt")):
        die(f"no cook at {cooked} (run 2_make_blueprints.bat and 3_cook.bat first)")
    packs = os.path.join(ROOT, "packs")
    os.makedirs(packs, exist_ok=True)
    say(f"Packing {args.id} {new} from {cooked}…")
    run([venv_python(), os.path.join("tools", "pack", "make_pack.py"), "--manifest", mpath, "--cooked", cooked,
         "--version", new, "--out", packs, "--base-url", f"{origin()}/packs"], cwd=ROOT)
    name = f"{args.id}-{new}"
    zip_src = os.path.join(packs, name + ".zip")
    with open(os.path.join(packs, name + ".json"), encoding="utf-8") as f:
        entry = json.load(f)
    entry.pop("files", None)
    pdir = os.path.join(PUBLIC, "packs")
    os.makedirs(pdir, exist_ok=True)
    shutil.copy2(zip_src, os.path.join(pdir, name + ".zip"))
    for fn in os.listdir(pdir):                          # only the current zip of this gamemode is served
        if fn.startswith(args.id + "-") and fn.endswith(".zip") and fn != name + ".zip":
            os.remove(os.path.join(pdir, fn))
            say(f"removed old {fn}")
    c = load_catalogue()
    modes = c.setdefault("gamemodes", [])
    for i, g in enumerate(modes):
        if g.get("id") == args.id:
            modes[i] = entry
            break
    else:
        modes.append(entry)
    save_catalogue(c)
    say(f"catalogue: {args.id} {new}, {name}.zip ({entry['size']} B, sha256 {entry['sha256'][:12]}…)")
    return new


# ---------------------------------------------------------------- deploy + verify
# What a release actually writes. deploy() stages exactly these and nothing else.
# What a release actually writes. deploy() stages exactly these and nothing else.
#
# server/public/packs IS one of them. It was missing until 2026-09-16 and that made `publish.py
# pack` undeployable: publish_pack() writes the new <ID>-<v>.zip and deletes the old one, both
# under server/, and deploy()'s own stray check below then refused the release because neither
# path was an artefact. `railway up` used to upload server/ wholesale so the omission was
# invisible; since 4144f3a Railway builds from the branch, so an unstaged zip is a 404 pack_url.
# gamemodes/<id>/manifest.json is deliberately NOT here even though publish_pack() bumps it:
# staging the whole source directory is exactly the `git add -A` hazard this list exists to avoid.
# The bump is committed by hand, by path, alongside the release.
RELEASE_PATHS = ("hub/version.py", "server/public/catalogue.json", "server/public/hub",
                 "server/public/packs")


def deploy(message=None, target_branch=None):
    """Ship server/ by PUSHING TO GITHUB, then let verify() wait for the live catalogue to change.

    This used to be `railway up` and that path is dead. The `community-hub` service was reconnected
    to the repo on 2026-09-15 and now builds from GitHub with **Root Directory `/server`**:

        railway api 'query { serviceInstance(serviceId: "<id>", environmentId: "<id>")
                             { rootDirectory watchPatterns source { repo } } }'
        -> rootDirectory "/server", source.repo "WarrS03448/community-hub"

    `railway up` uploads the CONTENTS of server/; railpack then looks for a `server/` folder INSIDE
    that archive, does not find it, and the build dies instantly with "railpack prepare exited with
    an error" - seconds after "unpacking archive", with no other diagnostic. The deployment shows
    FAILED while the previous one keeps serving, so the site silently stays on the old version, and
    the old code retried it three times, stacking three FAILED deployments. It is a config mismatch,
    not a transient builder error. Do not bring `railway up` back without re-checking that query.

    Two rules this function exists to enforce:

      * **Never `git add -A`.** This checkout is routinely shared with another session's in-flight
        work, and a release must not sweep that into the deploy commit. Only RELEASE_PATHS is staged.
      * **Refuse a half-deploy.** Railway ships whatever `server/` is on the branch, so an uncommitted
        change under server/ that is NOT a release artefact would be left behind and the live service
        would not match this checkout. That is worse than a refused release, so it stops here.

    A push is also a live change to the game's only clock (the lobby pak buys its ~4 s wait by
    holding /api/probe/slow open) and it drops every in-progress match, because match state is in
    memory. Both are reasons to release deliberately, not to retry blindly.
    """
    if not shutil.which("git") or not os.path.exists(os.path.join(ROOT, ".git")):
        die("deploying means pushing to GitHub, and this is not a git checkout with git on PATH")

    def git_out(*args):
        """Raw stdout, NOT stripped. `git status --porcelain` puts the two status columns in 0-1 and
        a space in 2, so the path starts at index 3 — and stripping the whole output would eat the
        leading space of the FIRST line only, shifting that one path by a character. The first line
        under server/ is normally `server/public/catalogue.json`, which every release rewrites, so a
        stripping version of this reported it as `erver/...`, failed the artefact match below, and
        refused to deploy. Every release. Strip at the call site instead."""
        r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        return r.stdout or ""

    if target_branch is not None:
        if run(['git', 'check-ref-format', '--branch', target_branch], cwd=ROOT, check=False) != 0:
            die('Invalid deployment branch')

    # Anything modified under server/ that is not a release artefact would never reach Railway.
    artefacts = tuple(p for p in RELEASE_PATHS if p.startswith("server/"))
    stray = []
    for line in git_out("status", "--porcelain", "--", "server").splitlines():
        if not line.strip():
            continue
        name = line[3:].strip().strip('"').replace("\\", "/")
        if not any(name == a or name.startswith(a + "/") for a in artefacts):
            stray.append(name)
    if stray:
        die("server/ has uncommitted changes that are not release artefacts, and Railway deploys "
            "whatever is on the branch — they would be left behind and the live service would not "
            "match this checkout. Commit or stash them first:\n    " + "\n    ".join(stray[:20]))

    say("Deploying: committing the release artefacts and pushing to GitHub…")
    present = [p for p in RELEASE_PATHS if os.path.exists(os.path.join(ROOT, p.replace("/", os.sep)))]
    run(["git", "add", "--"] + present, cwd=ROOT)
    if git_out("diff", "--cached", "--name-only").strip():
        run(["git", "commit", "-m", message or "Release"], cwd=ROOT)
    else:
        say("  nothing new to commit — pushing whatever is already ahead of origin")

    branch = git_out("rev-parse", "--abbrev-ref", "HEAD").strip() or "HEAD"
    destination = "HEAD:refs/heads/" + target_branch if target_branch else branch
    if run(["git", "push", "origin", destination], cwd=ROOT, check=False) != 0:
        die("git push failed — Railway only ships what reaches the branch, so nothing was deployed. "
            "Fix the push (pull/rebase if origin moved) and run `publish.py deploy` again")
    say(f"  pushed {destination} — Railway builds from the repo; verify() waits for the live catalogue")


def current_branch():
    """The checked-out branch's name ("HEAD" when detached), or None without git."""
    if not shutil.which("git") or not os.path.exists(os.path.join(ROOT, ".git")):
        return None
    r = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return (r.stdout or "").strip() or None


def as_published(catalogue):
    """A catalogue with the bits the SERVICE adds per request taken back off, for comparing.

    COMP_GAME_RULES_OVERRIDE attaches `rules_override` to a gamemode and numbers its version with a
    fourth component (server.cjs applyRulesOverride / overriddenVersion), neither of which is in the
    file on disk. This used to be a plain `live == local`, so a release cut while an override was
    set could NEVER verify: it sat comparing until the timeout and then printed a message about
    Railway deployments, which is the wrong place to look and the wrong thing to suspect.

    Everything that identifies the BUILD is still compared exactly - the pack urls, the sizes, the
    sha256s, the hub version and the three-part gamemode version. Only the two fields that are a
    function of an environment variable are dropped, and they are dropped from BOTH sides, so a
    live catalogue that disagrees about anything else still fails."""
    out = json.loads(json.dumps(catalogue or {}))
    for mode in out.get("gamemodes") or []:
        if not isinstance(mode, dict):
            continue
        overridden = mode.pop("rules_override", None) is not None
        version = str(mode.get("version") or "")
        if overridden and version.count(".") == 3:
            mode["version"] = version.rsplit(".", 1)[0]
    return out


def verify(wait_seconds=420):
    base = origin()
    local = load_catalogue()
    say(f"Verifying {base} …")
    deadline = time.time() + wait_seconds
    live = None
    while True:
        try:
            status, _h, body = http(base + "/catalogue.json")
            live = json.loads(body.decode("utf-8"))
            if as_published(live) == as_published(local):
                break
            say("  live catalogue differs from the local one — waiting for the deploy to land…")
        except Exception as e:                            # noqa: BLE001
            say(f"  not reachable yet ({e}) — waiting…")
        if time.time() > deadline:
            try:                                           # show Railway's view before giving up
                run([shutil.which("railway"), "logs", "--build", "--lines", "40"], cwd=SERVER, check=False)
            except Exception:                              # noqa: BLE001
                pass
            die("the live catalogue did not match the local one in time — open the Deployments tab in Railway: "
                "if the latest CLI deployment failed, its build log says why (a Root Directory that is not empty is the usual cause)")
        time.sleep(10)
    say("  catalogue.json: matches")
    status, _h, body = http(base + "/api/health")
    if status != 200:
        die(f"/api/health returned {status}")
    say("  /api/health: OK")
    # SIZE WAS NOT ENOUGH, AND A CHECK THAT LOOKS STRONGER THAN IT IS, IS THE DANGEROUS KIND.
    #
    # This used to do a HEAD and compare Content-Length, and nothing here ever hashed anything -
    # `sha256_of` was called only at WRITE time. A length match cannot tell two DIFFERENT BUILDS
    # OF THE SAME VERSION apart, which is not a hypothetical: on 2026-09-16 four sessions released
    # into this repo within the hour, three of them independently claimed 2.3.19, and two separate
    # installers existed under that one number. A green verify would have accepted either.
    #
    # The catalogue already carries the sha256 this function has been fetching all along, so the
    # comparison costs one GET instead of one HEAD. That is ~21MB for the hub and ~40KB a pack,
    # a few seconds, once per release - against the alternative of shipping somebody else's build
    # and being told so by a player.
    #
    # HEAD still goes first: it fails fast and cheap on the common case (the deploy has not landed,
    # so the file is missing or the wrong length), and only then is a 21MB download worth doing.
    checks = [("hub", local["hub"]["download_url"], local["hub"].get("size"),
               local["hub"].get("sha256"))]
    checks += [(g.get("id") or "pack", g["pack_url"], g.get("size"), g.get("sha256"))
               for g in local.get("gamemodes", [])]
    linux = local.get("linux")
    if linux is not None:
        # The Linux ZIP and its corresponding-source ZIP get the same HEAD-then-full-GET check. Both are
        # mandatory here: publish_linux always writes a size and a sha256, so a gap is a broken catalogue.
        linux = linux if isinstance(linux, dict) else {}
        for what, field, size_field, sha_field in (("Linux ZIP", "download_url", "size", "sha256"),
                                                   ("Linux source ZIP", "source_url", "source_size", "source_sha256")):
            url, size, want_sha = linux.get(field), linux.get(size_field), linux.get(sha_field)
            if not (isinstance(url, str) and url and size and want_sha):
                die(f"catalogue.linux is incomplete: the {what} needs {field}, {size_field} and {sha_field}")
            checks.append((what, url, size, want_sha))
    for what, url, size, want_sha in checks:
        try:
            status, h, _b = http(url, method="HEAD")
        except urllib.error.HTTPError as e:
            die(f"{url}: HTTP {e.code}")
        got = int(h.get("Content-Length") or -1)
        if status != 200 or (size and got != int(size)):
            die(f"{url}: status {status}, size {got} (expected {size})")
        if not want_sha:
            # An entry written before the catalogue carried hashes. Say so rather than claim a
            # check that did not happen - a silent skip is how this gap survived in the first place.
            say(f"  {url.split('/')[-1]}: {got} B OK (no sha256 in the catalogue — bytes unverified)")
            continue
        try:
            status, _h, body = http(url, timeout=300)
        except urllib.error.HTTPError as e:
            die(f"{url}: HTTP {e.code} on the body")
        served = hashlib.sha256(body).hexdigest()
        if served != want_sha:
            die(f"{url}: the bytes being served are NOT the {what} in this catalogue.\n"
                f"      served    sha256 {served}\n"
                f"      catalogue sha256 {want_sha}\n"
                "      Same length, different build — almost always two releases cut under one "
                "version number. Do not re-run verify; find out whose build is live.")
        say(f"  {url.split('/')[-1]}: {got} B, sha256 {served[:12]}… OK")
        if what == "Linux ZIP":
            # A record the client refuses deploys and serves fine; every client just never offers it.
            problem = live_linux_update_problem(live["linux"], body)
            if problem:
                die(f"the live catalogue.linux.update would never be offered: {problem}")
            say(f"  catalogue.linux.update: signed with key {live['linux']['update']['key_id']}, which the "
                "served ZIP pins; the client's checks pass OK")
    say("Live site verified.")


def live_linux_update_problem(entry, body):
    """Why a client installed from the served Linux ZIP (`body`) would refuse the live catalogue.linux
    as an update offer, or None. Only the served ZIP is left by now; publish_linux also checked the
    keys of the ZIP it replaced."""
    try:
        # Its rules are not compared here: publish_linux compared them when it published this ZIP, and a
        # newer copy (made for a Linux build still to come) must not fail a Windows release's verify.
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            keys, _rules = _pinned_keys_in(archive, LINUX_PREFIX + str(entry.get("version")), "the served Linux ZIP")
    except Refused as e:
        return str(e)
    except ZIP_ERRORS as e:
        return f"the served Linux ZIP is not a readable ZIP ({e})"
    try:
        trust.offer_manifest(entry, keys)
    except trust.UpdateRefused:
        update = entry.get("update") if isinstance(entry.get("update"), dict) else {}
        name = update.get("key_id")
        if not (isinstance(name, str) and name in keys):
            return f"its key_id is {name!r}, and the served ZIP pins only {', '.join(sorted(keys))}"
        return (f"the Linux client refuses it under key {name} (a signature that does not verify, a manifest "
                "that is not canonical or breaks an integer range, or a version, size or sha256 that differs)")
    return None


def linux_summary(catalogue):
    """One status line for catalogue.linux: its version and the files it serves."""
    linux = (catalogue or {}).get("linux")
    if linux is None:
        return "linux: none published"
    if not isinstance(linux, dict):
        return "linux: INVALID entry"
    name = str(linux.get("download_url") or "").rsplit("/", 1)[-1] or "?"
    source = str(linux.get("source_url") or "").rsplit("/", 1)[-1] or "?"
    return (f"linux {linux.get('version')}  {name} ({linux.get('size')} B), source {source}"
            + ("  REQUIRED" if linux.get("required") is True else ""))


def status():
    base = origin()
    local = load_catalogue()
    say(f"local : hub {local['hub'].get('version')}  " + ", ".join(f"{g['id']} {g.get('version')}" for g in local.get("gamemodes", [])))
    say(f"        installer {str(local['hub'].get('download_url', '')).rsplit('/', 1)[-1] or '?'} "
        f"(kind {local['hub'].get('kind') or 'legacy one-file exe'})")
    say("        " + linux_summary(local))
    try:
        _s, _h, body = http(base + "/catalogue.json")
        live = json.loads(body.decode("utf-8"))
        say(f"live  : hub {live['hub'].get('version')}  " + ", ".join(f"{g['id']} {g.get('version')}" for g in live.get("gamemodes", [])))
        say("        " + linux_summary(live))
        say("in sync" if live == local else "DIFFERENT — run publish.py deploy")
    except Exception as e:                                # noqa: BLE001
        say(f"live  : unreachable ({e})")
    say(f"hub/version.py: {read_hub_version()}")


def git_push(message):
    """Kept only so an existing `--push` on a command line does not crash. deploy() IS the push now,
    and this used to `git add -A`, which would sweep another session's in-flight work into a release
    commit. It deliberately does nothing."""
    say("(--push is a no-op: deploy() already commits the release artefacts and pushes)")


# ---------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("hub"); h.add_argument("--bump", choices=["patch", "minor", "major"]); h.add_argument("--version")
    h.add_argument("--same-version", action="store_true"); h.add_argument("--no-deploy", action="store_true"); h.add_argument("--push", action="store_true")
    p = sub.add_parser("pack"); p.add_argument("id"); p.add_argument("--bump", choices=["patch", "minor", "major"]); p.add_argument("--version")
    p.add_argument("--cooked"); p.add_argument("--no-deploy", action="store_true"); p.add_argument("--push", action="store_true")
    lx = sub.add_parser("linux", help="publish a native Linux beta build (docs/linux-release-runbook.md)")
    lx.add_argument("--dir", required=True, type=Path,
                    help="folder with the player ZIP, source ZIP, build report and signed update record")
    lx.add_argument("--no-deploy", action="store_true")
    lx.add_argument("--required", action="store_true", help="installed Linux clients must update")
    lx.add_argument("--source-url", help="https URL that serves the source ZIP elsewhere (not copied here)")
    lx.add_argument("--allow-behind-windows", action="store_true",
                    help="allow a version older than hub.version (only with the per-platform gate)")
    lx.add_argument("--break-in-app-updates", action="store_true",
                    help="publish although clients of the published Linux version cannot be shown to accept "
                         "this release (every key they pin is lost); they may have to download it by hand")
    migration = sub.add_parser('proxy-endpoints'); migration.add_argument('--version', required=True)
    d = sub.add_parser("deploy"); d.add_argument("--branch", help="Explicit remote deployment branch (no force push)")
    sub.add_parser("verify"); sub.add_parser("status")
    args = ap.parse_args(argv)
    if args.cmd == "hub":
        v = publish_hub(args)
        if not args.no_deploy:
            deploy(f"Release hub {v}"); verify()
        if args.push:
            git_push(f"publish hub {v}")
    elif args.cmd == "pack":
        v = publish_pack(args)
        if not args.no_deploy:
            deploy(f"Release {args.id} {v}"); verify()
        if args.push:
            git_push(f"publish {args.id} {v}")
    elif args.cmd == "linux":
        if not args.no_deploy:
            # deploy() pushes the CURRENT branch. The runbook's release worktree is on release/linux-<V>,
            # and a push there reaches no deployment: verify() would wait out its timeout and blame Railway.
            branch = current_branch()
            if branch != "main":
                die(f"publish.py linux deploys by pushing the current branch, and this checkout is on "
                    f"{branch or 'no branch'}, not main. Run it with --no-deploy, then deploy with "
                    "`publish.py deploy --branch main` (docs/linux-release-runbook.md, step 6)")
        v = publish_linux(args)
        if not args.no_deploy:
            deploy(f"Release Linux beta {v}"); verify()
    elif args.cmd == 'proxy-endpoints':
        publish_proxy_endpoints(args)
    elif args.cmd == "deploy":
        deploy(target_branch=args.branch); verify()
    elif args.cmd == "verify":
        verify()
    elif args.cmd == "status":
        status()
    say("\nDONE.")


if __name__ == "__main__":
    main()
