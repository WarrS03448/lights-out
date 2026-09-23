# Verification of the public security audit

Status: fixes implemented and tested locally; **not published or deployed**.
The currently published Windows release remains 2.6.8. No live exploitation,
player-account access, production-data mutation or game test was performed.

## Findings and fixes

1. **Steam login — confirmed.** Both callbacks verified the assertion with Steam
   but did not bind its signed return URL to the pending Lights Out transaction.
   The comment claiming Steam performed that application-specific check was
   incorrect. Regressions reproduced acceptance of a foreign return URL with a
   successful upstream-verification stub, and concurrent callback reuse. This
   verifies the application flaw; it is not evidence of an actual account breach.
   The shared validator now checks the exact stored callback, request hostname,
   namespace/mode, Steam provider, matching identity, mandatory signed fields,
   duplicate parameters and fresh nonce. Admin login also requires browser-bound
   state; alias login URLs first redirect to the canonical site. Redis Lua claims
   and desktop token pickup are atomic. Configured storage errors fail closed.

2. **Updates — confirmed.** The release publisher already signed and verified
   application/installer files, but the client launch path had no equivalent
   check. Hashes from the same compromised catalogue are insufficient. Both
   client launch paths now share Windows Authenticode verification, timestamp and
   pinned publisher identity checks. The pin is Microsoft's durable identity EKU,
   not a daily-renewed certificate thumbprint. Signed product/version checks
   refuse old releases, preventing downgrade to an unprotected updater. The file
   is locked against writes/replacement until process creation. No platform or
   verification failure falls back to unchecked execution. Existing installations
   gain this protection only after installing the patched client.

3. **Pillow — confirmed affected version.** The 11.3.0 pin is affected by the
   cited PSD memory-corruption advisory; the patch floor is 12.2.0. Updated to
   current stable 12.3.0. The old source comment cited build consistency as the
   reason for the pin. This review does not claim a demonstrated exploit through
   a currently reachable image source. The complete client/image tests and
   frozen build passed with the new dependency.

4. **Privacy wording — confirmed.** About's no-account/no-data claims contradicted
   the current service. Replaced them with a summary of online sign-in, stored
   identities/activity/messages/diagnostics, public information, retention and
   deletion requests. The existing policy's major retention claims match the
   software defaults. Added missing Steam/admin session and login lifetimes,
   and disclosed the new short-lived essential admin login cookie.
   Account deletion remains a manual privacy-request process, not a newly created
   self-service feature. Uninstall/sign-out do not delete server records, and
   provider processing/retention is separately disclosed.

5. **Source/download transparency — confirmed limitation, not an exploit.**
   The public snapshot intentionally substitutes unavailable hosted moderation
   providers, omits certain website features and excludes licensed/game-dependent
   inputs. Its README release numbers were stale. Updated the public README,
   BUILDING and source manifest disclosures, and added
   [source/download correspondence](release-provenance.md). The versioned 2.6.8
   record ties the full public commit to exact installer and pack hashes. A tested
   helper generates future records from immutable committed catalogues and
   verifies complete downloaded files. This is not a claim of reproducible builds
   or an independent build attestation. Linux remains a separate private pilot;
   a reported completed match does not establish general Linux support.

## Verification

- Failing-before/fixed-after login and update regression checks.
- `npm run test:openid`: 15 tests, including real Redis-compatible Lua execution,
  concurrent claims/polls, nonce reuse, provider refusal, malformed callbacks,
  browser state and unavailable storage. Public-origin suite also checks alias
  redirects and trusted callback configuration.
- Full implementation client suite: **840 passed, 26 subtests passed**.
- Full public-source client suite: **839 passed, 2 skipped, 26 subtests passed**.
  The skips need the historical signed installer, intentionally excluded from the
  public repository; those real-binary checks passed in the implementation tree.
- A concurrent rerun of both client suites hit the existing detached Windows
  cleanup test's six-second final-status deadline (`verifying_exit` rather than
  `done`). The unchanged test passed in isolation and the final full suites
  passed when run separately. The scheduling-sensitive failure is recorded;
  cleanup runtime and its test were not changed by this audit.
- Full server `npm test` passed in both trees. The first implementation run had
  one environment failure (`nodemailer` absent in the fresh checkout); installing
  the existing lockfile dependencies and rerunning the full suite resolved it.
- Actual Windows checks accepted the existing signed release and rejected a
  modified copy, unsigned files, another publisher's executable and signed
  downgrade attempts. No installer was executed. File replacement was refused
  while the verification handle was held.
- Local PyInstaller build passed. Frozen self-check exited 0 (11 screens,
  82 verbs); archive inspection confirms Pillow **12.3.0** and both updater
  modules. This is a local development package, not a published installer.
- About and Privacy checked at 390, 800 and 1440 pixels; screenshots inspected,
  no horizontal page overflow, retention/deletion links present. Existing square
  styling retained; no live client controls changed.
- Independent read-only security review identified the downgrade and alias-login
  cases during development; both were fixed and regression-tested. Final review
  reported no remaining material findings in the reviewed changes.

## References checked September 23, 2026

- [OpenID 2.0 assertion verification](https://openid.net/specs/openid-authentication-2_0.html#verification)
- [Pillow advisory GHSA-pwv6-vv43-88gr](https://github.com/python-pillow/Pillow/security/advisories/GHSA-pwv6-vv43-88gr)
- [Pillow release notes](https://pillow.readthedocs.io/en/stable/releasenotes/)
- [Microsoft durable signing identity and timestamps](https://learn.microsoft.com/en-us/azure/artifact-signing/concept-certificate-management)

Publication remains a separate step: build a new version through the official
signed release publisher, deploy server/pages, synchronize the reviewed public
source, create its immutable source/download record and verify the full live
downloads. The old 2.6.8 installer must not be presented as containing these fixes.
