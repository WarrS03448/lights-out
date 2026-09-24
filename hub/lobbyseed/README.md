# Authored lobby seeds

These cooked GM_CHLobby and GM_CHJoin Blueprints are authored by this project.
The client combines one class with the player's own LobbyHost level locally.
Game-owned levels, native stubs and assembled local paks are never distributed.

The September 24, 2026 revision carries the private per-match completed-round
recovery configuration through the existing host and join flow. Both classes
come from the audited clean Unreal 5.5 cook. The host still uses one native
listen-range flow, authenticated travel permission and one committed map load.
The traveling host class does not perform a parallel native search or join.

Both cache basenames change so existing installations rebuild their local seed:
`CommunityLobby_chlobby39_P.pak` and `CommunityJoin_chjoin5_P.pak`.
The locally assembled host seed SHA256 is
`7b0dea9727c4d1aae49cf91225bbc225c2679c0e28062601a1ae4b2dbbd62d26`.
The locally assembled join seed SHA256 is
`7f29e000ae6723bc554f937ba138aeb5c79490ec4db7fe96ba382e5e735dd0e7`.
These paks were built using read-only game input; they are not distributed.
The host identity retarget reproduces the input bytes exactly. No real game
was launched or modified for this build; native multiplayer recovery is not
established by compilation, cooking or bytecode checks.
