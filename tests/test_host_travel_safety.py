"""Run: python -m pytest tests/test_host_travel_safety.py

Host travel must not race a diagnostic online search. Check source AND shipped cook.
"""
import json
import pathlib
import re
import sys
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mirror" / "Bodycam" / "Scripts"))
sys.path.insert(0, str(ROOT / "tools" / "pak"))

import check_graphs
import kismet
import lobby_graphs
from hub import lobbypak

FORBIDDEN = {"FindLobbies", "JoinLobby"}


def test_host_travel_sends_the_stamped_match_credential():
    graph = json.loads(lobby_graphs.chlobby_logic())
    # Follow the actual data edges into SendAttributionEvent's bearer input.
    links = {tuple(link) for link in graph["links"]}
    assert ("rt_str.ReturnValue", "hdly.BearerToken") in links


def test_host_does_not_start_searches_beside_travel():
    graph = json.loads(lobby_graphs.chlobby_logic())
    calls = {n.get("func") for n in graph["nodes"] if n["type"] == "call"}
    assert "OpenLevel" in calls
    assert not calls & FORBIDDEN


def test_graph_checker_rejects_a_host_search_even_if_frozen_hash_is_updated():
    graph = json.loads(lobby_graphs.chlobby_logic())
    graph["nodes"].append({"id": "unsafe_search", "type": "call", "func": "FindLobbies",
                           "class": lobby_graphs.FIND_LOBBIES})
    problems = check_graphs.check("chlobby_logic", json.dumps(graph))
    assert any("host travel" in p and "FindLobbies" in p for p in problems)


def test_shipped_host_bytecode_cannot_launch_search_or_join():
    base = ROOT / "hub" / "lobbyseed" / "Bodycam" / "Content" / "GM" / "Gamemode" / "GM_CHLobby"
    package = kismet.Pkg(str(base.with_suffix(".uasset")), str(base.with_suffix(".uexp")))
    lines = []
    for index, _ in kismet.list_functions(package):
        function = kismet.parse_function(package, index)
        lines.extend(kismet.Dis(package, function["code"]).run())
    calls = "\n".join(lines)
    assert "CallMath OpenLevel@" in calls
    assert not any("CallMath " + name + "@" in calls for name in FORBIDDEN)
    # Inspect the shipped bytecode, not only the source graph: the bearer argument
    # immediately after the delay URL must be the stamped token's converted value.
    literal = re.search(r"Let \(([^\n]+)\)\n[^\n]*\n[^\n]*CallMath MakeLiteralName[^\n]*\n"
                        r"[^\n]*NameConst 'chreport-7f3a91'", calls)[1]
    converted = re.search(r"Let \(([^\n]+)\)\n[^\n]*\n[^\n]*CallMath Conv_NameToString[^\n]*\n"
                          r"[^\n]*\$Local " + re.escape(literal), calls)[1]
    assert re.search(r"String 'https://lightsout.up.railway.app/api/probe/slow'\n"
                     r"[^\n]*\$Local " + re.escape(converted), calls)
    assert calls.count("CallMath OpenLevel@") == 1
    assert calls.count("String 'LobbyHost'") == 2
    assert calls.count("CallMath GetCurrentLevelName@") == 3  # two guards plus diagnostics
    assert "NameConst '/Game/Map/LobbyHost/LobbyHost'" in calls
    assert re.search(r"Int 2\n[^\n]*CallMath OpenLevel@", calls)
    # A second stock BeginPlay would schedule another native range load. In the
    # cooked program its false guard must skip parent and land at the same fan
    # as the true path after parent returns.
    guarded_parent = re.search(
        r"JumpIfNot -> ([0-9a-f]+)\n[^\n]*\$Local ([^\n]+)\n"
        r"[^\n]*LocalFinalFunction ReceiveBeginPlay@GM_Host_C[^\n]*\n"
        r"[^\n]*\n[^\n]*Jump -> ([0-9a-f]+)", calls)
    assert guarded_parent and guarded_parent[1] == guarded_parent[3]
    assert 'BooleanAND' in guarded_parent[2]


def test_separate_joiner_still_uses_native_parent_join_flow():
    graph = json.loads(lobby_graphs.chjoin_logic())
    assert not any(n.get("func") == "OpenLevel" for n in graph["nodes"])
    assert any(n.get("type") == "callparent" for n in graph["nodes"])


def test_retrying_same_match_rebuilds_variant_after_seed_update(tmp_path, monkeypatch):
    seed = tmp_path / "seed.pak"
    seed.write_bytes(b"old host with unsafe search")
    monkeypatch.setattr(lobbypak, "seed_path", lambda *a, **kw: str(seed))
    monkeypatch.setattr(lobbypak, "cache_dir", lambda: str(tmp_path))
    monkeypatch.setattr(lobbypak, "available_levels", lambda game: {"BB5_Rome": "/Game/Rome"})
    builds = []

    def retarget(source, destination, *args, **kwargs):
        cooked = pathlib.Path(source).read_bytes()
        builds.append(cooked)
        pathlib.Path(destination).write_bytes(cooked)

    monkeypatch.setattr(lobbypak, "_retarget_module", lambda: SimpleNamespace(retarget_pak=retarget))
    args = ("fake-game", "/Game/Rome", "BB5_Rome")
    old = lobbypak.variant(*args, token="chm-same-match")
    assert lobbypak.variant(*args, token="chm-same-match") == old
    assert len(builds) == 1
    seed.write_bytes(b"fixed host without search")
    fixed = lobbypak.variant(*args, token="chm-same-match")
    assert pathlib.Path(fixed).read_bytes() == b"fixed host without search"
    assert len(builds) == 2
