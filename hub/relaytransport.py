"""Authenticated transport for the browser's relay controller."""
import time

from .network import TRANSPORT

MAX_REPORT_PEERS = 16


def refresh(client, session, change, config, probe):
    active = lambda: client._network_active(session, change)
    if not active():
        return
    region = config["region"]
    session["retry_delay"] = 2
    def unavailable(message):
        probe.set_peers([], False)
        client._network_mark_unavailable(session, change, region, message)
    if not region:
        unavailable("Choose a matchmaking region.")
        return
    if probe.needs_credentials():
        if not active():
            return
        status, body = client._get("/api/network/relay")
        if not active():
            return
        if status != 200 or not probe.configure(body):
            unavailable(client._network_response_error(body, "Relay connection service is unavailable."))
            session["retry_delay"] = 15
            return
    profile = probe.profile()
    if not profile["location"]:
        unavailable(profile["error"])
        return
    if not active():
        return
    status, body = client._post("/api/network/profile", {"region": region,
        "cross_region": config["cross_region"], "location": profile["location"],
        "age_seconds": profile["age_seconds"], "transport": TRANSPORT})
    if not active():
        return
    if status != 200 or not isinstance(body, dict) or not body.get("ready") or not body.get("revision") or not body.get("steam_id"):
        unavailable(client._network_response_error(body, "Could not register relay measurements."))
        return
    revision = body["revision"]
    probe.set_identity(body["steam_id"], revision)
    session["unavailable_sent"] = False
    if not active():
        return
    status, peers = client._get("/api/network/peers?offset=0")
    if not active():
        return
    if (status != 200 or not isinstance(peers, dict) or peers.get("revision") != revision
            or not isinstance(peers.get("peers"), list) or len(peers["peers"]) > 16):
        unavailable(client._network_response_error(peers, "Could not discover relay peers."))
        return
    probe.set_peers(peers["peers"], peers.get("queued") is True)
    samples = probe.take_measurements()
    measured_at = time.monotonic()
    pending = [(row, measured_at - row["age_seconds"]) for row in samples]
    for offset in range(0, len(pending), MAX_REPORT_PEERS):
        if not active():
            return
        now = time.monotonic()
        batch = [{**row, "age_seconds": now - sampled_at}
                 for row, sampled_at in pending[offset:offset + MAX_REPORT_PEERS]
                 if now - sampled_at <= 15]
        if not batch:
            continue
        status, result = client._post("/api/network/pings", {"revision": revision, "peers": batch})
        if not active():
            return
        # Queue/attempt expiry can race a final report. It must not create a new timestamp
        # for that sample or prevent other candidates from being measured next cycle.
        if status not in (200, 400, 409):
            unavailable(client._network_response_error(result, "Could not upload relay measurements."))
            return
    if active():
        client._network_set_status(True, region, "", session=session, change=change)
