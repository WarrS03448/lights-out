# Authored lobby seeds

These are our cooked `GM_CHLobby` and `GM_CHJoin` Blueprints. The hub combines the selected class with the player's own `LobbyHost` level using `build_lobby_override.py`. The finished pak contains game-owned level bytes and is built locally, never redistributed with the hub.

The files were refreshed from the verified UE 5.5 Windows unversioned cook on **2026-09-17**:

| Authored file | Bytes |
| --- | ---: |
| `Bodycam/Content/GM/Gamemode/GM_CHLobby.uasset` | 7,705 |
| `Bodycam/Content/GM/Gamemode/GM_CHLobby.uexp` | 7,260 |
| `Bodycam/Content/GM/Gamemode/GM_CHJoin.uasset` | 4,439 |
| `Bodycam/Content/GM/Gamemode/GM_CHJoin.uexp` | 5,508 |

`blueprints_summary.txt` is the actual build summary copied with the refreshed host assets. Its final line is `RESULT: OK` and it contains no uppercase `ERROR` entries. Both host files are byte-identical to that cook; the existing joiner files are preserved unchanged. The full cook completed with zero errors and one existing `GM_CHLobby` warning about a pruned `GetCurrentLevelName` diagnostic node.

## Seed versions and verification

The host cache name is **`CommunityLobby_chlobby34_P.pak`**. It passes the stamped private report capability as the host travel request’s bearer credential. It retains the earlier removal of the diagnostic lobby search and legacy join arm that raced host travel and matched the observed EOS crash. The graph still writes the private reporting capability into `BodycamGI.Search String` before the parent BeginPlay/travel path. The per-match join token remains in `Session Name`. The graph's diagnostic `BUILD_TAG` intentionally remains `chlobby-31`; the cache version identifies this changed host seed. Variant cache keys also include the seed digest, so retrying the same match after an app update cannot reuse the unsafe variant.

A local host seed assembled from these assets and the installed game read-only was **18,869 bytes**, SHA256 **`c48ec7ee8b9facc0e39ace3a43b18a3bc99c14e11b2dc7658e336711adc6245d`**. Identity retargeting reproduces it byte-for-byte; the authenticated host variant preserves the original bytecode and replaces the shared capability name successfully. Disassembly retains both map NameConst sites, the report-capability assignment and parent BeginPlay, and contains no direct `FindLobbies` or `JoinLobby` calls. Source and shipped-bytecode regression tests enforce that restriction.

The join cache name remains **`CommunityJoin_chjoin2_P.pak`**. Comparing old and new join disassembly found only a regenerated latent-action UUID, with no behavior changes. It still targets `SessionToJoin (Client)` using the per-match join token and leaves search/travel to the stock parent. Join-token retargeting passed; a host-ID placeholder is not part of this established join graph.

These are build, bytecode and packaging checks. **An in-game retry must confirm the auto-host crash is resolved.** No game launch or installation was performed for this refresh; it must not be described as an in-game-tested seed. See [connection IP privacy](../../PRIVACY.md) for the host-authorization change and its regression checks.

When changing the graph, refresh the authored assets and actual summary together, repeat the packaging checks, and bump the affected cache basename for a behavioral change. Never include the stand-in `GM_Host`, `BodycamGI`, or the game's `LobbyHost` level in this seed directory.
