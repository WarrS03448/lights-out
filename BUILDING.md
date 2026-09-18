# Building Lights Out

## Desktop client

Follow the Windows environment setup in the README. Build a local application
folder from the repository root:

```powershell
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm hub\hub.spec
```

The executable is `dist/LightsOut/LightsOut.exe`. Inno Setup 6 is required to wrap
that folder as an installer using `hub/installer.iss`; `hub/build_hub.bat` performs
the Windows packaging steps. No game installation is needed to package the
client. Playing or assembling a game mod does require your own compatible game.

The official Windows installer is digitally signed by **Samuel Warren**. Signing
local builds requires your own certificate and a separate signing step; the
packaging scripts do not apply the official release signature.

The optional third-party sound recordings are absent from this source release.
The desktop packaging specification includes existing files in the static tree;
it can build without those recordings. Supply your own licensed files at the
paths documented in `hub/webui/static/audio/README.md` to restore those cues.

A locally compiled app is not a byte-for-byte reproduction of the official
installer. Preserve third-party license notices when distributing builds, and
review every bundled runtime's distribution requirements. The root MIT license
does not replace those requirements.

In particular, `pyooz==0.0.8` is GPLv3-or-later and is collected by PyInstaller.
The resulting bundle must not be advertised as MIT-only; see the dependency
notice in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) before redistribution.

## Gamemodes

Authored Blueprint generators and the editor helper are published as source.
Authored cooked lobby seeds and the current BB5/CTF packs are also included, so
the desktop client can use them without an Unreal Editor installation.

Re-cooking the Blueprints requires Unreal Engine 5.5 and compatible game-class
definitions from a legally obtained local environment. This public repository
does **not** contain the extracted native headers or generated Bodycam/plugin
stubs needed to compile a complete mirror project. It therefore does not provide
a turnkey rebuild of the cooked gamemode assets. Never commit game headers,
engine/plugin binaries, stock maps, or an assembled pak containing stock assets.

`tools/pack/make_pack.py` packages the authored output of a valid local cook;
`tools/pak/build_gamemode.py` assembles mod data using the player's own game files.
The cook must end with `RESULT: OK` and have no graph errors.

## Maintainer tooling

`tools/release/publish.py` is included as source and is covered by mocked tests.
Its release commands build, change versions/catalogues, commit and push. It is
not required for development; do not run it against an official service or
deployment checkout. Configure your own origin, hosting and catalogue before
adapting it for a fork. No deployment secrets or private commit history are
included in this repository.

Known gameplay limit: removal of unauthorized lobby entrants has caused crashes
for some removed guests in prior private tests. Current source publication does
not establish that the underlying game behavior has been resolved.
