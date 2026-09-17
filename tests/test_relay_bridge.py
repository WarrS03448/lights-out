import http.client
import json
from types import SimpleNamespace
from hub.webui import httpbridge


def test_relay_bridge_requires_local_host_and_same_origin_writes():
    received = []
    client = SimpleNamespace(network_browser_state=lambda: {"generation": "test"},
        network_browser_input=lambda body: (received.append(body) or (200, {"ok": True})))
    panel = SimpleNamespace(session=SimpleNamespace(client=client))
    server, _url = httpbridge.start(panel)
    port = server.server_port
    def call(method, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            connection.request(method, "/network", json.dumps(body) if body else None, headers or {})
            reply = connection.getresponse()
            return reply.status, reply.read()
        finally:
            connection.close()
    try:
        assert call("GET")[0] == 200
        assert call("GET", headers={"Host": "attacker.example"})[0] == 403
        assert call("POST", {"type": "ready"}, {"Origin": "https://attacker.example", "Content-Type": "application/json"})[0] == 403
        assert received == []
        good = {"Origin": "http://127.0.0.1:%d" % port, "Content-Type": "application/json"}
        assert call("POST", {"type": "ready"}, good)[0] == 200
        assert received == [{"type": "ready"}]
    finally:
        server.shutdown(); server.server_close()
