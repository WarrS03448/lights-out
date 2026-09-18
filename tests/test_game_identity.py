"""Run: python -m pytest tests/test_game_identity.py. No game or network."""
import io
import struct
import pytest
from hub import game_identity as game


def test_private_protocol_is_bounded_and_rejects_non_objects():
    stream = io.BytesIO()
    game.write_frame(stream, {"v": 1, "op": "release"})
    stream.seek(0)
    assert game.read_frame(stream) == {"v": 1, "op": "release"}
    for raw in [b"", struct.pack("!I", 20000), struct.pack("!I", 2) + b"[]"]:
        with pytest.raises(game.GameIdentityError):
            game.read_frame(io.BytesIO(raw))


def test_proof_remains_alive_until_verification_and_is_released_on_failure():
    events = []
    class Ticket:
        def __init__(self, directory, identity):
            events.append("acquire")
            self.proof = {"ticket": "ab" * 128, "local_steam_id": "76561198000000001"}
        def __enter__(self):
            return self.proof
        def __exit__(self, *args):
            events.append("release")
    def request(action, payload, token):
        events.append(action)
        if action == "game/challenge":
            return {"challenge": "opaque", "identity": "a" * 24}
        if action == "game/verify":
            assert events[-2] == "acquire"
            raise game.GameIdentityError("verification failed")
    with pytest.raises(game.GameIdentityError):
        game.mint_session("parent", "game", request=request, ticket_factory=Ticket)
    assert events == ["game/challenge", "acquire", "game/verify", "release"]


def test_server_identity_is_authoritative_and_cancellation_prevents_adoption():
    class Ticket:
        def __init__(self, *args):
            pass
        def __enter__(self):
            return {"ticket": "ab" * 128, "local_steam_id": "76561198000000001"}
        def __exit__(self, *args):
            pass
    def request(action, payload, token):
        if action == "game/challenge":
            return {"challenge": "opaque", "identity": "a" * 24}
        return {"token": "lg_" + "a" * 43, "player_id": "a1111111-1111-4111-8111-111111111111",
                "game_steam_id": "76561198000000002"}
    with pytest.raises(game.GameIdentityError):
        game.mint_session("parent", "game", request=request, ticket_factory=Ticket)
    with pytest.raises(game.GameIdentityError):
        game.mint_session("parent", "game", should_stop=lambda: True, request=request, ticket_factory=Ticket)


def test_cleanup_always_closes_resources_when_termination_fails(monkeypatch):
    events = []
    class Process:
        def wait(self, timeout):
            raise TimeoutError()
        def kill(self):
            events.append("kill")
            raise OSError()
        def poll(self):
            return None
    class Resource:
        def close(self):
            events.append("close")
        def cleanup(self):
            events.append("cleanup")
    child = game.TicketProcess("game", "a" * 24)
    child.process, child.reader, child.writer, child.temporary = Process(), Resource(), Resource(), Resource()
    with pytest.raises(game.GameIdentityError):
        child.__exit__(None, None, None)
    assert events.count("close") == 2
    assert "cleanup" in events


def test_bounded_writer_cannot_hang_on_an_unread_pipe():
    import threading
    release = threading.Event()
    class Blocked:
        def write(self, data):
            release.wait(1)
            return len(data)
        def flush(self):
            pass
    try:
        with pytest.raises(game.GameIdentityError):
            game._timed_write(Blocked(), {"v": 1}, .02)
    finally:
        release.set()
