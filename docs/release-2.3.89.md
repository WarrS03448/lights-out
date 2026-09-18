# Lights Out 2.3.89

Steam players can connect their existing Lights Out account from **Settings >
Account > Connect Lights Out account**. The flow verifies the email sign-in,
requires a fresh sign-in to the same Steam account, and confirms the ownership
change with a separate emailed code. Players sign in again after connecting and
can then use either sign-in method for the same profile and existing Steam progress.

Two profiles that already have separate public progress cannot be merged.
Temporary verification sessions never replace the current gameplay identity.
An interrupted final confirmation requires a fresh sign-in to check its outcome.
Connecting is available while idle and outside a party. Disconnection and password
recovery controls remain future work.

The website's Create account navigation and footer links are red. Account guidance
now explains where to connect existing accounts. The official website's walkthrough
also states that its recording predates Lights Out account sign-in.

## Verification

Account tests cover ownership proof, preserved progress, conflict rejection,
credential cleanup and interrupted confirmation. Browser checks exercise the
connection screens in all seven supported languages, field preservation during
background updates, and desktop/mobile website links. These checks use local
fixtures and do not send mail or connect real user accounts.

Bodybomb remains at 1.0.28 and Capture the Flag at 1.0.4. The previously waived live
host-crash/migration test remains unperformed; this release does not add native
gameplay changes.

This is an unofficial community project, not affiliated with or endorsed by
Reissad Studio. Its purpose is community gamemodes, not cheating; it does not
download cheats.

Official installer: **21,394,312 bytes**, signed and timestamped by **Samuel Warren**.
SHA256: `7bcaf7bc97b973602596e26e6dcfd08b8d5a47bd4a2e324f431b4ea80b73cf1a`.
