# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Lights Out (one-folder Windows build: dist\LightsOut\LightsOut.exe + _internal\).
# Run from the repository root:  pyinstaller --noconfirm hub\hub.spec   (build_hub.bat does this, then wraps
# the folder in an Inno Setup installer, hub\installer.iss).
#
# Why --onedir and not --onefile: Defender's cloud ML flagged the unsigned one-file exe as a trojan
# (docs/distribution). A one-file build unpacks itself into %TEMP% and execs from there, which is
# exactly what droppers do; a plain folder with a versioned, described exe looks like ordinary
# software. The names are stable (no version in them) so the installer, the updater and shortcuts
# never have to chase a filename; the version lives in the exe's version-info resource instead.
import os, sys, re
# PyInstaller hook helpers for the --webui native deps (pywebview + pythonnet). Imported at the top
# and guarded so the spec still parses/runs on the macOS/Linux authoring box, where these helpers
# always exist (they ship with PyInstaller) but the packages they collect may not be installed.
try:
    from PyInstaller.utils.hooks import collect_all, collect_submodules
except ImportError as _e:                                 # pragma: no cover - PyInstaller always ships these
    print(f"hub.spec: PyInstaller hook helpers unavailable ({_e}); webui deps not collected")
    collect_all = collect_submodules = None
ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))      # SPECPATH = this hub/ folder; paths in a spec resolve against it
# Put hub/ on sys.path only long enough to read version.py, then remove it again: leaving it on the
# path lets PyInstaller's dependency analysis import hub/*.py as top-level modules (os.py, io.py would
# not clash here, but e.g. hub/live.py could shadow a future import) — the app imports them as the
# `hub` package, so the analyser should too.
sys.path.insert(0, SPECPATH)
from version import (
    HUB_VERSION, HUB_FILE_VERSION, version_tuple,
    COMPANY_NAME, PRODUCT_NAME, FILE_DESCRIPTION, LEGAL_COPYRIGHT,
)
sys.path.remove(SPECPATH)

# ---------------------------------------------------------------- version-info resource
# Built here from version.py rather than kept as a committed .txt so it cannot go stale. The
# versioninfo module imports pefile and pywin32-ctypes at module level, so this import ALWAYS fails on
# macOS/Linux (where we author and commit from) and only succeeds on the Windows build box. That is
# fine: the resource only means anything on Windows. When the import fails the build still goes
# through, just without the resource, so `pyinstaller hub\hub.spec` at least parses off-Windows.
_VT = version_tuple(HUB_VERSION)          # numeric 4-tuple; the resource fields must be numeric
try:
    from PyInstaller.utils.win32.versioninfo import (
        VSVersionInfo, FixedFileInfo, StringFileInfo, StringTable, StringStruct, VarFileInfo, VarStruct,
    )
except ImportError as _e:
    print(f"hub.spec: no version-info resource ({_e})")
    VERSION_INFO = None
else:
    VERSION_INFO = VSVersionInfo(
        ffi=FixedFileInfo(
            filevers=_VT,
            prodvers=_VT,
            mask=0x3F,
            flags=0x0,
            OS=0x40004,          # VOS_NT_WINDOWS32
            fileType=0x1,        # VFT_APP
            subtype=0x0,
            date=(0, 0),
        ),
        kids=[
            StringFileInfo([
                StringTable("040904B0", [     # en-US, Unicode
                    StringStruct("CompanyName", COMPANY_NAME),
                    StringStruct("FileDescription", FILE_DESCRIPTION),
                    StringStruct("FileVersion", HUB_FILE_VERSION),
                    StringStruct("InternalName", "LightsOut"),
                    StringStruct("OriginalFilename", "LightsOut.exe"),
                    StringStruct("ProductName", PRODUCT_NAME),
                    StringStruct("ProductVersion", HUB_VERSION),   # display string is fine for the string table
                    StringStruct("LegalCopyright", LEGAL_COPYRIGHT),
                ]),
            ]),
            VarFileInfo([VarStruct("Translation", [1033, 1200])]),
        ],
    )

# ---------------------------------------------------------------- data files
# The pak builder is imported by path at runtime (hub/paths.py -> <_MEIPASS>/tools/pak), so it travels as
# data files, invisible to the analyser. Only the import closure of build_gamemode.py and
# build_lobby_override.py is shipped: the rest of tools/pak is scratch/dump/test scripts (build_test*.py,
# build_dt_test.py, scanpaks.py, dtdump CLIs, ...) that nothing in the hub imports. Derived by following
# every `import`/`from` (top-level and inside functions) from the two entry modules:
#   build_gamemode        -> paklib, dtdump, pakfile, pkgedit, assetregistry_add, unversioned,
#                            dtrows (fn), classinfo (fn), locres (fn)
#   build_lobby_override  -> paklib, pkgedit, build_gamemode, classinfo (fn)
#   dtdump                -> uasset, udstruct, dtrows        dtrows   -> uasset, udstruct
#   udstruct              -> uasset                          pakfile  -> paklib, ooz (hiddenimport)
#   assetregistry_add     -> cityhash                        classinfo-> kismet     kismet -> pkgedit
#   locres                -> paklib, cityhash
# Adding a new sibling import to any of these means adding it here too (the build does not fail, the
# hub does, at install time).
PAK_DIR = os.path.join(ROOT, "tools", "pak")
PAK_MODULES = [
    "build_gamemode.py",
    "build_lobby_override.py",   # competitive lobby override; kept even though only the CLI uses it today
    "assetregistry_add.py",
    "cityhash.py",
    "classinfo.py",
    "dtdump.py",
    "dtrows.py",
    "kismet.py",
    "locres.py",
    "pakfile.py",
    "paklib.py",
    "pkgedit.py",
    # hub/lobbypak.py loads this BY PATH at runtime to cut the per-match lobby pak, so PyInstaller
    # cannot see the dependency and the omission is silent: _retarget_module() raises, lobbypak
    # swallows it, prepare() returns "" and every host is quietly told to open the map by hand.
    # Shipped 2.0.4 without it and that is exactly what happened.
    "retarget_lobby.py",
    "uasset.py",
    "udstruct.py",
    "unversioned.py",
]
_PAK_SET = set(PAK_MODULES)
_ALL_PAK = {f for f in os.listdir(PAK_DIR) if f.endswith(".py")}
# Guard the list against a future dependency edit: if any shipped module imports a SIBLING
# tools/pak/<name>.py that is not in PAK_MODULES, the build fails here rather than the installed hub
# failing at first use with a missing-module traceback. `import x` and `from x import ...` where
# x.py sits next to it are the only forms the builder uses (all sibling imports are bare, no package).
_IMPORT_RE = re.compile(r"^\s*(?:from\s+(\w+)\s+import|import\s+([\w, ]+))", re.M)
for _m in PAK_MODULES:
    _path = os.path.join(PAK_DIR, _m)
    if not os.path.isfile(_path):
        raise SystemExit(f"hub.spec: tools/pak/{_m} is listed in PAK_MODULES but does not exist")
    with open(_path, encoding="utf-8") as _fh:
        _src = _fh.read()
    for _from, _imp in _IMPORT_RE.findall(_src):
        _names = [_from] if _from else [n.strip() for n in _imp.split(",")]
        for _name in _names:
            if f"{_name}.py" in _ALL_PAK and f"{_name}.py" not in _PAK_SET:
                raise SystemExit(
                    f"hub.spec: tools/pak/{_m} imports sibling '{_name}' which is not in PAK_MODULES. "
                    f"Add '{_name}.py' to PAK_MODULES (and check ITS imports) so the frozen hub ships it."
                )
PAK_TOOLS = [(os.path.join(PAK_DIR, f), os.path.join("tools", "pak")) for f in PAK_MODULES]

# OUR cooked lobby GameMode, so the hub can assemble CommunityLobby_P.pak on the player's machine.
# The finished pak is NOT shipped: it contains the game's LobbyHost level with four bytes changed,
# and this project does not redistribute Reissad Studio's asset bytes (the same rule build_gamemode
# and build_lobby_override already follow). Our Blueprint ships, their level is read out of their own
# install, and hub/lobbypak.py joins the two. blueprints_summary.txt rides along because
# build_lobby_override refuses to build from a cook whose verdict it cannot read.
SEED_DIR = os.path.join(SPECPATH, "lobbyseed")
LOBBY_SEED = [
    (os.path.join(dirpath, f), os.path.join("hub", "lobbyseed",
                                            os.path.relpath(dirpath, SEED_DIR)).replace(os.sep + ".", ""))
    for dirpath, _dirs, files in os.walk(SEED_DIR)
    for f in files
    if not f.endswith(".md")
]
# the icon files (window/taskbar/tray); the exe's own icon comes from hub.ico below. Filter to real
# image files: we commit from a Mac, so a stray .DS_Store in hub/assets must never get bundled.
ASSETS_DIR = os.path.join(SPECPATH, "assets")
ASSETS = [
    (os.path.join(ASSETS_DIR, f), os.path.join("hub", "assets"))
    for f in os.listdir(ASSETS_DIR)
    if f.lower().endswith((".ico", ".png")) and os.path.isfile(os.path.join(ASSETS_DIR, f))
]

# ---------------------------------------------------------------- web UI (--webui) packaging
# The default Tk build does not touch any of this: the webview is only imported when the app is
# launched with --webui (hub.webui.shell is the sole importer). But for a FROZEN --webui launch to
# work the files and native deps must already be inside the one-folder tree, so we bundle them here
# unconditionally. Two parts:
#   (1) the static asset tree (index.html, app.css, app.js, fonts/*.woff2). paths.webui_dir() reads
#       it from <_MEIPASS>/hub/webui/static when frozen, so the dest mirrors that layout exactly,
#       the same way PAK_TOOLS -> tools/pak and ASSETS -> hub/assets do above.
#   (2) pywebview + its Windows Edge WebView2 backend (pythonnet/clr). These load their backends
#       DYNAMICALLY (webview picks a platform backend at import time; pythonnet's `clr` is a
#       C-extension brought in through clr_loader), so PyInstaller's static analyser sees almost
#       none of it and it must be collected explicitly.
#
# RUNTIME DEPENDENCY (not bundled here): the Edge WebView2 *Runtime* is a machine-level component,
# not a Python package, so PyInstaller cannot and does not ship it. It is present by default on
# Windows 11 and modern Edge; on stripped Windows 10 images it may need Microsoft's Evergreen
# WebView2 bootstrapper installed separately (see docs/ui-redesign-plan.md, Risks).
WEBUI_STATIC_DIR = os.path.join(SPECPATH, "webui", "static")
WEBUI_STATIC = []
for _dirpath, _dirnames, _filenames in os.walk(WEBUI_STATIC_DIR):
    for _f in _filenames:
        if _f == ".DS_Store":                             # authored on a Mac: never bundle Finder cruft
            continue
        _src = os.path.join(_dirpath, _f)
        if not os.path.isfile(_src):
            continue
        # Preserve the tree under hub/webui/static (top level -> "hub/webui/static", fonts/ ->
        # "hub/webui/static/fonts") so paths.webui_dir() finds every file where it expects it.
        _rel = os.path.relpath(_dirpath, WEBUI_STATIC_DIR)
        _dest = os.path.join("hub", "webui", "static") if _rel == "." else os.path.join("hub", "webui", "static", _rel)
        WEBUI_STATIC.append((_src, _dest))

# Collect the webview/pythonnet native deps. collect_submodules pulls every webview backend module;
# collect_all pulls the data files, binaries (the interop DLLs) and submodules of pythonnet/clr_loader.
# Guarded per package: on the macOS/Linux authoring box pythonnet is not installed (requirements pins
# it win32-only), and collecting an absent package can raise — swallow that so the spec still runs for
# an off-Windows dry parse. On the Windows build box all three are present and fully collected.
WEBUI_DATAS = []
WEBUI_BINARIES = []
WEBUI_HIDDEN = [
    "webview",
    "webview.platforms.winforms",       # the Windows GUI backend (System.Windows.Forms via pythonnet)
    "webview.platforms.edgechromium",   # the Edge WebView2 renderer used by the winforms backend
    "clr",                              # pythonnet's runtime import name (the C-extension)
    "clr_loader",
    "clr_loader.netfx",
    "clr_loader.ffi",
]
if collect_submodules is not None:
    try:
        WEBUI_HIDDEN += collect_submodules("webview")
    except Exception as _e:
        print(f"hub.spec: collect_submodules('webview') skipped ({_e})")
if collect_all is not None:
    for _pkg in ("webview", "pythonnet", "clr_loader"):
        try:
            _d, _b, _h = collect_all(_pkg)
            WEBUI_DATAS += _d
            WEBUI_BINARIES += _b
            WEBUI_HIDDEN += _h
        except Exception as _e:
            print(f"hub.spec: collect_all('{_pkg}') skipped ({_e})")

LICENSE_DATAS = [
    (os.path.join(ROOT, "LICENSE"), "licenses"),
    (os.path.join(ROOT, "THIRD_PARTY_NOTICES.md"), "licenses"),
] + [
    (os.path.join(ROOT, "LICENSES", f), "licenses")
    for f in os.listdir(os.path.join(ROOT, "LICENSES")) if f.endswith(".txt")
]

a = Analysis(
    [os.path.join(ROOT, "hub_entry.py")],
    pathex=[ROOT],
    binaries=WEBUI_BINARIES,          # pythonnet/clr interop DLLs for the --webui backend (empty off Windows)
    datas=PAK_TOOLS + ASSETS + LOBBY_SEED + WEBUI_STATIC + WEBUI_DATAS + LICENSE_DATAS,
    # ooz (pyooz) and these stdlib modules are imported by the builder at runtime (it lives in data files,
    # so it is invisible to the analyser). builtins/sys are always present, so they are not listed.
    hiddenimports=[
        "ooz", "tkinter", "tkinter.filedialog", "tkinter.messagebox", "pystray._win32",
        "PIL.Image", "PIL.ImageDraw", "PIL.ImageFont", "winsound", "wave",
        "argparse", "hashlib", "io", "json", "os", "struct", "subprocess", "tempfile", "uuid", "zlib",
    ] + WEBUI_HIDDEN,   # pywebview + pythonnet/clr backend modules (only loaded at runtime under --webui)
    hookspath=[],
    runtime_hooks=[],
    excludes=["numpy", "pytest", "unittest", "xmlrpc"],   # urllib needs email; PIL stays (tray icon); keep the rest lean
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,  # onedir: binaries and datas go into _internal\ via COLLECT below
    name="LightsOut",
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=False,          # no console window: it is a plain GUI app
    disable_windowed_traceback=False,
    icon=os.path.join(ASSETS_DIR, "hub.ico"),
    version=VERSION_INFO,
)
coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False, upx=False,
    name="LightsOut",    # -> dist\LightsOut\LightsOut.exe + dist\LightsOut\_internal\
)
