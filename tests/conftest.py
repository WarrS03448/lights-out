"""Keep pytest away from the tests that are standalone scripts.

Four files here are not pytest tests and were never meant to be. They run their checks at import
time and finish with a bare `sys.exit(...)` at module level - which pytest executes during
COLLECTION, so the exit propagates out of the collector and the whole run dies with

    INTERNALERROR> SystemExit: 0
    no tests ran

...before a single real test has been touched. That is a confusing failure: nothing is broken, and
the output says nothing ran rather than naming the file that stopped it.

So they are excluded here and are still run the way they always were, directly:

    python tests/test_wire.py

Everything else in this directory is ordinary `def test_*` and collects normally. `test_hub.py`
also self-runs, but it guards that behind `if __name__ == "__main__"`, so importing it is safe and
pytest can collect it like any other module.

If you add a test file that runs itself at import time, add it to this list - or better, put the
self-run behind the same `__main__` guard and it needs no entry at all.
"""

import pytest

collect_ignore = [
    "test_host_permit.py",
    "test_lobbypak.py",
    "test_redeploy.py",
    "test_wire.py",
    "test_wire_rejoin.py",
]

# ---------------------------------------------------------------------------------------------
# The four files above are run directly, and they carry a `#!/usr/bin/env python3.12` shebang - so
# `python tests/test_wire.py` can land on the Windows py launcher and die with "No runtime
# installed that matches 3.12" before the file is even read. Name an interpreter and they pass:
#
#     .venv/Scripts/python.exe tests/test_wire.py
#
# One test in test_hub.py fails whenever the REAL hub is running: test_only_one_hub_runs_at_a_time
# asserts the single-instance Windows mutex can be claimed, and LightsOut.exe holds it. That is
# the test working. Close the hub before a full run.

@pytest.fixture(autouse=True)
def _state_dir_per_module(request, monkeypatch, tmp_path):
    """Point HUB_STATE_DIR at the state dir of the file the running test came from.

    SEVEN files here do `os.environ["HUB_STATE_DIR"] = _STATE` at module scope, each with its own
    temp directory. Run individually that is fine. Under pytest every module is IMPORTED before
    anything runs, in one process - so the last import wins and every file afterwards is writing
    into some other file's state dir.

    test_hub.py::test_state_roundtrip is the one that noticed: it asserts state_file() lives under
    ITS _STATE, and by the time it ran that had been overwritten. It passed alone and failed in a
    full run, which is the most misleading way for a test to fail.

    paths.state_dir() reads the variable live on every call, so re-pointing it per test is enough -
    and monkeypatch puts it back afterwards, so nothing leaks the way the module-level assignment
    did.
    """
    state = getattr(request.module, "_STATE", None)
    if isinstance(state, str) and state:
        monkeypatch.setenv("HUB_STATE_DIR", state)
    else:
        monkeypatch.setenv("HUB_STATE_DIR", str(tmp_path / "hub-state"))


@pytest.fixture(autouse=True)
def _isolate_game_processes(request):
    isolate = getattr(request.module, "isolated_game_processes", None)
    if isolate:
        with isolate(request.function):
            yield
    else:
        yield
