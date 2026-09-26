#!/usr/bin/env python3.12
"""Run: python -m pytest tests/test_single_site_fingerprints.py.

The reviewed 1v1 source fingerprints. No retail game bytes: sha is stubbed so the
"files" are their own digests.
"""
from pathlib import Path
import re
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools/pak"))
import single_site

BOMBHOUSE_2026_09_24 = ("41b2cb7509b227b71aa0f1bc281cda59debece33a56a8c08684346afe70c9a8e",
                        "1a6516a9877d61e1599e12c0fdc68fef1d635572fa693196992983a71bfa5f17")
BOMBHOUSE_2026_09_25 = ("41b2cb7509b227b71aa0f1bc281cda59debece33a56a8c08684346afe70c9a8e",
                        "8267a4cdc2118c2b7aee73bf5310816adfdc1d85c777c58908bd86125b0d0616")


@pytest.fixture(autouse=True)
def digest_is_the_file(monkeypatch):
    monkeypatch.setattr(single_site, "sha", lambda data: data.decode())


def verify(key, pair):
    single_site.verify_source(key, pair[0].encode(), pair[1].encode())


def test_players_on_either_side_of_the_2026_09_25_update_can_build_bomb_house():
    verify("BombHouse", BOMBHOUSE_2026_09_24)   # has not taken the update yet
    verify("BombHouse", BOMBHOUSE_2026_09_25)   # the friend who updated and could not install


def test_every_reviewed_fingerprint_passes_and_nothing_else_does():
    known = set()
    for key, (_stem, _site, pairs) in single_site.CASES.items():
        assert pairs, key
        for pair in pairs:
            assert all(re.fullmatch(r"[0-9a-f]{64}", h) for h in pair), (key, pair)
            verify(key, pair)
            known.add(pair)
    header, payload = BOMBHOUSE_2026_09_25
    airsoft = single_site.CASES["Airsoft"][2][0]
    for key, pair in (("BombHouse", (header, "0" * 64)),         # right header, unreviewed payload
                      ("BombHouse", ("0" * 64, payload)),
                      ("BombHouse", airsoft),                    # another map's reviewed bytes
                      ("Airsoft", BOMBHOUSE_2026_09_24)):
        with pytest.raises(ValueError):
            verify(key, pair)


def test_an_unreviewed_map_says_what_happened_and_whose_move_it_is():
    with pytest.raises(ValueError) as caught:
        verify("BombHouse", ("0" * 64, "0" * 64))
    text = str(caught.value)
    assert "Bomb House" in text and "Nothing was installed" in text
    assert "Nothing is wrong with your PC" in text and "Lights Out update" in text
    with pytest.raises(ValueError, match="Unapproved"):
        verify("Paintball", BOMBHOUSE_2026_09_25)
    assert set(single_site.MAP_TITLES) == set(single_site.CASES)
