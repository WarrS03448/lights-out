"""Run: python -m pytest tests/test_recovery_config.py."""
import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "pak"))
try:
    config = importlib.import_module("recovery_config")
except ModuleNotFoundError:
    config = None


def checkpoint():
    return {"v": 1, "round": 3, "limit": 7, "scores": [2, 1], "objective": 1,
            "rows": [{"id": "76561198000000001", "team": 0, "k": -1, "d": 2, "sp": 3},
                     {"id": "76561198000000002", "team": 1, "k": 2, "d": 1, "sp": 3}]}


def test_private_envelope_round_trip_preserves_large_ids_and_signed_kills():
    assert config is not None, "versioned recovery configuration is not implemented"
    encoded = config.encode("a" * 64, "0123456789abcdef", 2, checkpoint(), "b" * 64)
    assert len(encoded) < 1024
    assert set(encoded) <= set("0123456789abcdefghijklmnopqrstuvwxyz-")
    parsed = config.decode(encoded)
    assert parsed == {"token": "a" * 64, "match_id": "0123456789abcdef", "epoch": 2,
                      "checkpoint": checkpoint(), "hash": "b" * 64}
    assert config.decode("a" * 64)["checkpoint"] is None


def test_configuration_rejects_partial_and_malformed_snapshots_without_echoing_credentials():
    assert config is not None, "versioned recovery configuration is not implemented"
    good = config.encode("a" * 64, "0123456789abcdef", 2, checkpoint(), "b" * 64)
    for bad in [good[:-2], good + "-extra", good.replace("-r1-", "-r2-"),
                good.replace("76561198000000002", "76561198000000001"), "a" * 1024]:
        with pytest.raises(ValueError) as error:
            config.decode(bad)
        assert "a" * 64 not in str(error.value)
    for change in [{"round": 2}, {"rows": []}, {"objective": 2}]:
        value = {**checkpoint(), **change}
        with pytest.raises(ValueError):
            config.encode("a" * 64, "0123456789abcdef", 2, value, "b" * 64)
