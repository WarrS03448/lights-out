#!/usr/bin/env python3
"""build_gamemode.py — build ONE merged `~mods` pak from any number of community gamemode manifests.

Everything here reproduces the recipe proven in-game on 2026-09-14 (Tests #3b, #4, #7b, #8, #10):
per mode M (manifest "id"):
  GM/DATA/DataAsset/DA_<M>              clone of the base mode's GameModeConfigDataAsset, score limit patched
  GM/Gamemode/GM_<M>                    clone of the base GameMode class, class-default GameModeConfig -> DA_<M>
  UI/Menus/Play/Cards/Data/DT_UI_<M>Maps   one row per map -> DA_<M>_<Map>
  UI/MetaData/DA_<M>_<Map>              clone of the stock map metadata, LevelName "<M>_<L>"
  GM_Maps/Community/<M>/DM_<M>_<L>      clone of the stock DM_<L> level, World Settings.DefaultGameMode -> GM_<M>_C
merged across all modes (one override each):
  DT_UI_CustomGameModes / DT_GameModeData / DT_GamemodeInfo   + one row per mode under key <M> (base mode's row cloned)
  Bodycam/AssetRegistry.bin             stock registry + one World record per community level (else servertravel can't find it)

Manifest (JSON):
{ "id": "CommunityTest", "base": "DeathMatch", "title": "COMMUNITY TEST MODE", "description": "...",
  "players": {"min": 1, "max": 7}, "estimate_minutes": 10, "rules": {"score_limit": 5}, "maps": ["BombHouse", "CQB"] }
Map ids are the row keys of DT_UI_DeathmatchMaps (BombHouse, CQB, Hospital, Paintball, Pool, Russian, Backrooms, Wornhouse, Airsoft, Trenches, Rome, ...).

Usage: python3 build_gamemode.py --paks <Bodycam/Content/Paks> --work <workdir> --out <Name_P.pak> manifest.json [manifest2.json ...]
Only base "DeathMatch" is implemented (the only stock pattern verified so far).
Each mode gets its OWN GameMode enum value (13..15; override with "enum"): the game keys the post-match map vote,
max players, win info and the lobby label off the enum (first matching row wins), so reusing DeathMatch's value would hand
all of that to the stock mode (found 2026-09-14: the vote after a community match offered stock maps).

TWO enums, and they are not the same number (decoded 2026-09-16, see docs/bodybomb-5v5.md "The two enums"):
  * the CARD ROW's GameMode byte  -> BodycamGI.Gamemode (UpdateSession StartGame) -> TravelToMap's level-name prefix,
    FindGMMaxPlayerFromEnum, the lobby attribute. This is the one that must stay unique per community mode.
  * the CLASS CDO's GamemodeForCompatibility -> GT_Base.SetGameModeInfo -> GameState.GameMode. THIS is what the in-world
    gameplay reads: BombZone destroys itself at BeginPlay unless it is 3 (BodyBomb) or 12 (Wingman), and GT_Bodycam's
    round/timeout switches have a case per stock mode and a default that does nothing.
Manifest/base key "cdo_enum" sets the second one alone (default: same as "enum"). A mode that wants a stock mode's
in-world behaviour - BB5 IS Bodybomb - sets cdo_enum to that stock value while keeping its own card enum.
"""
import argparse, hashlib, json, os, struct, sys, uuid
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paklib, dtdump
from pakfile import PakSet
from pkgedit import CookedPackage, case_preserving_hash, non_case_preserving_hash
from assetregistry_add import Registry
import unversioned as uv

C = "Bodycam/Content/"
BASES = {"DeathMatch": {"prefix": "DM_", "enum": 2, "ui_row": "DeathMatch", "gmd_row": "DeathMatch", "gmi_row": "Deathmatch",
                        "class_pkg": "/Game/GM/Gamemode/GM_Deathmatch", "class": "GM_Deathmatch_C",
                        "config_pkg": "/Game/GM/DATA/DataAsset/DA_GameModeDeathmatch", "config": "DA_GameModeDeathmatch",
                        "maps_table": "/Game/UI/Menus/Play/Cards/Data/DT_UI_DeathmatchMaps", "level_dir": "/Game/GM_Maps/DeathMatch"},
         "TeamDeathMatch": {"prefix": "TDM_", "enum": 8, "ui_row": "TeamDeathMatch", "gmd_row": "TeamDeathMatch", "gmi_row": "TeamDeathMatch",
                        "class_pkg": "/Game/GM/Gamemode/GM_TeamDeathMatch", "class": "GM_TeamDeathMatch_C",
                        "config_pkg": "/Game/GM/DATA/DataAsset/DA_GameModeTeamDeathmatch", "config": "DA_GameModeTeamDeathmatch",
                        "maps_table": "/Game/UI/Menus/Play/Cards/Data/DT_UI_TeamDeathmatchMaps", "level_dir": "/Game/GM_Maps/TeamDeathmatch"},
         "HardPoint": {"prefix": "HP_", "enum": 11, "ui_row": "HardPoint", "gmd_row": "Hardpoint", "gmi_row": "HardPoint",
                        "class_pkg": "/Game/GM/Gamemode/GM_Hardpoint", "class": "GM_Hardpoint_C",
                        "config_pkg": "/Game/GM/DATA/DataAsset/DA_Hardpoint", "config": "DA_Hardpoint",
                        "maps_table": "/Game/UI/Menus/Play/Cards/Data/DT_UI_HardpointMaps", "level_dir": "/Game/GM_Maps/Hardpoint"},
         # "cooked" bases: the class + config come from the mirror project's cook output (--cooked <Saved/Cooked/Windows>) instead of stock
         # clones; rows/levels/maps are still cloned from the stock mode named here (TDM: team spawns, team HUD).
         "CTF": {"kind": "cooked", "prefix": "TDM_", "enum": 8, "ui_row": "TeamDeathMatch", "gmd_row": "TeamDeathMatch", "gmi_row": "TeamDeathMatch",
                        "class_pkg": "/Game/GM/Gamemode/GM_CTF", "class": "GM_CTF_C",
                        "config_pkg": "/Game/GM/DATA/DataAsset/DA_CTF", "config": "DA_CTF",
                        "maps_table": "/Game/UI/Menus/Play/Cards/Data/DT_UI_TeamDeathmatchMaps", "level_dir": "/Game/GM_Maps/TeamDeathmatch",
                        "extra_dirs": ["GM/Gamemode/CTF"]},
         # Bodybomb 5v5 (Sam, 2026-09-14): the game's Bodybomb assets (BB_ levels with their bomb + bomb zones, DT_UI_BodybombMaps, the
         # Bodybomb rows) around our cooked GM_BB5 (own bomb rule component: bomb dropped at the attacker spawn; 4x drone cooldown;
         # no spectator drone). The stock Bodybomb card row is inactive in custom games (IsActive 0) -> "ui_overrides" turns ours on
         # and drops the ranked-only end-of-match tab. "tags" = the base mode's rules container (GM_BodyBomb: friendly fire on).
         # "cdo_enum": 3 = the mode IS Bodybomb to the in-world gameplay (bomb zones, round-timeout winner, announcer); without it
         # every BombZone in every BB_ level destroys itself 2 s into the round and nothing can be planted (measured 2026-09-16).
         "BB5": {"kind": "cooked", "prefix": "BB_", "enum": 3, "cdo_enum": 3, "ui_row": "Bodybomb", "gmd_row": "BodyBomb", "gmi_row": "Bodybomb",
                        "class_pkg": "/Game/GM/Gamemode/GM_BB5", "class": "GM_BB5_C",
                        "config_pkg": "/Game/GM/DATA/DataAsset/DA_BB5", "config": "DA_BB5",
                        "maps_table": "/Game/UI/Menus/Play/Cards/Data/DT_UI_BodybombMaps", "level_dir": "/Game/GM_Maps/BodyBomb",
                        "extra_dirs": ["GM/Gamemode/BB5"],
                        "tags": ["Rules.Weapon.InfiniteMag", "Rules.Inventory.ApplyTiming.NextRespawn", "Rules.Damage.EnableFriendlyFire"],
                        # Sam, 2026-09-16: "for our gamemode we should always have everyone spawn in base spawn". Stock Bodybomb
                        # excludes nothing, so the scored spawn system may pick the map-wide `waiting` pool; these two tags leave
                        # only the `1` / `2` base starts. Every BB5 map has both (Rome's live in the Rome art sublevel, not Rome_BP).
                        "excluded_start_tags": ["waiting", "Drone"],
                        "ui_overrides": {"IsActive": True, "EndGameTabs_remove": ["UI.EndPlay.Ranks"]}},
         "DOM": {"kind": "cooked", "prefix": "TDM_", "enum": 8, "ui_row": "TeamDeathMatch", "gmd_row": "TeamDeathMatch", "gmi_row": "TeamDeathMatch",
                        "class_pkg": "/Game/GM/Gamemode/GM_DOM", "class": "GM_DOM_C",
                        "config_pkg": "/Game/GM/DATA/DataAsset/DA_DOM", "config": "DA_DOM",
                        "maps_table": "/Game/UI/Menus/Play/Cards/Data/DT_UI_TeamDeathmatchMaps", "level_dir": "/Game/GM_Maps/TeamDeathmatch",
                        "extra_dirs": ["GM/Gamemode/DOM"]},
         }
# Separate authored assets, with the same stock Bodybomb map and rule contracts.
BASES["BB1"] = {key: (value.replace("BB5", "BB1") if isinstance(value, str)
                     else [v.replace("BB5", "BB1") for v in value] if key == "extra_dirs"
                     else value) for key, value in BASES["BB5"].items()}

# Native ABodycamGameMode.DefaultDroneClass (BP_FPV_Drone_Spectator_C on the game's abstract GM: the free-fly drone a dead player gets
# in round-based modes). Manifest "drone_class": "spectator" -> the game's own SpectatorCamera_C instead (follows team-mates: "dead
# players go straight to spectating"), "none" -> null (native fallback), absent -> whatever the cooked class stores (inherit).
SPECTATOR_PKG, SPECTATOR_CLS = "/Game/GM/SpectatorCamera", "SpectatorCamera_C"
# UGameModeConfigDataAsset (names from the UHT header dump, 2026-09-14) = 4 unversioned structs; the 4th (LoadoutConfig) is never stored:
#   A = FPhaseConfig: #0 float PhaseDuration (BB/WM 180, VS 240), #1 bool bUseTimerForWaitingPlayers, #2 float WaitingForPlayersDuration,
#       #3 float RoundWarmupDuration (6.0 everywhere), #4 float EndRoundDuration, #5 float RespawnDelay (Zombie 3.0), #6 float RemainingTimeToStartTimerSounds
#   B = FScoringConfig: #0 int ScoreLimit (DM 40, TDM 75, HP 300, BB/VS 10), #1 int MaxPhases (BB 10, VS/Zombie -1)
#   C = FTeamConfig: #0 int TeamMaxSize (DM 0, TDM/HP/BB/VS 5, WM 2, Zombie 4), #1 int MaxPlayers (DM 7, others 10, WM/Zombie 4), #2 int TeamSwitchInterval
# In-game (v15, 2026-09-14): TenVTen with TeamMaxSize 10 / MaxPlayers 20 (+ GamemodeInfo.MaxPlayer 20 + ?MaxPlayers=20 travel option) -> lobby & tablet 20/20, teams 10v10.
_F = {i: 4 for i in range(8)}
def _nested(d, p): return uv.parse(d[p:], _F)[1]
DA_SIZES = {0: _nested, 1: _nested, 2: _nested}
# GM_*_C class-default object: #4 GamemodeForCompatibility (enum byte), #11 rules GameplayTagContainer, #12 GameModeConfig (object),
# #18 SpawnSystemComponent (object), #19 TeamManagementComponent (object; DM stores an explicit zero)
CDO_SIZES = {4: 1, 11: lambda d, p: 4 + 8 * struct.unpack_from("<i", d, p)[0], 12: 4, 18: 4, 19: 4}
TAG = b"\xc1\x83\x2a\x9e"
ENUM_PKG = "/Game/GM/DATA/Enum/GameMode"
FIRST_FREE_ENUM = 13   # stock GameMode enum: None=0 ... Wingman=12, GameMode_MAX=13
LAST_COMPATIBLE_ENUM = 15  # stock replicated FByteProperty uses four bits, including MAX


def mode_enum(manifest, index):
    """Keep menu identities within the retail game's four-bit network format.

    Released BB1 packs used 16. Translate those cached manifests as well as new
    ones, so rebuilding an existing installation fixes it without a redownload.
    This does not alter the separate in-match Bodybomb identity (3).
    """
    value = int(manifest.get("enum", FIRST_FREE_ENUM + index))
    if manifest.get("id") == "BB1" and value == 16:
        value = 15
    if not FIRST_FREE_ENUM <= value <= LAST_COMPATIBLE_ENUM:
        raise ValueError(f"{manifest['id']}: no stock-compatible gamemode identity for {value}")
    return value

# ------------------------------------------------------------------ small helpers (from the test builders)
def rename(pk, new_pkg, old_name, new_name):
    old_pkg = pk.package_name; pk.package_name = new_pkg
    for n in pk.names:
        if n[0] == old_pkg: n[0] = new_pkg
        elif n[0] == old_name: n[0] = new_name
        elif n[0] == "Default__" + old_name: n[0] = "Default__" + new_name
        else: continue
        n[1], n[2] = non_case_preserving_hash(n[0]), case_preserving_hash(n[0])
    pk.guid = hashlib.sha256(new_pkg.encode()).digest()[:16]

def add_dep(pk, export_i, imp, group="create_before_ser"):
    e = pk.exports[export_i]; order = ["ser_before_ser", "create_before_ser", "ser_before_create", "create_before_create"]
    at = e["first_dep"] + sum(e[g] for g in order[:order.index(group) + 1])
    pk.preload.insert(at, imp); e[group] += 1
    for o in pk.exports:
        if o is not e and o["first_dep"] >= at: o["first_dep"] += 1

def row_entries(b, start, props, offs):
    """One DataTable row (from dtdump.parse_table) as {property index: bytes | None}: the exact input uv.pack needs to write it back."""
    from dtrows import unversioned_header
    present, _ = unversioned_header(b, start + 8)
    return {idx: (b[start + offs[_short(props[idx]["name"])][0]: start + offs[_short(props[idx]["name"])][1]] if isp else None) for idx, isp in present}

def _short(name): return name.split("_")[0] if "_" in name else name   # UserDefinedStruct field 'IsActive_12_GUID' -> 'IsActive'

def field_index(props, key): return next(i for i, f in enumerate(props) if _short(f["name"]) == key)

def base_text(src):
    key = uuid.uuid4().hex.upper().encode() + b"\0"; s = src.encode("utf-8") + b"\0"
    return struct.pack("<I", 0) + b"\x00" + struct.pack("<i", 1) + b"\0" + struct.pack("<i", len(key)) + key + struct.pack("<i", len(s)) + s

LOC_NAMESPACE = "CommunityGamemodes"        # every community gamemode's in-game text lives under this locres namespace
CULTURES = ("de", "en", "es", "fr", "pt", "ru", "zh")   # exactly the cultures Bodycam ships (Content/Localization/Game/<c>/Game.locres)
def loc_text(ns, key, src):
    """A cooked FText (history Base) with a FIXED namespace/key, so the merged Game.locres can translate it.
    The runtime looks up (ns, key) and uses the translation only if the entry's source hash matches `src`."""
    return struct.pack("<I", 0) + b"\x00" + paklib.write_fstring(ns) + paklib.write_fstring(key) + paklib.write_fstring(src)

def verify(uasset, uexp, label, tmp):
    p = os.path.join(tmp, "_v.uasset"); open(p, "wb").write(uasset)
    v = CookedPackage.load(p, lenient=True); assert v.serialize() == uasset, label
    end = max(e["serial_offset"] + e["serial_size"] for e in v.exports)
    assert end == len(uasset) + len(uexp) - 4 == v.bulk_start and uexp[-4:] == TAG, (label, end, len(uasset), len(uexp), v.bulk_start)
    return v

def unversioned_first_value(u):
    """Offset of the FIRST property's value in an unversioned export (asserts property #0 is present)."""
    p = 0; frags = []
    while True:
        h = struct.unpack_from("<H", u, p)[0]; p += 2; frags.append((h & 0x7f, bool(h & 0x80), h >> 9))
        if h & 0x100: break
    nz = sum(v for skip, z, v in frags if z)
    if nz: p += 1 if nz <= 8 else 2 if nz <= 16 else 4 * ((nz + 31) // 32)
    assert frags[0][0] == 0 and frags[0][2] >= 1 and not frags[0][1], ("property #0 not stored plainly", frags)
    return p

def pkg_of(path): return "/Game/" + path[len(C):].rsplit(".", 1)[0]

SPAWN_SIZES = {3: lambda d, p: 4 + 8 * struct.unpack_from("<i", d, p)[0]}   # USpawnSystemComponent #3 ExcludedPlayerStartTags: TArray<FName>
def patch_spawn_tags(pk, u, tags, label):
    """Write ExcludedPlayerStartTags into the GM class's default `SpawnSystemComponent` subobject.

    The spawn system is native and scored (line-of-sight blockers, distance to enemies and corpses),
    and it has no team/side knob: the ONLY data lever is which PlayerStartTags it may not use. Stock
    maps tag their starts `1` / `2` (the two team bases), `waiting` (a pool spread over the whole map,
    19-30 per map) and `Drone`. Deathmatch and GunGame exclude `["Drone", "1", "2"]` so they spawn
    only in the pool; BodyBomb, TDM, HardPoint, Versus and Wingman exclude NOTHING, so the pool is a
    candidate for them too and a player can be placed far from either base (Sam, 2026-09-16, on Rome).
    Excluding the pool leaves only the bases. FName comparison is case-insensitive, so "waiting" also
    covers the "Waiting" spelling Airsoft and RussianBuilding use, and "Drone" covers "drone".

    Patched BEFORE the CDO, because the subobject sits after the CDO in the file and a CDO that grows
    would leave this export's recorded serial_offset pointing into stale bytes. serialize() recomputes
    every offset afterwards, so growing the export itself is safe."""
    names = [n[0] for n in pk.names]
    i = next(i for i, e in enumerate(pk.exports) if names[e["name_idx"]] == "SpawnSystemComponent")
    e = pk.exports[i]; off = e["serial_offset"] - pk.total_header_size
    old = bytes(u[off:off + e["serial_size"]])
    ent, end = uv.parse(old, SPAWN_SIZES)
    ent[3] = struct.pack("<i", len(tags)) + b"".join(struct.pack("<ii", pk.add_name(t), 0) for t in tags)
    new = uv.pack(ent) + old[end:]
    u[off:off + e["serial_size"]] = new; e["serial_size"] = len(new)
    print(f"   {label}: spawns restricted to the team bases, ExcludedPlayerStartTags = {tags}")
    return i

def prop_size(pr):
    """Serialized size (or size function) of one Blueprint-class property for unversioned parsing."""
    t = pr["type"]
    fixed = {"BoolProperty": 1, "ByteProperty": 1, "Int8Property": 1, "IntProperty": 4, "UInt32Property": 4, "FloatProperty": 4,
             "DoubleProperty": 8, "Int64Property": 8, "NameProperty": 8, "ObjectProperty": 4, "ClassProperty": 4, "SoftObjectProperty": None,
             "InterfaceProperty": 4, "EnumProperty": 1}
    if t in fixed and fixed[t] is not None: return fixed[t]
    if t in ("StrProperty",):
        return lambda d, p: 4 + (abs(struct.unpack_from("<i", d, p)[0]) * (2 if struct.unpack_from("<i", d, p)[0] < 0 else 1))
    if t == "StructProperty":
        sname = pr.get("struct_name", "")
        if sname == "PointerToUberGraphFrame": return 2     # a struct without reflected members = one empty unversioned header (00 01)
        if sname in ("Vector", "Rotator"): return 24
        if sname == "Transform": return 80
        if sname == "TimerHandle": return 8
    if t == "ArrayProperty":
        # int32 count + elements (only ever needed when a CDO stores a non-empty array; empty arrays are zero-masked)
        inner = pr.get("inner")
        def arr(d, p, inner=inner):
            n = struct.unpack_from("<i", d, p)[0]
            if n == 0: return 4
            es = prop_size(inner)
            if callable(es): raise ValueError(f"variable-size array elements not supported for CDO patching: {pr['name']}")
            return 4 + n * es
        return arr
    raise ValueError(f"unsupported own property type for CDO patching: {t} {pr['name']} {pr.get('struct_name')}")

# ------------------------------------------------------------------ the builder
class Builder:
    def __init__(self, paks_dir, work, cooked=None, allow_unverified_cook=False):
        self.ps = PakSet(paks_dir); self.work = work; os.makedirs(work, exist_ok=True); self.cooked = cooked
        if cooked and not allow_unverified_cook: self.check_cook_verdict(cooked)
        self.stock_dir = os.path.join(work, "stock"); os.makedirs(self.stock_dir, exist_ok=True)
        self.files = {}      # pak contents: full path -> bytes
        self.registry_adds = []   # (template pkg, new pkg)
        self.table_rows = {"ui": [], "gmd": [], "gmi": []}   # appended rows per table (bytes builders)

    @staticmethod
    def check_cook_verdict(cooked):
        """collect_cooked.bat copies the mirror's blueprints_summary.txt next to the cook output. A cook is usable only if that
        run ended with "RESULT: OK": graph link errors leave pins at their defaults and still compile CLEAN (v20: both CTF
        flags at the world origin), so the verdict, not the compile status, decides. --allow-unverified-cook overrides."""
        summary = os.path.join(cooked, "blueprints_summary.txt")
        if not os.path.exists(summary):
            sys.exit(f"REFUSING TO BUILD: {summary} not found. Run 2_make_blueprints.bat and 3_cook.bat (collect_cooked.bat copies the "
                     f"summary next to the cooked files), or pass --allow-unverified-cook for a cook made before that check existed.")
        text = open(summary, encoding="utf-8", errors="replace").read()
        bad = [l for l in text.splitlines() if l.startswith("ERROR")]
        if "RESULT: OK" not in text or bad:
            sys.exit("REFUSING TO BUILD: the Blueprint run that produced this cook did not end with RESULT: OK" +
                     (":\n  " + "\n  ".join(bad[:10]) if bad else "") + "\nFix the graphs, rerun 2_make_blueprints.bat and 3_cook.bat.")
        print(f"cook verdict: RESULT: OK ({summary})")

    # ---- stock access (cached on disk so dtdump & friends can take paths) ----
    def stock(self, full):
        """Extract a stock file from the player's paks, cached on disk.

        Written to a temp name and renamed, because the obvious version POISONS ITS OWN CACHE: it
        opened the destination (creating a 0-byte file) and only then called read(), so any failure
        in there - a missing Oodle decompressor, a half-copied pak - left an empty file that every
        later run happily returned, and the error surfaced as an unrelated struct.error hundreds of
        lines away (2026-09-14). A partial write now leaves no cache entry at all."""
        p = os.path.join(self.stock_dir, full.replace("/", "__"))
        if os.path.exists(p) and os.path.getsize(p) > 0:
            return p
        tmp = p + ".part"
        try:
            data = self.ps.read(full)
            if not data:
                raise ValueError("empty extraction for " + full)
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, p)
        finally:
            if os.path.exists(tmp):
                try: os.remove(tmp)
                except OSError: pass
        return p
    def stock_bytes(self, full): return open(self.stock(full), "rb").read()
    def stock_available(self, full):
        """in the paks, or already in the <work>/stock cache (files pulled by byte range from paks too big to stage)"""
        return self.ps.has(full) or os.path.exists(os.path.join(self.stock_dir, full.replace("/", "__")))
    def cooked_pkg(self, pkg, ext=".uasset"):
        """Paths of a package cooked by the mirror project (<cooked>/Bodycam/Content/...)."""
        assert self.cooked, "this base needs --cooked <mirror>/Saved/Cooked/Windows"
        base = os.path.join(self.cooked, "Bodycam", "Content", *pkg[len("/Game/"):].split("/"))
        assert os.path.exists(base + ext), f"cooked package not found: {base}{ext}"
        return base + ext, base + ".uexp"
    def src_pkg(self, base, key, ext=".uasset"):
        return self.cooked_pkg(base[key], ext) if base.get("kind") == "cooked" else self.stock_pkg(base[key], ext)
    def stock_pkg(self, pkg, ext=".uasset"): return self.stock(C + pkg[len("/Game/"):] + ext), self.stock(C + pkg[len("/Game/"):] + ".uexp")

    # ---- the GameMode enum: one new enumerator per community mode (so every enum-keyed lookup finds OUR rows) ----
    def enum_asset(self, modes):
        values = [m["_enum"] for m in modes]
        if not values or any(not FIRST_FREE_ENUM <= value <= LAST_COMPATIBLE_ENUM for value in values):
            raise ValueError("No stock-compatible gamemode enum layout")
        if len(set(values)) != len(values):
            raise ValueError("Cannot install modes with duplicate stock-compatible gamemode identities")
        ua, ue = self.stock_pkg(ENUM_PKG); pk = CookedPackage.load(ua); u = open(ue, "rb").read(); names = [n[0] for n in pk.names]
        assert u[:2] == b"\x00\x03" and u[2:6] == b"\0\0\0\0", u[:8].hex()
        p = 6; n_disp = struct.unpack_from("<i", u, p)[0]; p += 4; disp_start = p
        for _ in range(n_disp):
            p += 8 + 4 + 1   # key FName, FText flags, history (Base)
            for _ in range(3):
                ln = struct.unpack_from("<i", u, p)[0]; p += 4 + (ln if ln >= 0 else -2 * ln)
        disp_end = p
        assert u[p:p + 4] == b"\0\0\0\0", u[p:p + 8].hex(); p += 4   # UObject trailer between script properties and UEnum data
        n_names = struct.unpack_from("<i", u, p)[0]; p += 4; entries = []
        for _ in range(n_names):
            idx, num, val = struct.unpack_from("<iiq", u, p); p += 16; entries.append((idx, num, val))
        tail = u[p:-4]
        max_idx = next(i for i, (idx, num, val) in enumerate(entries) if names[idx].endswith("_MAX")); max_val = entries[max_idx][2]
        assert max_val == FIRST_FREE_ENUM, ("stock enum changed?", max_val)
        new_disp = b""; new_entries = []
        for m in modes:
            v = m["_enum"]; short = f"NewEnumerator{v}"
            new_disp += struct.pack("<ii", pk.add_name(short), 0) + base_text(m.get("enum_name", m["id"]))
            new_entries.append((pk.add_name(f"GameMode::{short}"), 0, v))
        # MAX is a terminal enum entry, not an entry count. At the last available
        # four-bit value it aliases the preceding real value. Unreal's value/name
        # lookup selects the first entry, so BB1 remains a named, valid value 15.
        # Giving MAX its usual value 16 would change ALL replicated GameMode
        # fields to five bits, even in an ordinary stock Bodycam match.
        terminal = min(max(values) + 1, LAST_COMPATIBLE_ENUM)
        entries = entries[:max_idx] + new_entries + [(entries[max_idx][0], entries[max_idx][1], terminal)]
        out = u[:6] + struct.pack("<i", n_disp + len(modes)) + u[disp_start:disp_end] + new_disp + b"\0\0\0\0" + struct.pack("<i", len(entries))
        for idx, num, val in entries: out += struct.pack("<iiq", idx, num, val)
        out += tail + TAG
        pk.exports[0]["serial_size"] = len(out) - 4
        a = pk.serialize(); verify(a, out, "GameMode enum", self.work)
        self.files[f"{C}GM/DATA/Enum/GameMode.uasset"] = a; self.files[f"{C}GM/DATA/Enum/GameMode.uexp"] = out
        print("enum:", ", ".join(f"{m['id']}={m['_enum']}" for m in modes), "MAX =", entries[-1][2])

    # ---- per-mode packages ----
    def config_asset(self, m, base):
        ua, ue = self.src_pkg(base, "config_pkg")
        da = CookedPackage.load(ua); u = bytearray(open(ue, "rb").read())
        rename(da, f"/Game/GM/DATA/DataAsset/DA_{m['id']}", base["config"], f"DA_{m['id']}")
        top, end = uv.parse(bytes(u), DA_SIZES); trailer = bytes(u[end:])
        A, _ = uv.parse(top[0], _F); B, _ = uv.parse(top[1], _F); Cc, _ = uv.parse(top[2], _F)
        rules = m.get("rules", {}); i32 = lambda v: struct.pack("<i", int(v)); f32 = lambda v: struct.pack("<f", float(v))
        if "score_limit" in rules: B[0] = i32(rules["score_limit"])
        if "team_size" in rules: Cc[0] = i32(rules["team_size"])
        mp = rules.get("max_players", m.get("players", {}).get("max"))
        if mp is not None: Cc[1] = i32(mp)
        if "time_limit" in rules: A[0] = f32(rules["time_limit"])   # seconds; only BB/VS/WM set it in stock data — semantics unverified for DM/TDM
        if "max_rounds" in rules: B[1] = i32(rules["max_rounds"])                    # FScoringConfig.MaxPhases (BB 10, VS -1)
        if "team_switch_interval" in rules: Cc[2] = i32(rules["team_switch_interval"])   # FTeamConfig.TeamSwitchInterval (sides swap every N rounds; stock: unset)
        top = {0: uv.pack(A), 1: uv.pack(B), 2: uv.pack(Cc)}
        u = uv.pack(top) + trailer
        chk, _ = uv.parse(u, DA_SIZES); assert uv.parse(chk[1], _F)[0][0] == B[0]
        assert len(da.exports) == 1; da.exports[0]["serial_size"] = len(u) - 4   # the patched struct may have grown (new fields)
        a = da.serialize(); verify(a, bytes(u), "DA", self.work)
        self.rules_applied = {"A": {k: (struct.unpack("<f", v)[0] if v else None) for k, v in A.items()}, "B": {k: (struct.unpack("<i", v)[0] if v else None) for k, v in B.items()}, "C": {k: (struct.unpack("<i", v)[0] if v else None) for k, v in Cc.items()}}
        self.files[f"{C}GM/DATA/DataAsset/DA_{m['id']}.uasset"] = a; self.files[f"{C}GM/DATA/DataAsset/DA_{m['id']}.uexp"] = bytes(u)
        return f"/Game/GM/DATA/DataAsset/DA_{m['id']}", f"DA_{m['id']}"

    def gamemode_class(self, m, base, da_pkg, da_name):
        if base.get("kind") == "cooked": return self.cooked_gamemode_class(m, base, da_pkg, da_name)
        ua, ue = self.stock_pkg(base["class_pkg"])
        gm = CookedPackage.load(ua); u = bytearray(open(ue, "rb").read())
        for a, b in zip(gm.exports, gm.exports[1:]): assert b["serial_offset"] == a["serial_offset"] + a["serial_size"]
        cls = f"GM_{m['id']}_C"; rename(gm, f"/Game/GM/Gamemode/GM_{m['id']}", base["class"], cls)
        p_imp = gm.add_import("/Script/CoreUObject", "Package", 0, da_pkg)
        o_imp = gm.add_import("/Script/Bodycam", "GameModeConfigDataAsset", p_imp, da_name)
        names = [n[0] for n in gm.names]
        cdo_i = next(i for i, e in enumerate(gm.exports) if names[e["name_idx"]] == "Default__" + cls)
        tags = m.get("excluded_start_tags", base.get("excluded_start_tags"))
        if tags:
            si = patch_spawn_tags(gm, u, tags, m["id"])
            assert gm.exports[si]["serial_offset"] > gm.exports[cdo_i]["serial_offset"], "SpawnSystemComponent must follow the CDO"
        cdo = gm.exports[cdo_i]; off = cdo["serial_offset"] - gm.total_header_size; old = bytes(u[off:off + cdo["serial_size"]])
        ent, end = uv.parse(old, CDO_SIZES); assert set(ent) <= {4, 11, 12, 18, 19} and ent.get(4) == bytes([base["enum"]]), ("unexpected base CDO layout", old.hex(), ent)
        ent[4] = bytes([m["_cdo_enum"]]); ent[12] = struct.pack("<i", o_imp)   # #4 GamemodeForCompatibility = our in-match enum, #12 GameModeConfig = our asset
        new = uv.pack(ent) + old[end:]
        u[off:off + cdo["serial_size"]] = new; cdo["serial_size"] = len(new)
        add_dep(gm, cdo_i, o_imp, "create_before_ser")
        a = gm.serialize(); verify(a, bytes(u), "GM class", self.work)
        self.files[f"{C}GM/Gamemode/GM_{m['id']}.uasset"] = a; self.files[f"{C}GM/Gamemode/GM_{m['id']}.uexp"] = bytes(u)
        return f"/Game/GM/Gamemode/GM_{m['id']}", cls

    def cooked_gamemode_class(self, m, base, da_pkg, da_name):
        """A GM_* class cooked by the mirror project: rename to GM_<id>, point its config import at DA_<id>, and write the
        mode's enum into GamemodeForCompatibility. Property indices shift by the number of the class's OWN properties
        (own props come first in the unversioned schema): enum = k+3, GameModeTags = k+10, ConfigDataAsset = k+11."""
        from classinfo import bgc_props
        ua, ue = self.cooked_pkg(base["class_pkg"])
        props = bgc_props(ua, ue); k = len(props)
        gm = CookedPackage.load(ua); u = bytearray(open(ue, "rb").read())
        for a, b in zip(gm.exports, gm.exports[1:]): assert b["serial_offset"] == a["serial_offset"] + a["serial_size"]
        cls = f"GM_{m['id']}_C"; rename(gm, f"/Game/GM/Gamemode/GM_{m['id']}", base["class"], cls)
        # retarget the cooked config reference (DA_<base> -> DA_<id>) by renaming the import's package/object names
        for n in gm.names:
            if n[0] == base["config_pkg"]: n[0] = da_pkg
            elif n[0] == base["config"]: n[0] = da_name
            else: continue
            n[1], n[2] = non_case_preserving_hash(n[0]), case_preserving_hash(n[0])
        names = [n[0] for n in gm.names]
        cdo_i = next(i for i, e in enumerate(gm.exports) if names[e["name_idx"]] == "Default__" + cls)
        tags = m.get("excluded_start_tags", base.get("excluded_start_tags"))
        if tags:
            si = patch_spawn_tags(gm, u, tags, m["id"])
            assert gm.exports[si]["serial_offset"] > gm.exports[cdo_i]["serial_offset"], "SpawnSystemComponent must follow the CDO"
        cdo = gm.exports[cdo_i]; off = cdo["serial_offset"] - gm.total_header_size; old = bytes(u[off:off + cdo["serial_size"]])
        sizes = {k + 3: 1, k + 10: CDO_SIZES[11], k + 11: 4, k + 12: 4, k + 17: 4, k + 18: 4}   # k+12 = DefaultDroneClass (native #4)
        for i, pr in enumerate(props): sizes[i] = prop_size(pr)
        ent, end = uv.parse(old, sizes)
        ent[k + 3] = bytes([m["_cdo_enum"]])
        tags = m.get("rule_tags", base.get("tags"))
        if tags:   # the rules GameplayTagContainer: int count + (FName) per tag — the mirror's stand-in parent stores none, so the child must
            ent[k + 10] = struct.pack("<i", len(tags)) + b"".join(struct.pack("<ii", gm.add_name(t), 0) for t in tags)
        drone = m.get("drone_class")
        if drone == "spectator":
            p_imp = gm.add_import("/Script/CoreUObject", "Package", 0, SPECTATOR_PKG)
            c_imp = gm.add_import("/Script/Engine", "BlueprintGeneratedClass", p_imp, SPECTATOR_CLS)
            ent[k + 12] = struct.pack("<i", c_imp); add_dep(gm, cdo_i, c_imp, "create_before_ser")
        elif drone == "none": ent[k + 12] = None
        elif drone is not None: raise ValueError(f"{m['id']}: unknown drone_class {drone!r} (spectator | none)")
        new = uv.pack(ent) + old[end:]
        u[off:off + cdo["serial_size"]] = new; cdo["serial_size"] = len(new)
        a = gm.serialize(); verify(a, bytes(u), "cooked GM class", self.work)
        self.files[f"{C}GM/Gamemode/GM_{m['id']}.uasset"] = a; self.files[f"{C}GM/Gamemode/GM_{m['id']}.uexp"] = bytes(u)
        print(f"   cooked class {base['class']} -> {cls}: {k} own propert{'y' if k == 1 else 'ies'} ({', '.join(p['name'] for p in props)}), CDO stored {sorted(ent)}"
              + (f", rules {tags}" if tags else "") + (f", drone {drone}" if drone else ""))
        # any other cooked packages the mode ships (flag/base actors, ...)
        for d in base.get("extra_dirs", []):
            root = os.path.join(self.cooked, "Bodycam", "Content", *d.split("/"))
            if not os.path.isdir(root): continue
            for dp, dn, fn in os.walk(root):
                for f in fn:
                    rel = os.path.relpath(os.path.join(dp, f), os.path.join(self.cooked, "Bodycam", "Content")).replace(os.sep, "/")
                    self.files[C + rel] = open(os.path.join(dp, f), "rb").read()
        return f"/Game/GM/Gamemode/GM_{m['id']}", cls

    def stock_map_catalog(self, base):
        """row key -> (metadata package, LevelName) from the base mode's maps table."""
        ua, ue = self.stock_pkg(base["maps_table"])
        pk = CookedPackage.load(ua); names = [n[0] for n in pk.names]; u = open(ue, "rb").read()
        n = struct.unpack_from("<i", u, 10)[0]; p = 14; cat = {}
        for _ in range(n):
            key = names[struct.unpack_from("<i", u, p)[0]]; hdr = struct.unpack_from("<H", u, p + 8)[0]; p += 10
            active = 1
            if not (hdr & 0x80): active = u[p]; p += 1     # IsActive stored (true); a zero mask means it is false and absent
            else: p += 1   # zero mask byte
            meta = struct.unpack_from("<i", u, p)[0]; p += 4
            im = pk.imports[-meta - 1]; meta_pkg = names[pk.imports[-im[4] - 1][5]]
            mua, mue = self.stock_pkg(meta_pkg); mp = CookedPackage.load(mua); mn = [x[0] for x in mp.names]; mu = open(mue, "rb").read()
            lo = unversioned_first_value(mu)
            cat[key] = (meta_pkg, mn[struct.unpack_from("<i", mu, lo)[0]], active)
        return cat

    def map_assets(self, m, base, cls_pkg, cls, key, meta_pkg, level_name):
        mid = m["id"]
        # level clone
        src_pkg = f"{base['level_dir']}/{base['prefix']}{level_name}"; ua, ue = self.stock_pkg(src_pkg, ".umap")
        lv = CookedPackage.load(ua, lenient=True); u = bytearray(open(ue, "rb").read())
        new_level = f"{mid}_{level_name}"; new_pkg = f"/Game/GM_Maps/Community/{mid}/{new_level}"   # no DM_ prefix: TravelToMap adds "" for our enum
        rename(lv, new_pkg, f"{base['prefix']}{level_name}", new_level)
        p_imp = lv.add_import("/Script/CoreUObject", "Package", 0, cls_pkg)
        c_imp = lv.add_import("/Script/Engine", "BlueprintGeneratedClass", p_imp, cls)
        lv.add_import(cls_pkg, cls, p_imp, "Default__" + cls)
        ws_i = len(lv.exports) - 1; ws = lv.exports[ws_i]; assert lv.names[ws["name_idx"]][0] == "WorldSettings"
        off = ws["serial_offset"] - lv.total_header_size; old = bytes(u[off:off + ws["serial_size"]])
        assert old[:4] == bytes.fromhex("16 02 23 03"), ("unexpected WorldSettings layout", src_pkg, old[:8].hex())
        new = bytes.fromhex("16 02 0a 02 18 03") + old[4:8] + struct.pack("<i", c_imp) + old[8:]
        u[off:off + ws["serial_size"]] = new; ws["serial_size"] = len(new)
        add_dep(lv, ws_i, c_imp, "create_before_ser")
        a = lv.serialize(); verify(a, bytes(u), "level " + new_level, self.work)
        self.files[f"{C}GM_Maps/Community/{mid}/{new_level}.umap"] = a; self.files[f"{C}GM_Maps/Community/{mid}/{new_level}.uexp"] = bytes(u)
        self.registry_adds.append((src_pkg, new_pkg))
        # metadata clone
        mua, mue = self.stock_pkg(meta_pkg); da = CookedPackage.load(mua); du = bytearray(open(mue, "rb").read())
        meta_name = meta_pkg.rsplit("/", 1)[1]; new_meta = f"DA_{mid}_{key}"
        # sanity-check LevelName BEFORE the rename: in the game's older metadata assets (UI/MetaData/Airsoft) the asset name and the
        # LevelName value share ONE name-table entry, which rename() rewrites (BB5 1.0.4 install failed on exactly this assert)
        lo = unversioned_first_value(bytes(du)); found = da.names[struct.unpack_from("<i", du, lo)[0]][0]
        assert found == level_name, ("metadata LevelName mismatch", meta_pkg, found, level_name)
        rename(da, f"/Game/UI/MetaData/{new_meta}", meta_name, new_meta)
        # LevelName is what TravelToMap feeds to `servertravel`; UE parses `?Key=Value` options off it (AGameSession::InitOptions reads ?MaxPlayers=)
        travel_name = f"{mid}_{level_name}" + "".join(f"?{k}={v}" for k, v in m.get("url_options", {}).items())
        struct.pack_into("<ii", du, lo, da.add_name(travel_name), 0)
        a = da.serialize(); verify(a, bytes(du), "metadata " + new_meta, self.work)
        self.files[f"{C}UI/MetaData/{new_meta}.uasset"] = a; self.files[f"{C}UI/MetaData/{new_meta}.uexp"] = bytes(du)
        return f"/Game/UI/MetaData/{new_meta}", new_meta

    def maps_table(self, m, base, metas):
        ua, ue = self.stock_pkg(base["maps_table"]); mt = CookedPackage.load(ua); src = open(ue, "rb").read()
        tname = f"DT_UI_{m['id']}Maps"; rename(mt, f"/Game/UI/Menus/Play/Cards/Data/{tname}", base["maps_table"].rsplit("/", 1)[1], tname)
        rs = struct.unpack_from("<i", src, 2)[0]; assert mt.names[mt.imports[-rs - 1][5]][0] == "BP_MapDefinition"
        rows = b""
        for key, (pkg, name) in metas:
            p_imp = mt.add_import("/Script/CoreUObject", "Package", 0, pkg)
            o_imp = mt.add_import("/Game/MenuSystemPro/Blueprints/UI/Types/PDA_LevelMetaData", "PDA_LevelMetaData_C", p_imp, name)
            add_dep(mt, 0, o_imp, "create_before_ser")
            rows += struct.pack("<ii", mt.add_name(key), 0) + bytes.fromhex("00 05") + b"\x01" + struct.pack("<i", o_imp)
        u = src[:10] + struct.pack("<i", len(metas)) + rows + TAG
        mt.exports[0]["serial_size"] = len(u) - 4
        a = mt.serialize(); verify(a, u, tname, self.work)
        self.files[f"{C}UI/Menus/Play/Cards/Data/{tname}.uasset"] = a; self.files[f"{C}UI/Menus/Play/Cards/Data/{tname}.uexp"] = u
        return f"/Game/UI/Menus/Play/Cards/Data/{tname}", tname

    def build_mode(self, m):
        if m.get("_cooked"): self.cooked = m["_cooked"]     # pack-based builds: every mode brings its own cooked folder (build_from_packs)
        base = BASES[m.get("base", "DeathMatch")]
        m["_cdo_enum"] = int(m.get("cdo_enum", base.get("cdo_enum", m["_enum"])))
        assert 0 <= m["_cdo_enum"] < 256, m["id"]
        da_pkg, da_name = self.config_asset(m, base)
        cls_pkg, cls = self.gamemode_class(m, base, da_pkg, da_name)
        cat = self.stock_map_catalog(base); metas = []
        lower = {k.lower(): k for k in cat}      # table keys differ in case between modes (DM 'Wornhouse' vs TDM 'WornHouse')
        for want in m["maps"]:
            if want.lower() not in lower: raise KeyError(f"unknown map '{want}'; known: {sorted(cat)}")
            key = lower[want.lower()]
            meta_pkg, level_name, active = cat[key]
            lvl = f"{base['level_dir']}/{base['prefix']}{level_name}"
            if not self.stock_available(C + lvl[len('/Game/'):] + ".umap"): raise FileNotFoundError(f"stock level {lvl} not available in the given paks or in <work>/stock (see tools/ps/extract_levels.bat)")
            metas.append((key, self.map_assets(m, base, cls_pkg, cls, key, meta_pkg, level_name)))
        table_pkg, table_name = self.maps_table(m, base, metas)
        self.table_rows["ui"].append((m, base, table_pkg, table_name)); self.table_rows["gmd"].append((m, base, da_pkg, da_name)); self.table_rows["gmi"].append((m, base))
        print(f"mode {m['id']}: base {m.get('base', 'DeathMatch')}, enum {m['_enum']}"
              + (f" (in-match {m['_cdo_enum']})" if m["_cdo_enum"] != m["_enum"] else "")
              + f", {len(metas)} map(s), class {cls}, config {da_name} {self.rules_applied}")

    # ---- merged tables ----
    def merge_tables(self):
        S = lambda p: self.stock_pkg(p)
        # UI cards
        ua, ue = S("/Game/UI/Menus/Play/Cards/Data/DT_UI_CustomGameModes"); sa, se = S("/Game/UI/Menus/Play/Cards/Data/Struct_GameModeDefinition")
        _, _, b, rows, props = dtdump.parse_table(ua, ue, sa, se); pk = CookedPackage.load(ua)
        body = b[:10] + struct.pack("<i", len(rows) + len(self.table_rows["ui"])) + b[14:len(b) - 4]
        for m, base, table_pkg, table_name in self.table_rows["ui"]:
            name, start, end, row, offs = next(r for r in rows if r[0] == base["ui_row"])
            p_imp = pk.add_import("/Script/CoreUObject", "Package", 0, table_pkg); t_imp = pk.add_import("/Script/Engine", "DataTable", p_imp, table_name)
            add_dep(pk, 0, t_imp, "create_before_ser")
            # the row as {property index: bytes | None (zero)} — re-packed below, so fields the stock row leaves at zero (Bodybomb:
            # IsActive) can be set too; rows re-pack byte-identically when nothing changes (checked on every stock table)
            ent = row_entries(b, start, props, offs); fi = lambda key: field_index(props, key)
            for f, v in (("PlayerMin", m.get("players", {}).get("min", row["PlayerMin"])), ("PlayerMax", m.get("players", {}).get("max", row["PlayerMax"])),
                         ("EstimateTime", m.get("estimate_minutes", row["EstimateTime"])), ("MapTable", t_imp)):
                ent[fi(f)] = struct.pack("<i", int(v))
            ent[fi("GameMode")] = bytes([m["_enum"]])
            ent[fi("Name")] = loc_text(LOC_NAMESPACE, f"{m['id']}.Title", m["title"]); ent[fi("Description")] = loc_text(LOC_NAMESPACE, f"{m['id']}.Description", m.get("description", ""))
            for key, val in base.get("ui_overrides", {}).items():
                if key == "IsActive": ent[fi(key)] = b"\x01" if val else None
                elif key == "EndGameTabs_remove":   # TArray<FGameplayTag>: int count + FName per tag
                    tags = ent.get(fi("EndGameTabs")) or b"\0\0\0\0"; n = struct.unpack_from("<i", tags, 0)[0]
                    keep = [tags[4 + 8 * i: 12 + 8 * i] for i in range(n) if pk.names[struct.unpack_from("<i", tags, 4 + 8 * i)[0]][0] not in val]
                    ent[fi("EndGameTabs")] = struct.pack("<i", len(keep)) + b"".join(keep)
                else: raise ValueError(f"unknown ui_override {key}")
            body += struct.pack("<ii", pk.add_name(m["id"]), 0) + uv.pack(ent)
        body += TAG; pk.exports[0]["serial_size"] = len(body) - 4; a = pk.serialize(); verify(a, body, "UI table", self.work)
        self.files[f"{C}UI/Menus/Play/Cards/Data/DT_UI_CustomGameModes.uasset"] = a; self.files[f"{C}UI/Menus/Play/Cards/Data/DT_UI_CustomGameModes.uexp"] = body
        open(os.path.join(self.work, "ui.uasset"), "wb").write(a); open(os.path.join(self.work, "ui.uexp"), "wb").write(body)
        chk = dtdump.parse_table(os.path.join(self.work, "ui.uasset"), os.path.join(self.work, "ui.uexp"), sa, se)[3]
        assert [r[0] for r in chk[len(rows):]] == [m["id"] for m, *_ in self.table_rows["ui"]]
        # GameModeData
        ua, ue = S("/Game/GM/DATA/DT/DT_GameModeData"); sa, se = S("/Game/GM/DATA/Structure/STR_GameModeData")
        _, _, b, rows, _ = dtdump.parse_table(ua, ue, sa, se); pk = CookedPackage.load(ua)
        body = b[:10] + struct.pack("<i", len(rows) + len(self.table_rows["gmd"])) + b[14:len(b) - 4]
        for m, base, da_pkg, da_name in self.table_rows["gmd"]:
            name, start, end, row, offs = next(r for r in rows if r[0] == base["gmd_row"]); assert row["GameMode"] == base["enum"]
            p_imp = pk.add_import("/Script/CoreUObject", "Package", 0, da_pkg); o_imp = pk.add_import("/Script/Bodycam", "GameModeConfigDataAsset", p_imp, da_name)
            add_dep(pk, 0, o_imp, "create_before_ser")
            r = bytearray(b[start:end]); s, e = offs["GameModeConfig"]; struct.pack_into("<i", r, s, o_imp)
            s, e = offs["GameMode"]; assert e - s == 1; r[s] = m["_enum"]
            body += struct.pack("<ii", pk.add_name(m["id"]), 0) + bytes(r[8:])
        body += TAG; pk.exports[0]["serial_size"] = len(body) - 4; a = pk.serialize(); verify(a, body, "GameModeData", self.work)
        self.files[f"{C}GM/DATA/DT/DT_GameModeData.uasset"] = a; self.files[f"{C}GM/DATA/DT/DT_GameModeData.uexp"] = body
        open(os.path.join(self.work, "gmd.uasset"), "wb").write(a); open(os.path.join(self.work, "gmd.uexp"), "wb").write(body)
        chk = dtdump.parse_table(os.path.join(self.work, "gmd.uasset"), os.path.join(self.work, "gmd.uexp"), sa, se)[3]
        for (m, base, da_pkg, da_name), r in zip(self.table_rows["gmd"], chk[len(rows):]): assert r[0] == m["id"] and r[3]["GameModeConfig"].endswith(da_name), r
        # GamemodeInfo
        ua, ue = S("/Game/GM/DATA/DT/DT_GamemodeInfo"); sa, se = S("/Game/GM/DATA/Structure/STR_GamemodeWinInfo")
        _, _, b, rows, _ = dtdump.parse_table(ua, ue, sa, se); pk = CookedPackage.load(ua)
        body = b[:10] + struct.pack("<i", len(rows) + len(self.table_rows["gmi"])) + b[14:len(b) - 4]
        for m, base in self.table_rows["gmi"]:
            name, start, end, row, offs = next(r for r in rows if r[0] == base["gmi_row"])
            r = bytearray(b[start:end])
            s, e = offs["Gamemode"]; assert e - s == 1; r[s] = m["_enum"]
            if "MaxKill" in offs and offs["MaxKill"][1] - offs["MaxKill"][0] == 4 and "score_limit" in m.get("rules", {}): struct.pack_into("<i", r, offs["MaxKill"][0], int(m["rules"]["score_limit"]))
            if "GamemodeTime" in offs and offs["GamemodeTime"][1] - offs["GamemodeTime"][0] == 8 and "time_limit" in m.get("rules", {}): struct.pack_into("<d", r, offs["GamemodeTime"][0], float(m["rules"]["time_limit"]))
            # MaxPlayer drives the bot fill (GM_Bodycam: bots = MaxPlayer - connected) and the scoreboard "n/Max" (v13 finding: stock 10 capped the 10v10 test)
            if "MaxPlayer" in offs and offs["MaxPlayer"][1] - offs["MaxPlayer"][0] == 4: struct.pack_into("<i", r, offs["MaxPlayer"][0], int(m["players"]["max"]))
            if "ScoreToWin" in offs and offs["ScoreToWin"][1] - offs["ScoreToWin"][0] == 4 and "score_limit" in m.get("rules", {}): struct.pack_into("<i", r, offs["ScoreToWin"][0], int(m["rules"]["score_limit"]))
            body += struct.pack("<ii", pk.add_name(m["id"]), 0) + bytes(r[8:])
        body += TAG; pk.exports[0]["serial_size"] = len(body) - 4; a = pk.serialize(); verify(a, body, "GamemodeInfo", self.work)
        self.files[f"{C}GM/DATA/DT/DT_GamemodeInfo.uasset"] = a; self.files[f"{C}GM/DATA/DT/DT_GamemodeInfo.uexp"] = body
        open(os.path.join(self.work, "gmi.uasset"), "wb").write(a); open(os.path.join(self.work, "gmi.uexp"), "wb").write(body)
        chk = dtdump.parse_table(os.path.join(self.work, "gmi.uasset"), os.path.join(self.work, "gmi.uexp"), sa, se)[3]
        assert [r[0] for r in chk[len(rows):]] == [m["id"] for m, base in self.table_rows["gmi"]]

    # ---- localization: the player's own Game.locres per culture + our entries (never any game text redistributed) ----
    def merge_localization(self, modes):
        """Each mode may carry `_loc` = the parsed gamemodes/<id>/loc.json ({"namespace", "entries": {key: {culture: text}}}).
        For every culture the game ships, clone the stock Game.locres and add every entry (source = the 'en' text, which
        is exactly what the row / Blueprint carries). Missing translations fall back to English."""
        from locres import LocRes
        entries = {}
        for m in modes:
            loc = m.get("_loc") or {}
            ns = loc.get("namespace", LOC_NAMESPACE)
            assert ns == LOC_NAMESPACE, f"{m['id']}: loc namespace must be {LOC_NAMESPACE}, got {ns!r}"
            for key, texts in (loc.get("entries") or {}).items():
                assert key.startswith(m["id"] + "."), f"{m['id']}: loc key {key!r} must start with '{m['id']}.'"
                assert "en" in texts and texts["en"], f"{m['id']}: loc key {key!r} has no 'en' source text"
                entries[key] = texts
            # the row texts must be translatable even when a mode ships no loc.json at all
            entries.setdefault(f"{m['id']}.Title", {"en": m["title"]})
            entries.setdefault(f"{m['id']}.Description", {"en": m.get("description", "")})
            assert entries[f"{m['id']}.Title"]["en"] == m["title"], f"{m['id']}: loc.json 'en' Title != manifest title"
            assert entries[f"{m['id']}.Description"]["en"] == m.get("description", ""), f"{m['id']}: loc.json 'en' Description != manifest description"
        if not entries: return
        for cul in CULTURES:
            full = f"Bodycam/Content/Localization/Game/{cul}/Game.locres"
            if not self.stock_available(full):
                print(f"  localization: {full} not in the paks - skipped"); continue
            l = LocRes.load(self.stock(full))
            assert not l.check_hashes(), f"{full}: hash mismatch in the stock file (format change?)"
            n = 0
            for key, texts in entries.items():
                src = texts["en"]; text = texts.get(cul) or src
                if not src: continue
                l.add(LOC_NAMESPACE, key, src, text); n += 1
            data = l.dumps()
            self.files[full] = data
            print(f"  localization: {cul} +{n} entries -> {len(data)} B")

    def merge_registry(self, progress=None):
        """progress(fraction 0..1) through the four steps that cost anything: extracting the stock
        registry, parsing it, appending one record per new package (a linear scan each), writing it
        back out. Measured 2026-09-16 on Bodycam's 68 MB / 44k-record registry: .05 / .15 / .65 / .15."""
        step = progress or (lambda f: None)
        data = self.stock_bytes("Bodycam/AssetRegistry.bin"); step(0.05)
        reg = Registry(data); step(0.20)
        n = max(len(self.registry_adds), 1)
        for i, (tmpl, new) in enumerate(self.registry_adds):
            reg.add_like(tmpl, new); step(0.20 + 0.65 * (i + 1) / n)
        self.files["Bodycam/AssetRegistry.bin"] = reg.serialize(); step(1.0)

    def write(self, out, progress=None):
        step = progress or (lambda f: None)
        big = [rel for rel, data in self.files.items() if len(data) >= 65536]   # registry + level nav meshes (Zlib entries proven in-game since v10)
        # packing is ~90% of this step (the registry's zlib blocks), the read-back verify the rest
        paklib.write_pak(out, "../../../", self.files, seed=0, compress=big,
                         progress=lambda done, total: step(0.90 * (done / total if total else 1.0)))
        r = paklib.PakReader(out); assert set(r.files) == set(self.files)
        import zlib
        for i, (rel, data) in enumerate(self.files.items()):
            step(0.90 + 0.10 * (i + 1) / max(len(self.files), 1))
            e, blocks = r.raw_blocks(rel)
            got = blocks[0] if e["comp_idx"] == 0 else b"".join(zlib.decompress(x) for x in blocks)
            assert got == data and paklib.fnv64_path(rel, r.seed) in r.phi, rel
        print("OK", out, os.path.getsize(out), "B sha256", hashlib.sha256(open(out, "rb").read()).hexdigest())
        for rel in sorted(self.files): print("  ", rel, len(self.files[rel]), "B")

# What each build step costs, as a share of the whole build. Measured 2026-09-16 on this machine
# (two modes, 13 maps, Bodycam's 68 MB registry): 2.8 s cold stock cache, 2.1 s warm, of which the
# pak write is ~55% and the registry merge ~25%. The shares only have to be roughly right: they are
# what turns the install bar from a barber's pole into a bar that moves through the real work.
BUILD_STEPS = {"enum": 0.03, "modes": 0.14, "tables": 0.03, "localization": 0.03,
               "registry": 0.24, "write": 0.53}


def build_from_packs(paks_dir, work_dir, pack_dirs, out_path, log=print, progress=None, rules_override=None):
    """Library entry point used by the hub. A pack dir holds manifest.json + cooked/ (only OUR cooked packages: GM_<id>, DA_<id>,
    GM/Gamemode/<id>/*; never the game's assets) + cooked/blueprints_summary.txt (the RESULT: OK verdict of the Blueprint run that
    produced it). The game's own assets are read from the player's paks at build time (paks_dir), so nothing of the game is redistributed.
    progress(fraction 0..1, step name) reports how far through the build we are, weighted by BUILD_STEPS.
    rules_override is {mode_id: {rule: number}} merged OVER each manifest's own "rules" (the hub passes what the catalogue's
    rules_override carries, so a test build can play short rounds without a new pack); the manifest on disk is never touched.
    Returns {"out": path, "size": bytes, "sha256": hex, "modes": [ids]}."""
    import builtins, io
    packs = [os.path.abspath(p) for p in pack_dirs]
    if not packs: raise ValueError("no packs selected")
    for p in packs:
        if not os.path.exists(os.path.join(p, "manifest.json")): raise FileNotFoundError(f"pack without manifest.json: {p}")
        Builder.check_cook_verdict(os.path.join(p, "cooked"))
    b = Builder(paks_dir, work_dir, cooked=None, allow_unverified_cook=True)   # verdicts were checked per pack above
    ids = set(); modes = []
    for i, p in enumerate(packs):
        m = json.load(open(os.path.join(p, "manifest.json"), encoding="utf-8"))
        assert m["id"].isidentifier() and m["id"] not in ids, m["id"]; ids.add(m["id"])
        m["_enum"] = mode_enum(m, i)
        m["_cooked"] = os.path.join(p, "cooked")
        over = (rules_override or {}).get(m["id"])
        if over:
            m["rules"] = {**m.get("rules", {}), **over}
            log(f"{m['id']}: rules overridden by the catalogue: " + ", ".join(f"{k}={v}" for k, v in sorted(over.items())))
        loc_path = os.path.join(p, "loc.json")
        m["_loc"] = json.load(open(loc_path, encoding="utf-8")) if os.path.exists(loc_path) else None
        modes.append(m)
    # a monotonic 0..1 across the weighted steps: `at` is where the current step starts, and a step
    # reports its own 0..1 inside its share
    base = [0.0]
    def step(name, frac=1.0):
        if progress: progress(min(base[0] + BUILD_STEPS[name] * max(0.0, min(1.0, frac)), 1.0), name)
    def done(name):
        base[0] += BUILD_STEPS[name]; step(name, 0.0)

    # route the builder's prints to the caller's log
    real_print = builtins.print
    def _p(*a, **k): log(" ".join(str(x) for x in a))
    builtins.print = _p
    try:
        step("enum", 0.0)
        b.enum_asset(modes); done("enum")
        for i, m in enumerate(modes):
            b.build_mode(m); step("modes", (i + 1) / len(modes))
        done("modes")
        b.merge_tables(); done("tables")
        b.merge_localization(modes); done("localization")
        b.merge_registry(progress=lambda f: step("registry", f)); done("registry")
        b.write(out_path, progress=lambda f: step("write", f)); done("write")
    finally:
        builtins.print = real_print
    data = open(out_path, "rb").read()
    return {"out": out_path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "modes": [m["id"] for m in modes]}

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--paks", required=True); ap.add_argument("--work", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--cooked", help="mirror project cook output, e.g. mirror/cooked (needed for cooked bases; must contain blueprints_summary.txt ending in RESULT: OK)")
    ap.add_argument("--allow-unverified-cook", action="store_true", help="build even if the cook carries no RESULT: OK verdict (pre-v21 cooks only)")
    ap.add_argument("manifests", nargs="+"); args = ap.parse_args()
    b = Builder(args.paks, args.work, args.cooked, args.allow_unverified_cook)
    ids = set(); modes = []
    for i, mf in enumerate(args.manifests):
        m = json.load(open(mf, encoding="utf-8")); assert m["id"].isidentifier() and m["id"] not in ids, m["id"]; ids.add(m["id"])
        m["_enum"] = mode_enum(m, i)
        loc_path = os.path.join(os.path.dirname(os.path.abspath(mf)), "loc.json")
        m["_loc"] = json.load(open(loc_path, encoding="utf-8")) if os.path.exists(loc_path) else None
        modes.append(m)
    b.enum_asset(modes)
    for m in modes: b.build_mode(m)
    b.merge_tables(); b.merge_localization(modes); b.merge_registry(); b.write(args.out)

if __name__ == "__main__":
    main()
