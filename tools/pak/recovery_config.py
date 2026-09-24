"""Bounded private lobby configuration; imported by the launcher and pak retargeter.

An ASCII FName transports only authored configuration through the existing private
GameInstance field. Never log the envelope: its first 64 characters are a capability.
"""
import re


def _bad():
    raise ValueError("Invalid recovery configuration")


def _number(text, low, high):
    if not isinstance(text, str) or not re.fullmatch(r"0|[1-9][0-9]*", text):
        _bad()
    value = int(text)
    if not low <= value <= high:
        _bad()
    return value


def decode(value):
    if not isinstance(value, str) or len(value) > 1023 or not re.fullmatch(r"[a-z0-9-]+", value):
        _bad()
    parts = value.split("-")
    if not re.fullmatch(r"[a-f0-9]{64}", parts[0]):
        _bad()
    out = {"token": parts[0], "match_id": "", "epoch": 0, "checkpoint": None, "hash": ""}
    if len(parts) == 1:
        return out
    if len(parts) < 6 or parts[1] != "r1" or not re.fullmatch(r"[a-f0-9]{16}", parts[2]) or parts[-1] != "z":
        _bad()
    out.update(match_id=parts[2], epoch=_number(parts[3], 0, 999999))
    if parts[4] == "none" and len(parts) == 6:
        return out
    if len(parts) < 12 or not re.fullmatch(r"[a-f0-9]{64}", parts[4]):
        _bad()
    count = _number(parts[10], 2, 10)
    if len(parts) != 12 + 5 * count:
        _bad()
    boundary, limit = _number(parts[5], 0, 99), _number(parts[6], 1, 999)
    scores = [_number(parts[7], 0, limit - 1), _number(parts[8], 0, limit - 1)]
    if sum(scores) != boundary:
        _bad()
    rows, seen = [], set()
    for offset in range(11, len(parts) - 1, 5):
        identity, team, kills, deaths, spawns = parts[offset:offset + 5]
        if not re.fullmatch(r"[0-9]{17}", identity) or identity in seen:
            _bad()
        seen.add(identity)
        rows.append({"id": identity, "team": _number(team, 0, 1), "k": _number(kills, 1, 1999) - 1000,
                     "d": _number(deaths, 0, 99), "sp": _number(spawns, 0, 999)})
    if {row["team"] for row in rows} != {0, 1}:
        _bad()
    out.update(hash=parts[4], checkpoint={"v": 1, "round": boundary, "limit": limit, "scores": scores,
               "objective": _number(parts[9], 0, 1), "rows": rows})
    return out


def encode(token, match_id, epoch, checkpoint=None, checkpoint_hash=""):
    parts = [token, "r1", match_id, str(epoch), checkpoint_hash if checkpoint is not None else "none"]
    if checkpoint is not None:
        try:
            if checkpoint["v"] != 1:
                _bad()
            parts += [str(checkpoint[name]) for name in ("round", "limit")]
            if len(checkpoint["scores"]) != 2:
                _bad()
            parts += [str(n) for n in checkpoint["scores"]]
            parts += [str(checkpoint["objective"]), str(len(checkpoint["rows"]))]
            for row in sorted(checkpoint["rows"], key=lambda r: r["id"]):
                parts += [row["id"], str(row["team"]), str(row["k"] + 1000), str(row["d"]), str(row["sp"])]
        except (KeyError, TypeError, AttributeError):
            _bad()
    parts.append("z")
    try:
        value = "-".join(parts)
    except TypeError:
        _bad()
    decode(value)
    return value
