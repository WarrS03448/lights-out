# Authored lobby seeds

These cooked `GM_CHLobby` and `GM_CHJoin` Blueprints are authored by this project.
The client combines one class with the player's own `LobbyHost` level locally.
Game-owned level bytes, generated native stubs and assembled local paks are never
redistributed with the client or source release.

| Authored file | Bytes |
| --- | ---: |
| `Bodycam/Content/GM/Gamemode/GM_CHLobby.uasset` | 8,436 |
| `Bodycam/Content/GM/Gamemode/GM_CHLobby.uexp` | 10,432 |
| `Bodycam/Content/GM/Gamemode/GM_CHJoin.uasset` | 4,439 |
| `Bodycam/Content/GM/Gamemode/GM_CHJoin.uexp` | 5,508 |

The host assets are the verified September 18, 2026 UE5.5 Windows unversioned
**chlobby36** cook, already exercised in the retained private test. The original
host-only `blueprints_summary.txt` is retained verbatim: its older builder banner
says chlobby35, while the final compiled graph includes chlobby36's guarded native
parent call. It ends `RESULT: OK` with no uppercase `ERROR`; final build and cook
completed without compiler/cook errors. The unchanged join assets retain their
previous verified provenance. Tests inspect the actual shipped bytecode as well
as the generated source.

The cache name is `CommunityLobby_chlobby36_P.pak`; the locally assembled host
seed SHA256 is `29dd3119ddbe87808dec8af33b2e0b1d4c1e4032184c8333d9c0f94282ec3b82`.
Variant keys include the seed digest so existing cached variants are replaced.

The host stamps its session token and reporting capability, keeps native
Selected Level Name on the shooting range, and runs stock parent BeginPlay only
on its first standalone boot. In the resulting listen range it requests the
existing authenticated travel permit, then commits one match-map load. The stage
survives world changes; repeated callbacks and return-to-range cannot rearm it.
No direct FindLobbies or JoinLobby calls are introduced into the host class.

`CommunityJoin_chjoin2_P.pak` and both joiner assets are unchanged. Joiners still
use their stamped session token and the native parent join flow.

Solo tests observed one selected-map load per launch. Automated source, bytecode,
identity retargeting, installed-map packaging and connection tests passed. A
multi-client Steam test was not performed for this release, and a previously
observed native memory crash is not claimed fixed. Public host recovery after an
already-arrived host process closes is not added by this change.
