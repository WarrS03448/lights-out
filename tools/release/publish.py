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
    publish.py deploy        commit the release artefacts and push to GitHub (Railway builds from the repo),
                             then verify.
    publish.py verify        the live catalogue must equal server/public/catalogue.json, every pack_url and the
                             hub download_url must answer with the right size, /api/health must be OK.
    publish.py status        local vs live versions.
    publish.py proxy-endpoints --version X.Y.Z
        one-time, disassembly-verified migration of the shipped BB5/lobby URL constants.
        Produces local release artifacts only; build the signed hub and deploy afterward.
    --push                   accepted and ignored. `deploy` IS the push now; --push used to `git add -A`,
                             which would sweep another session's in-flight work into a release commit.

Nothing here is edited by hand any more: the catalogue, the installer in public/hub and the zips in public/packs
are all written by this script. Double-click wrappers: release\\*.bat.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit
from pathlib import Path
import zipfile

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
    return new


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
    say("Live site verified.")


def status():
    base = origin()
    local = load_catalogue()
    say(f"local : hub {local['hub'].get('version')}  " + ", ".join(f"{g['id']} {g.get('version')}" for g in local.get("gamemodes", [])))
    say(f"        installer {str(local['hub'].get('download_url', '')).rsplit('/', 1)[-1] or '?'} "
        f"(kind {local['hub'].get('kind') or 'legacy one-file exe'})")
    try:
        _s, _h, body = http(base + "/catalogue.json")
        live = json.loads(body.decode("utf-8"))
        say(f"live  : hub {live['hub'].get('version')}  " + ", ".join(f"{g['id']} {g.get('version')}" for g in live.get("gamemodes", [])))
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
