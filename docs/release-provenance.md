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
| Linux client source in this tree | Published as the corresponding-source ZIP beside each Linux download (see "Linux beta" below), not in this repository's tree. |
| Linux update signing key | Kept outside source. A local build cannot produce an update that installed official Linux clients accept. |

## Linux beta

Lights Out also has a native Linux beta for 64-bit x86 PCs (x86_64). It is a
beta: it has been tested on fewer systems than the Windows release, and each
release note (`docs/releases/linux-<version>.md`) says which systems it was
tested on. The official Linux download is one ZIP,
`LightsOut-Linux-Native-<version>-x86_64.zip`, offered at lightsoutranked.com
next to the Windows download and attached to the public GitHub pre-release
`linux-v<version>` of WarrS03448/lights-out.

### Corresponding source

The Linux client's source is not in this repository's tree yet. Its
corresponding source is the ZIP published beside each Linux download,
`LightsOut-Linux-Native-<version>-source.zip`, on the website and on the same
GitHub pre-release. That ZIP holds:

- the exact source the release was built from: the app, the native renderer
  and relay, setup, the launcher, and every build recipe and lock;
- the source of the bundled GPL and LGPL components: pyooz, CPython, and the
  Ubuntu source packages of the LGPL and GPL desktop libraries;
- `SOURCE-MANIFEST.json`, which lists every file in the ZIP with its SHA-256,
  and the URL and SHA-256 of every upstream source it only refers to.

`SOURCE-MANIFEST.json` also names the commit the source was exported from. That
commit is in the private development repository. The export leaves out
private-only files, such as test-deployment helpers and third-party recordings,
and its `public_export` record lists what was left out. Nothing the release is
built from is left out.

### Identify a Linux download

The `release-source.json` asset of each `linux-v<version>` pre-release records
both ZIPs, as the artifacts `linux-native-zip` and `linux-source-zip`, with their
sizes and SHA-256 hashes. They are read from the catalogue committed at the
tagged public commit, and the website's `/catalogue.json` (key `linux`) carries
the same values. To check a download:

```sh
sha256sum LightsOut-Linux-Native-<version>-x86_64.zip
python tools/release/source_manifest.py --source-ref linux-v<version> --verify-file LightsOut-Linux-Native-<version>-x86_64.zip
python tools/release/source_manifest.py --source-ref linux-v<version> --verify-file LightsOut-Linux-Native-<version>-source.zip
```

### Linux updates

The installed Linux app updates itself from its update strip. It only offers a
build whose update record (`linux.update` in the catalogue) carries a valid
Ed25519 signature from a key pinned inside the app, and before it installs
anything it checks the download's size and SHA-256 against that signed record.
The signing key is kept outside the repository. You can always download the ZIP
again from the website and open it instead; that installs or updates too.

The same limits apply as for Windows: the hashes establish a maintainer-declared
correspondence between the source ZIP and the exact download. They do not prove
that a third party can rebuild the download byte for byte, and no independently
reproducible build or signed build attestation is claimed. The source ZIP's
README describes how the release was built.
