# Lights Out 2.3.88 and Bodybomb 5v5 1.0.28

Players can create a Lights Out email/password account in the app or at
[lightsoutranked.com/account](https://lightsoutranked.com/account), and choose
Lights Out or Steam sign-in. New Lights Out sign-ins require an emailed code;
Remember sign-in persists on that device until sign-out or security revocation.
Passwords require at least six characters and are stored as salted scrypt hashes.
The active Bodycam Steam identity is verified independently of the chosen sign-in
method so auto-host, join, team sorting, roster checks and kicks use game identities,
while rank and progress remain on the player profile.

Account ownership APIs prevent arbitrary profile merging. User-facing linking,
disconnection and password-recovery controls remain pending and ownership changes
are disabled on the public service. Creating a separate account does not migrate
an existing Steam profile’s progress. Existing Steam players should wait for
account-link management if they want to keep that profile under a new sign-in.

Confirmed missing human players receive five minutes to reconnect. At expiry,
the existing missed-match queue cooldown and RR deduction apply once, and the
match can resume. Ordinary death and respawn do not reopen the human-ready gate.
Host migration preserves the match roster and teams, rotates reporting authority,
and allows the original host to rejoin as a player. Restored matches count as
ranked results when valid final evidence is available. Final scoreboards accept
confirmed remaining players, preserve all original participants and mark missing
statistics as incomplete rather than inventing them.

Privacy, storage, terms, FAQ, account pages and app disclosures now describe
email delivery through Resend, game identity verification, remembered sessions,
retention, unlinking, reports and account-aware support requests. The app provides
Privacy and Terms links beside its sign-in controls in all seven languages.

## Verification

- Full production Python suite: 743 tests and 26 subtests passed before the final
  disclosure addition; 157 affected client tests passed afterward.
- Public source Python suite: 747 tests and 26 subtests passed.
- Full production server suite passed, including 247 lifecycle checks and 75
  account tests. Migration and reconnect tests exercise real Redis Lua semantics.
- Browser checks covered 112 match-flow states, account creation in seven languages
  at mobile and desktop widths, policy links and equal-size sign-in buttons.
  Local account browser checks use fake replies and send no real mail.
- Blueprint generation ended with RESULT: OK; cook completed with no errors or
  warnings. Modified cooked functions were disassembled and checked.
- Host/join seed and map retargeting, replacement and cleanup checks passed in
  scratch game directories. Production packages contain no private bot override.
- Packaged app self-check and timestamped application/installer signatures passed.

The live host-crash/migration test was explicitly skipped. Native migration is
implemented but has not been validated in a real multi-player host crash for this
release. Automated tests do not establish ten-player runtime reliability.

Official signed installer: **21,379,320 bytes**, signed by **Samuel Warren**.
SHA256: `d4eb9db0254aa58f1a144f47c5df64ae8fe25b28daebc538b4c2a987a1436777`.
Bodybomb pack SHA256: `7891a9b49edcda712e8a03bf09dd0213ca94afe3cd5afb961c5bf85f91e9ba0f`.
Capture the Flag remains at 1.0.4.

This is an unofficial community project, not affiliated with or endorsed by
Reissad Studio. Its purpose is community gamemodes, not cheating; it does not
download cheats.
