"""Keep both authored Bodybomb modes on the same graph implementation."""
import json


def remap(value, mode):
    if mode not in ("BB5", "BB1"):
        raise ValueError("Unknown Bodybomb mode")
    return value.replace("BB5", mode)


def graph(value, mode):
    result = remap(value, mode)
    json.loads(result)  # Fail before sending malformed graph data to the editor.
    return result
