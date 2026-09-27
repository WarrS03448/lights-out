#!/usr/bin/env python3.12
"""The Linux beta's own real-server wire tests, run against THIS tree's server.

    .venv/Scripts/python.exe tests/linux_client_wire.py [--windows-build] [test_wire.py ...]

The Linux client lives on another branch (codex/linux-beta). Its tests/test_wire.py,
test_wire_rejoin.py, test_host_permit.py and test_redeploy.py each start `node server.cjs` from
their own checkout and drive that checkout's hub. This runs them unchanged in substance against
this tree's server/ instead: each is copied into a temp dir and aimed by text substitution - the
server directory, the port, the hub version read from this server's catalogue, the Linux public
beta switched on (--windows-build leaves it off: that branch built for Windows), and the game
launch stubbed. Nothing is written into the Linux checkout (no bytecode, hub state in temp dirs).

Environment:
    HUB_LINUX_ROOT      a codex/linux-beta checkout (required; without it this says so and skips)
    LINUX_WIRE_PORT     first port (default 8950); test n uses PORT + 2n and PORT + 2n + 1
    LINUX_WIRE_SERVER   another server/ to aim at (default: this tree's), for a baseline
Exit 0 when every test passed (or skipped), 1 otherwise.

BASELINE, 2026-09-27 (Linux client e5ec3289): test_wire.py and test_host_permit.py pass against
this server. test_wire_rejoin.py ("the running game is adopted for end-of-match cleanup") and
test_redeploy.py ("both matched": its server runs without COMP_NETWORK_TEST_BYPASS, so the queue
waits for relay measurements nobody posts) fail the same step against the Linux branch's OWN
server too (LINUX_WIRE_SERVER=<linux checkout>/server): compare failing names, not counts.
"""
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time

REPO = pathlib.Path(__file__).resolve().parent.parent
SERVER = pathlib.Path(os.environ.get("LINUX_WIRE_SERVER") or REPO / "server")
TESTS = {  # file: (its port line, its second port line or None)
    "test_wire.py": ("PORT = 8931", None),
    "test_wire_rejoin.py": ("PORT = 8933", None),
    "test_host_permit.py": ("PORT = 8934", None),
    "test_redeploy.py": ("PORT = 8935", "STORE_PORT = 8936"),
}

PREAMBLE = '''
# ---- aimed by tests/linux_client_wire.py at GATE_SERVER_DIR on GATE_PORT ----
import tempfile as _gate_tempfile
GATE_SERVER = pathlib.Path(os.environ["GATE_SERVER_DIR"])
GATE_LINUX = os.environ.get("GATE_LINUX") == "1"
os.environ.setdefault("HUB_STATE_DIR", _gate_tempfile.mkdtemp(prefix="hub-gate-"))
import socket as _gate_socket
try:
    _gate_socket.create_connection(("127.0.0.1", PORT), 0.3).close()
    print("port %d already answers: refusing to test a leftover server" % PORT)
    sys.exit(2)
except OSError:
    pass
'''

AFTER_HUB_IMPORT = '''
import json as _gate_json
from hub import version as _gate_version, game as _gate_game
_gate_version.LINUX_PUBLIC_BETA = GATE_LINUX
C.HUB_VERSION = _gate_json.loads((GATE_SERVER / "public" / "catalogue.json")
                                 .read_text("utf-8"))["hub"]["version"]
_gate_game.launch_game = lambda: True
_gate_game.launch_game_with_args = lambda *a, **k: True
_gate_game.close_game = lambda **kw: "not-running"
print("  aimed: server=%s linux_beta=%s hub_version=%s" % (GATE_SERVER, GATE_LINUX, C.HUB_VERSION))
'''


def adapt(src, name, port_line, store_line):
    """The Linux branch's test, aimed at GATE_SERVER_DIR; fails loudly if a pattern moved."""
    out = src.replace(port_line, 'PORT = int(os.environ["GATE_PORT"])' + PREAMBLE, 1)
    if store_line:
        out = out.replace(store_line, "STORE_PORT = PORT + 1", 1)
    for old in ('cwd=str(pathlib.Path(__file__).resolve().parent.parent / "server")',
                'cwd=str(REPO / "server")'):
        out = out.replace(old, "cwd=str(GATE_SERVER)")
    for old in ('(root / "server" / "public" / "catalogue.json")',
                '(REPO / "server" / "public" / "catalogue.json")'):
        out = out.replace(old, '(GATE_SERVER / "public" / "catalogue.json")')
    # The copy lives in a temp dir: its hub, and anything else it finds beside itself, is the
    # Linux checkout's.
    out = out.replace("pathlib.Path(__file__).resolve().parent.parent",
                      'pathlib.Path(os.environ["HUB_LINUX_ROOT"])')
    m = re.search(r"^from hub import competitive as C[^\n]*\n", out, re.M)
    if m:
        out = out[:m.end()] + AFTER_HUB_IMPORT + out[m.end():]
    if name == "test_redeploy.py":
        # test_redeploy predates client-side relay admission and fails on its own server too
        # ("Choose a matchmaking region."); test_wire.py's make() turns it off the same way.
        out = out.replace("    s = C.LiveSession(p)\n    s.adopt_account(",
                          "    s = C.LiveSession(p)\n    s._network_config = None\n    s.adopt_account(", 1)
        if "s._network_config = None" not in out:
            raise SystemExit("%s: the LiveSession pattern moved; update adapt()" % name)
    if "GATE_PORT" not in out:
        raise SystemExit("%s: the port line moved; update TESTS" % name)
    if "from hub import competitive as C" in src and "_gate_version" not in out:
        raise SystemExit("%s: the hub import moved; update adapt()" % name)
    return out


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    windows_build = "--windows-build" in sys.argv
    root = os.environ.get("HUB_LINUX_ROOT")
    if not root or not (pathlib.Path(root) / "hub" / "linux_beta.py").is_file():
        print("SKIPPED: set HUB_LINUX_ROOT to a codex/linux-beta checkout (hub/linux_beta.py); got %s"
              % (root or "nothing"))
        return 0
    root = pathlib.Path(root).resolve()
    base = int(os.environ.get("LINUX_WIRE_PORT") or 8950)
    work = pathlib.Path(tempfile.mkdtemp(prefix="linux-client-wire-"))
    print("Linux client: %s   server: %s   build: %s   work: %s"
          % (root, SERVER, "Windows (linux_beta off)" if windows_build else "Linux", work))
    results = []
    for n, (name, (port_line, store_line)) in enumerate(TESTS.items()):
        if args and name not in args:
            continue
        original = root / "tests" / name
        if not original.is_file():
            results.append((name, "missing from the Linux checkout"))
            continue
        copy = work / name
        copy.write_text(adapt(original.read_text("utf-8"), name, port_line, store_line), "utf-8")
        env = {k: v for k, v in os.environ.items()
               if not k.upper().startswith(("UPSTASH", "REDIS", "RAILWAY", "COMP_", "PROBE_", "SMTP"))}
        env.update(GATE_SERVER_DIR=str(SERVER), GATE_PORT=str(base + 2 * n), HUB_LINUX_ROOT=str(root),
                   GATE_LINUX="0" if windows_build else "1", PYTHONDONTWRITEBYTECODE="1",
                   HUB_STATE_DIR=tempfile.mkdtemp(prefix="hub-linux-wire-", dir=str(work)))
        log = work / (name + ".log")
        started = time.monotonic()
        with open(log, "w", encoding="utf-8") as f:
            code = subprocess.call([sys.executable, "-B", str(copy)], cwd=str(work), env=env,
                                   stdout=f, stderr=subprocess.STDOUT, timeout=1200)
        text = log.read_text("utf-8", "replace")
        verdict = "passed" if code == 0 else "FAILED (exit %s, see %s)" % (code, log)
        if "EADDRINUSE" in text:
            verdict = "FAILED: a port was taken (see %s)" % log
        results.append((name, "%s in %.0f s" % (verdict, time.monotonic() - started)))
        print("  %-22s %s" % results[-1], flush=True)
    failed = [r for r in results if not r[1].startswith("passed")]
    print("LINUX CLIENT WIRE TESTS %s (%d of %d)" % ("FAILED" if failed else "PASSED",
                                                     len(results) - len(failed), len(results)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
