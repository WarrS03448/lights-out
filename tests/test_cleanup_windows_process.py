"""Windows integration: closes only a disposable child, never a game or Steam."""
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from hub import match_cleanup as cleanup


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows process handles')
def test_detached_cleanup_survives_launcher_exit_and_requires_saved_receipt(tmp_path):
    ready = threading.Event()
    match_id, job_id = 'f' * 16, uuid.uuid4().hex
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            value = {'ok': True, 'match_id': match_id, 'data_collected': ready.is_set(),
                     'close_allowed': ready.is_set(), 'close_after': 0}
            data = json.dumps(value).encode()
            self.send_response(200); self.send_header('Content-Length', str(len(data))); self.end_headers()
            self.wfile.write(data)
        def log_message(self, *_): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(90)'],
                             creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        # The handle belongs to this test's harmless child, with PID + creation time + full image.
        handle = cleanup._open_process(child.pid)
        try: identity = cleanup._identity(handle, child.pid)
        finally: cleanup._apis()[0].CloseHandle(handle)
        job = {'schema': 1, 'job_id': job_id, 'match_id': match_id, 'steam_id': '76561198000000001',
               'token': 'local-test-only', 'expected_exe': identity['exe'], 'identity': identity}
        cleanup._write(cleanup._job_path(job_id), job)
        env = {**os.environ, 'HUB_API_BASE': f'http://127.0.0.1:{server.server_port}'}
        launcher = subprocess.run([sys.executable, '-c',
            'from hub.match_cleanup import ensure_worker; import sys; ensure_worker(sys.argv[1])', job_id],
            cwd=Path(__file__).resolve().parents[1], env=env, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW)
        assert launcher.returncode == 0
        deadline = time.monotonic() + 12
        while len(requests) < 2 and time.monotonic() < deadline: time.sleep(.2)
        assert len(requests) >= 2 and child.poll() is None
        ready.set()
        child.wait(timeout=40)
        deadline = time.monotonic() + 6
        while cleanup.read_status(job_id)['state'] != 'done' and time.monotonic() < deadline: time.sleep(.2)
        status = cleanup.read_status(job_id)
        assert status['state'] == 'done' and status['reason'] == 'verified_exit'
        assert status['attempts'] == 1
    finally:
        if child.poll() is None: child.terminate(); child.wait(timeout=5)
        server.shutdown(); server.server_close()
