# Bomb carriers could not shoot or pick up the bomb

Reported 2026-09-26: in Bodybomb 1v1, players "are unable to shoot their guns or pick up the bomb
until they drop their weapon", consistently, across games and players. Live packs: BB1 1.0.4,
BB5 1.0.30. Both run the same bomb rule graph (`bb5_graphs.rule_assign` / `rule_logic`), so 5v5
bomb carriers were affected too. 1v1 shows it every round because the lone attacker is always the
carrier and the bomb is placed where they stand.

## Cause

Read from the game's `BP_InventoryComponent` (installed paks, after the 2026-09-25 update):

1. `SpawnSpecialItem` on an inventory that is not initialized yet only queues the class
   (`AddToPendingItems`). `BroadcastInitialization` later spawns every queued class with
   `SpawnSpecialItem(class, bShouldEquip = true)`. The caller's `false` is discarded.
2. The queued bomb is therefore equipped (`CurrentEquippedItemSlot = Special`). After
   `BroadcastInitialization`, `InternalInitializeInventory` swaps to the Primary weapon only when
   no slot is equipped, so the carrier starts holding the bomb, not a gun.
3. Our `DropBombWhenReady` then removed the bomb with `DropItem(Special)`. `DropItem` is the
   low-level half of a drop. It calls `item.Drop` and `HandleDropItem` and empties the slot, but
   never changes `CurrentEquippedItemSlot`. The game's own callers (`InternalDropItemSlot`) swap
   to the best remaining weapon afterwards. We did not, so the carrier was left empty-handed with
   the current slot pointing at an empty Special slot.
4. Picking the bomb up goes to the Special slot, which the inventory still treats as the current
   slot, so `InternalGrabItemBasedOnRules` takes its swap-by-drop path. It applies
   `GE_State_Dropping` (`Weapon.State.Dropping`, which blocks `GA_Fire_Base`, reload and weapon
   animations), plays the drop animation, and waits for the held item to drop before finishing
   the grab in `HandleItemDropped`. Nothing is held, so the drop never happens. The player keeps
   the dropping state, and the bomb stays marked `Inventory.Item.BeingPickedUp`.
5. Switching to the gun works (`CanSwap` only needs a valid target), but it cannot fire. Dropping
   the gun runs `InternalDropItemSlot` with `Weapon.State.Dropping` present, which calls
   `HandleItemDropped`. That removes the state and completes the stuck bomb grab: the workaround
   players found.

The round-start hand-out usually takes the queued path. The game's own `AC_BombRuleComponent`
polls for the bomb actor for this reason, and our `PollBomb` comment already noted that
"creation may be pending".

## Fix (BB1 1.0.5, BB5 1.0.31)

`AssignPlayerToObjective` no longer hands anything out. It remembers the carrier, clears
`bBombSpawned` and restarts `DropBombWhenReady`, then returns `CurrentBomb` (None), the same as
the game's version while its bomb is pending. On each 0.2 s tick, `DropBombWhenReady`:

- **before the hand-out:** waits for the carrier's `IsInitialized()`, then calls
  `SpawnSpecialItem(Bombe, false)`. With the inventory initialized, the bomb is spawned straight
  into the Special slot with no swap, and the current slot stays on the carrier's gun.
  `CurrentBomb` is retained on that tick.
- **after it:** drops it with `DropItem(Special)` as before, then `PlaceBomb`. The drop is skipped
  if `GetCurrentEquippedItem()` is the bomb, because dropping an equipped item with `DropItem`
  is exactly what caused this. In that case the carrier keeps the bomb, as in stock Bodybomb.
- **out of tries (10 s) without a hand-out:** the stock hand-out, `SpawnSpecialItem(Bombe, true)`,
  and no drop.

`IsInitialized` and `GetCurrentEquippedItem` are added to the `BP_InventoryComponent` stand-in
with the game's exact signatures. Both are ordinary functions on the game class, not ubergraph
thunks. Calls resolve by name, like `DropItem` and `GetItemForSlot` already do.

## Verification

- `tests/test_bomb_handout_graph.py` executes the generated graphs against a model of the decoded
  inventory. The model reproduces the stale slot for the released hand-out. The new graph keeps
  the carrier on their gun whether the inventory is still loading or already initialized, never
  drops a bomb in the carrier's hands, and falls back to the stock hand-out.
  8 of its 9 tests fail on the released graph.
- Full Python suite: 950 passed, 4 skipped. `check_graphs.py bb5` reports only the pre-existing
  duplicate `recovery_actor` link.
- Blueprint build `RESULT: OK` with every compile CLEAN and no warnings. Cook: 489 packages,
  0 errors. Editor binaries built from the same mirror source as `origin/main`.
- Disassembly of the cooked `AC_BB1BombRule` shows the new flow. Compared with the live 1.0.4 and
  1.0.30 packs, every other package's bytecode is identical apart from latent-action UUIDs and
  generated delegate names. In the bomb rules, only the hand-out code, new locals and shifted
  ubergraph entry offsets differ.
- The hub's `build_from_packs` builds the merged pak from the new packs against the installed
  game. The installed `DA_BB1`/`DA_BB5` are byte-identical to what the live packs produce.

Not yet verified in a live match: the carrier holding their gun at round start, and a normal bomb
pickup.
