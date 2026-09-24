# Signed public releases

**2.8.4 compatibility policy:** sign the app and downloadable setup, and explicitly
keep `SignedUninstaller=no`. Older clients must not encounter a newly signed Inno
uninstaller that their update gate could accept. The publisher rejects internal
signing evidence. See [the audit](releases/2.8.4-audit.md).
Passing signing checks is not antivirus clearance.

The official publisher is **Samuel Warren**. Azure Artifact Signing is already
configured and verified; do not create another account or repeat identity
verification for each release. The working process was recovered from the
September 17, 2026 **Secure installer downloads** project task.

## Client enforcement (September 23 security changes)

The updater now verifies every payload immediately before launch using Windows
Authenticode, a timestamp, the publisher name and the fixed Artifact Signing
subscriber identity EKU `1.3.6.1.4.1.311.97.951605561.555398629.748726612.577571204`.
The EKU was read from the verified 2.6.8 installer; Microsoft documents it as a
durable identity across daily certificate renewals. Catalogue data and environment
variables cannot override the client pin. Any signing identity migration must
ship a reviewed trust-policy transition before switching publishers.

The signed PE product/version must identify Lights Out and be newer than the
running client. This prevents replaying an older signed client to remove the
verification gate. A read-only Windows handle blocks modification/replacement
until process creation. Invalid, unsigned, wrong-publisher, old, unrecognized or
unverifiable updates do not run. This applies to installer and legacy launch paths.
The checks become active only after the patched client is installed.

See `hub/update_trust.py`, `tests/test_update_security.py` and
[Microsoft certificate management](https://learn.microsoft.com/en-us/azure/artifact-signing/concept-certificate-management).

## Release procedure

1. Prepare the release in a clean checkout based on current production. Run the
   applicable client, server, browser and cooked-asset tests. Keep private test
   accounts, endpoints, configuration and game-owned assets out of the release.
2. Ensure Azure CLI is signed in to the existing signing account. Its normal
   credential cache is used; never put access tokens or client secrets in source,
   signing metadata or logs. If authentication expires, complete Azure CLI login
   interactively with the account that has Certificate Profile Signer access.
3. Run `python tools/release/publish.py hub --bump patch --no-deploy`. The publisher
   checks signing configuration, builds the app, signs/verifies `LightsOut.exe`,
   runs the frozen self-check including native signature verification, and builds
   the installer with Inno's signing callback. Inno signs only the final installer;
   internal components retain the unsigned status of previous public releases.
   The builder clears prior signing evidence and uninstaller caches, then requires
   exactly one callback artifact, byte-identical to the final installer. It verifies
   that installer without signing it twice. A failed signing or verification step stops
   before staging the download or updating its catalogue entry.
4. Review the final release diff and tests, commit the intended source and
   release artifacts, and publish through the deployment repository. Hashes and
   sizes in the catalogue must come from the **signed final installer**. Never
   rebuild or alter the installer after the catalogue is produced.
5. Verify the exact production commit deployed. Download the complete installer
   through `/hub/download`, compare its SHA256/size with the catalogue, and run
   `powershell -NoProfile -File tools/release/sign.ps1 -VerifyOnly -Path <download>`.
   It must have a valid signature, a timestamp and the expected publisher.
6. Update the independent public source repository using its established
   sanitization, licensing and asset exclusions. Preserve synthetic test IDs;
   run source/secret checks and the public test suites. Do not push private
   deployment history into the public repository.

`hub/build_hub.bat` remains a local development build, not a public release path.
Use the publisher above for signed public builds. Private diagnostic builders are
separate and are not automatically promoted to the public service.

## Existing signing setup

On the release PC, the nonsecret configuration is outside the repository:

- Metadata: `%LOCALAPPDATA%/LightsOut/signing/metadata.json`
- Account/profile: `lightsout-signing` / `lightsout-public`, Public Trust
- Region/endpoint: East US, `https://eus.codesigning.azure.net`
- Dlib: `%LOCALAPPDATA%/Microsoft/MicrosoftArtifactSigningClientTools/Azure.CodeSigning.Dlib.dll`
- SignTool: latest installed numeric Windows SDK under
  `%ProgramFiles(x86)%/Windows Kits/10/bin/<version>/x64/signtool.exe`
- Azure CLI: `%ProgramFiles%/Microsoft SDKs/Azure/CLI2/wbin/az.cmd`

The metadata selects Azure CLI authentication using `ExcludeCredentials` for
other providers. The current configuration contains no access token.
`tools/release/sign.ps1` adds the installed CLI directory to its process PATH.
Override paths with `HUB_SIGN_SIGNTOOL`, `HUB_SIGN_DLIB`, `HUB_SIGN_METADATA`;
The official publisher is Samuel Warren. Forks using another identity must also
review the durable subscriber EKU pin in the signing script and client verifier;
changing `HUB_SIGN_PUBLISHER` alone cannot create an official release.
Do not change the app installation ID.

The signing command uses SHA256, the Azure dlib and metadata, and the Microsoft
timestamp service `http://timestamp.acs.microsoft.com`. Verification uses
SignTool `/pa /all`, Windows Authenticode status, expected publisher/subscriber EKU and a
timestamp certificate. A valid signature identifies the publisher; it does not
promise that every SmartScreen reputation warning disappears.

Microsoft reference:
https://learn.microsoft.com/en-us/azure/artifact-signing/how-to-signing-integrations

## Player-side update verification (2.8.4)

The app calls Windows WinVerifyTrust directly. It starts no PowerShell process.
Only the signer and timestamp from the verified native trust state are accepted;
publisher, subscriber EKU, embedded product/version, executable role and downgrade checks remain
mandatory. The executable is held read-only against replacement until process
creation. Certificate revocation must be verifiable using Windows' trust policy.
Release signing scripts run only on the build PC.

Product/version are read from the executable's PE resource section, without
external MUI resources. Multiple signatures/signers and missing or invalid
timestamps fail closed. Windows integration tests exercise genuine signed
releases, PE and compressed-payload tampering, removed/forged timestamps, wrong
publishers, and replacement attempts. The signed frozen self-check tests the
same verifier inside the packaged application.

The signed FileDescription must identify an installer for `inno-setup` or the
portable application for legacy launches. Inno's uninstaller has a higher internal
file version and the same product name; those fields alone do not make it an
update. The role check blocks a signed renamed uninstaller in the new client.
Older clients do not have that check, so `SignedUninstaller=no` remains mandatory
until a separately reviewed compatibility transition permits internal signing.

Inno references: [SignedUninstaller](https://jrsoftware.org/ishelp/topic_setup_signeduninstaller.htm),
[SignTool](https://jrsoftware.org/ishelp/topic_setup_signtool.htm).
