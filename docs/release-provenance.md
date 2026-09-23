# Source, downloads and build limits

Lights Out is an unofficial community gamemode project, not affiliated with or
endorsed by Reissad Studio. Its purpose is community play, not cheating.

## Identify a download

The **2.6.9 security release** includes a machine-readable
[release-source.json](https://github.com/WarrS03448/lights-out/releases/download/v2.6.9/release-source.json)
asset with its exact public source commit and installer/pack SHA256 hashes.
The tag `v2.6.9` identifies that commit. Download and compare the complete file;
the signed Windows installer is the official client download.

### Historical 2.6.8 correspondence

The public tag **v2.6.8** resolves to commit
`123d38ad00eb45f393f4fc132bb86cddfe4db768` in
[WarrS03448/lights-out](https://github.com/WarrS03448/lights-out/tree/123d38ad00eb45f393f4fc132bb86cddfe4db768).
Its committed catalogue records these exact downloads:

| Artifact | Bytes | SHA256 |
| --- | ---: | --- |
| LightsOut-Setup-2.6.8.exe | 24083128 | `3b122050f169bb0bab859a6fd92fe05792816e6a127ad73c14d8d02d158694b0` |
| BB5-1.0.29.zip | 73321 | `3c1aca5d2d8a02471f547dde8ca98e078fd0b7b5292e54c24ac88e2e9587e3cf` |
| CTF-1.0.4.zip | 32874 | `e0ef3843e4daa9ea9d4e4f092bfdd3b4ec937173692e2c5dece1343c12fd6f60` |

The machine-readable record is [releases/2.6.8.json](releases/2.6.8.json).
Compare a complete downloaded file with that record, rather than relying on its
filename. On Windows, `Get-FileHash -Algorithm SHA256 <file>` computes its hash.
The installer also carries Samuel Warren's timestamped Authenticode signature.
Old installer URLs may stop being served when a new release replaces them; the
source tag and recorded hashes still identify the old release.

The September 23 security changes are included in **2.6.9**. The v2.6.8 record
identifies the historical release; its installer does not contain those fixes.

## What this proves

The tag/commit and hashes establish a maintainer-declared correspondence between
a source snapshot and exact distributed bytes. They do **not** prove that a
third party can reproduce the installer byte for byte. Signing, build tools,
bundled dependencies, optional licensed recordings, and the cooked assets all
affect the result. No independently reproducible build or signed build
attestation is claimed.

For each future public release, after its source commit is fixed, generate and
include a versioned record with the release assets:

```powershell
python tools/release/source_manifest.py --source-ref <full-public-commit> --output release-source.json
python tools/release/source_manifest.py --source-ref <full-public-commit> --verify-file <downloaded-installer>
```

The tool reads the catalogue from the selected immutable commit, not the working
tree. Verify the final signed installer and both packs against the record. The
release tag must identify that source commit. Keep older versioned records.
This tool neither publishes a release nor changes a catalogue.

## Intentionally unavailable source/build inputs

The public repository includes the desktop client, ordinary authentication,
matchmaking, rating and social services, authored gamemode generators, and cooked
gamemode output. It is not an exact copy of the hosted service or deployment.

| Material | Public-build behavior or reason for omission |
| --- | --- |
| `server/fair-play.cjs` | Public adapter reports hosted evidence review unavailable; no enforcement. |
| `server/suspicion.cjs` | Public adapter reports review scoring unavailable/incomplete; no score or enforcement. |
| `server/cheater-restitution.cjs` | Hosted moderation/result-correction operations are unavailable. Ordinary ban persistence and correction-status annotations remain. |
| Live website leaderboard implementation | Absent from v2.6.8's public snapshot, including its anonymous server route and page assets. This is a disclosed source/hosted-site difference. |
| Production credentials, player records, private deployment settings and operations history | Never part of the public source or a local build. Generic configurable private-mode helpers are not actual deployment settings. |
| Third-party UI recordings | Omitted for licensing; provide independently licensed replacements or use silent optional cues. |
| Extracted game headers, generated Bodycam/plugin stubs, Unreal/game binaries and stock assets | Not redistributed. Re-cooking requires a legally obtained compatible Unreal/game environment. The repository is not a turnkey rebuild of the cooked gamemodes. |
| Official signing credentials | Kept outside source. A local build cannot acquire the official publisher signature. |

## Linux testing

The public downloadable client is a Windows release. A separate private Linux
pilot has package, installation and automated tests, and a tester has reported
completing a Steam/Proton bot match. Follow-up checks of game shutdown and restart
remain outstanding. That limited evidence does not establish general Linux
support, compatibility across distributions, or equivalence to the Windows
release. The private pilot is not included in the public v2.6.8 source snapshot.
