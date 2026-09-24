# Bodybomb 1v1 update, September 24, 2026

Bodybomb 1v1 pack 1.0.2 changes ranked Paintball matches to first to five wins,
with nine rounds maximum. Rounds remain two minutes, drone cooldown remains 3x,
and sides and spawns still switch every round. Bodybomb 5v5 keeps its existing
first-to-seven rules. The rules descriptions are updated in all seven languages.

The server now finds completed matches across both ladders when the background
cleanup worker asks for permission to close Bodycam. Previously, an installed
client's request without a mode could check the 5v5 records after a 1v1 match
ended and never receive that permission. Completion still requires a saved
result, participant authorization, and the existing client process checks.

Existing matches retain the rules they started with across a server restart.
New 1v1 queue entries require pack 1.0.2. The Lights Out 2.8.0 app does not need
to be replaced for this update.

The pack reuses the exact authored game logic from the published 1.0.1 pack,
including recovery. The existing installer applies the manifest's scoring
values to the native configuration. Verification checks the resulting installed
configuration, both modes, localized descriptions, completion routing,
recovery checkpoints and saved results. Actual multiplayer gameplay was not
run for this update.

Lights Out is a community project, unaffiliated with and not endorsed by
Reissad Studio. These opt-in community gamemodes are not cheats.
