# Authored lobby seeds

These cooked GM_CHLobby and GM_CHJoin Blueprints are authored by this project.
The client combines one class with the player's own LobbyHost level locally.
Game-owned level bytes, generated native stubs and assembled local paks are never redistributed.

The September 22, 2026 endpoint revision retains the shipped chlobby37/chjoin3
logic and the original verified cook verdict. It is an equal-width post-cook
substitution of four inline URL constants, not a fresh Unreal cook. All changed
functions were disassembled before and after; the only difference is the hostname.
The export sizes, offsets and all header bytes are unchanged. Full byte reversal
reproduces the shipped original exactly. The release proof is in
`docs/releases/2026-09-22-proxy-endpoints.json`.

| Authored file | Bytes |
| --- | ---: |
| GM_CHLobby.uasset | 8,648 |
| GM_CHLobby.uexp | 10,777 |
| GM_CHJoin.uasset | 4,718 |
| GM_CHJoin.uexp | 6,982 |

Both cache basenames change so existing installations rebuild the local seed:
`CommunityLobby_chlobby38_P.pak` and `CommunityJoin_chjoin4_P.pak`.
The local host seed SHA256 is `bfc5ed1ccf1481bc182c667b722ccb2fd3ae77a8ab1f2e86a5ab5879f2459dc8`.
The local join seed SHA256 is `153e0e934e79fecf65298c76da9ffcc022c8a1408da9d6a5c376154be75a5a62`.
These assembled paks were verified in a temporary folder using read-only game input;
they are not distributed. No game was launched or modified for this revision.

The host still stamps its session token and reporting capability, uses the native
listen-range flow, obtains its authenticated travel permit, then commits one
match-map load. Joiners retain native parent joining and participant migration
capabilities. This endpoint revision adds no gameplay behavior.
