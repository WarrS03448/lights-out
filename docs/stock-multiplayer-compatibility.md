# Original multiplayer compatibility update

Lights Out 2.8.2 and Bodybomb 1v1 1.0.3 correct a network-format mismatch introduced
by the 1v1 mode's identifier. This addresses the identified cause of original
server joins connecting briefly and returning to the lobby with custom modes
installed. Capture the Flag, Bodybomb 5v5 and Bodybomb 1v1 can remain installed
together. The original game modes and their settings are preserved.

## Updating an existing installation

1. Fully close Bodycam.
2. Update Lights Out to 2.8.2.
3. In Gamemodes, update Bodybomb 1v1 to 1.0.3. This rebuilds the combined
   installation while keeping all currently installed modes.
4. Reopen Bodycam.

The app update alone does not replace an already installed combined pak. Both
updates are required for affected installations. Players who already removed
their modes can reinstall them after updating the app. BB5 and CTF do not need
new pack versions.

## Implementation and verification

Bodycam's stock GameMode enum uses values 0 through 12. Extending it with BB1's
old identifier 16 and terminal MAX 17 changed replicated enum-byte serialization
from four to five bits. Stock peers still read four bits. The installer now uses
CTF 13, BB5 14 and BB1 15, with terminal MAX at most 15. Cached BB1 packs with
identifier 16 are translated when rebuilt. Unsupported identifiers and duplicate
identifiers fail before pak output. In-match Bodybomb's separate identity 3 and
the existing gameplay rules are unchanged.

Verification includes regression tests for every nonempty combination of the
three released modes, a real Unreal Engine 5.5 serialization probe (72 checks),
and actual local pak builds for all seven combinations. The latter preserve
every original enum entry and every original row byte in all three shared mode
tables. The new 1v1 pack reuses all 24 previously verified authored cooked files
unchanged. No retail game assets are distributed.

Actual stock-server joining, quick matchmaking, stock hosting with an unmodified
peer, and a custom 1v1 session still need multiplayer acceptance testing. The
automated results establish the corrected serialization contract; they do not
claim those live gameplay checks have been performed.

This is an unofficial community project, not affiliated with or endorsed by
Reissad Studio. Its purpose is community play, not cheating; the downloads do
not provide cheats.
