"""Does publish.py verify() actually catch two builds cut under one version number?

That is the whole point of the change, and "it compiles" does not test it. This stands a local
HTTP server up as the live site and drives verify() against it four ways:

  1. served bytes match the catalogue            -> passes
  2. same LENGTH, different bytes                -> must DIE (the old check passed this)
  3. wrong length                                -> must die (it always did)
  4. catalogue entry carries no sha256           -> passes, but says the bytes are unverified

Run:  python test_verify.py
"""
import hashlib
import http.server
import json
import os
import pathlib
import sys
import threading

WT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WT / "tools" / "release"))

HUB = b"PRETEND-INSTALLER-" + b"A" * 400
IMPOSTOR = b"PRETEND-INSTALLER-" + b"B" * 400          # same length, different build
assert len(HUB) == len(IMPOSTOR)

STATE = {"hub": HUB}


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/octet-stream"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command == "GET":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if self.path.startswith("/catalogue.json"):
            return self._send(200, json.dumps(STATE["catalogue"]).encode(), "application/json")
        if self.path.startswith("/api/health"):
            return self._send(200, b'{"ok":true}', "application/json")
        if self.path.startswith("/hub/"):
            return self._send(200, STATE["hub"])
        self._send(404, b"no")

    def log_message(self, *a):
        pass


def serve():
    srv = http.server.HTTPServer(("127.0.0.1", 8813), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def catalogue(sha):
    entry = {"version": "9.9.9", "download_url": "http://127.0.0.1:8813/hub/Setup-9.9.9.exe",
             "page_url": "http://127.0.0.1:8813/", "size": len(HUB), "kind": "inno-setup"}
    if sha is not None:
        entry["sha256"] = sha
    return {"hub": entry, "gamemodes": []}


def run_case(name, served, cat_sha, expect_die):
    STATE["hub"] = served
    STATE["catalogue"] = catalogue(cat_sha)
    (WT / "server" / "public" / "catalogue.json").write_text(
        json.dumps(STATE["catalogue"], indent=2), encoding="utf-8")

    import importlib
    import publish
    importlib.reload(publish)
    publish.origin = lambda: "http://127.0.0.1:8813"

    died, out = [], []
    publish.say = lambda m="": out.append(str(m))

    def fake_die(msg):
        died.append(str(msg))
        raise SystemExit(1)
    publish.die = fake_die

    try:
        publish.verify(wait_seconds=5)
        outcome = "passed"
    except SystemExit:
        outcome = "died"
    except Exception as e:                                    # noqa: BLE001
        outcome = "raised %s" % e

    want = "died" if expect_die else "passed"
    ok = outcome == want
    print("%-46s %-8s (wanted %s)  %s" % (name, outcome, want, "ok" if ok else "FAIL"))
    if died:
        print("      die: %s" % died[0].splitlines()[0])
    for line in out:
        if "sha256" in line or "unverified" in line:
            print("      say: %s" % line.strip())
    return ok


def main():
    serve()
    backup = (WT / "server" / "public" / "catalogue.json").read_bytes()
    results = []
    try:
        results.append(run_case("matching bytes", HUB, hashlib.sha256(HUB).hexdigest(), False))
        results.append(run_case("SAME LENGTH, different build", IMPOSTOR,
                                hashlib.sha256(HUB).hexdigest(), True))
        results.append(run_case("wrong length", HUB[:-10],
                                hashlib.sha256(HUB).hexdigest(), True))
        results.append(run_case("catalogue has no sha256", HUB, None, False))
    finally:
        (WT / "server" / "public" / "catalogue.json").write_bytes(backup)
        print("\n(catalogue.json restored)")
    print("\n%d/%d cases behaved correctly" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
