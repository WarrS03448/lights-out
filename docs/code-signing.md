# Signed public releases

The official publisher is **Samuel Warren**. Azure Artifact Signing is already
configured and verified; do not create another account or repeat identity
verification for each release. The working process was recovered from the
September 17, 2026 **Secure installer downloads** project task.

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
   runs the frozen self-check, builds the installer with the signed app inside,
   then signs/verifies the installer. A failed signing or verification step stops
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
other operators can set `HUB_SIGN_PUBLISHER` to their own verified identity.
The official default is Samuel Warren. Do not change the app installation ID.

The signing command uses SHA256, the Azure dlib and metadata, and the Microsoft
timestamp service `http://timestamp.acs.microsoft.com`. Verification uses
SignTool `/pa /all`, Windows Authenticode status, expected publisher and a
timestamp certificate. A valid signature identifies the publisher; it does not
promise that every SmartScreen reputation warning disappears.

Microsoft reference:
https://learn.microsoft.com/en-us/azure/artifact-signing/how-to-signing-integrations
