# Lights Out

An open-source Windows community client and matchmaking service for **Bodycam**.
Install community gamemodes, sign in with Steam, play with friends, queue for
Bodybomb 5v5, and review match results and ranks.

**Lights Out is an unofficial community project, not affiliated with or endorsed
by Reissad Studio. Its purpose is community gamemodes, not cheating. The content
it downloads into the game is not designed to provide cheats.** This is not an
anti-cheat guarantee. A separately purchased, compatible Bodycam installation is
required to play.

[Download the Windows app](https://lightsout.up.railway.app/)
· [Source releases](https://github.com/WarrS03448/lights-out/releases)
· [Report a bug](https://github.com/WarrS03448/lights-out/issues)

## What is included

This public source release corresponds to **Lights Out 2.3.86**,
**Bodybomb 5v5 1.0.27**, and **Capture the Flag 1.0.4**.

| Folder | Contents |
| --- | --- |
| `hub/` | Python desktop client, local web bridge, HTML/CSS/JavaScript interface |
| `server/` | Node.js website, Steam authentication, matchmaking, ratings and analytics |
| `gamemodes/` | Authored manifests and translations |
| `mirror/Bodycam/Scripts/` | Authored Blueprint graph generators |
| `mirror/Bodycam/Source/BodycamMirrorEditor/` | Authored Unreal Editor graph-building helpers |
| `tools/pak/`, `tools/pack/` | Pak reading, local assembly and pack creation |
| `hub/lobbyseed/`, `packs/` | Authored cooked Blueprints and pack test fixtures |
| `tests/`, `server/scripts/test*` | Automated tests |

Game assets, extracted game headers, generated third-party native stubs, private
operations history, credentials and player data are not part of this repository.
Game levels, artwork and other stock assets are read from the player's own install.
The borrowed UI click/report/map-ban recordings are omitted; these optional cues
are silent in a source build unless you supply appropriately licensed replacements.

## Run the website and service locally

Install Node.js **20 or newer**. The server has no npm runtime dependencies.

```sh
cd server
npm start
```

Open <http://localhost:8081>. See [server/README.md](server/README.md) for
configuration and the limits of running without external services.

## Run the desktop client from source

Use Windows with Python and Microsoft's Edge WebView2 Runtime. The published
Windows build used Python 3.14.6. From the repository root in PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r hub\requirements.txt
# Use a separate state folder and a local service while developing.
$env:HUB_STATE_DIR = Join-Path $env:TEMP 'lights-out-dev'
$env:HUB_API_BASE = 'http://127.0.0.1:8081'
$env:HUB_CATALOGUE_URL = 'http://127.0.0.1:8081/catalogue.json'
.\.venv\Scripts\python.exe -m hub
```

Start the local server first. Its bundled catalogue describes the official release
and links to the official downloads; replace the catalogue URLs and hashes when
hosting your own builds. Without the environment overrides, the client connects
to the official service. Do not use the official service for automated testing.

## Test

```powershell
.\.venv\Scripts\python.exe -m pip install pytest
.\.venv\Scripts\python.exe -m pytest -q
cd server
npm test
```

These suites use local fixtures and temporary servers. Additional browser tests
require Playwright and Chromium; playback checks also need licensed audio fixtures.
Unreal compilation and real multiplayer testing
are separate checks; passing unit tests does not establish ten-player reliability.

## Build and contribute

See [BUILDING.md](BUILDING.md), [CONTRIBUTING.md](CONTRIBUTING.md) and
[SECURITY.md](SECURITY.md). This public source repository does not deploy the
official service when a contributor pushes a change.

## Connection privacy

The application does not record player connection IP addresses. Host travel uses
a private per-match credential; diagnostics retain only selected protocol and
gameplay fields. Hosting and relay providers still process network addresses.
See [PRIVACY.md](PRIVACY.md) for the audited paths, automated checks and limits.

## License

Original Lights Out code and documentation are available under the [MIT
License](LICENSE). Bundled fonts and the CityHash implementation retain their
upstream notices; dependencies and the game have separate licenses. See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). The MIT license grants no rights
to Bodycam, Reissad Studio's assets, or third-party trademarks.
