"""Hidden, opt-in WebView2 runtime smoke test, driven by test_relay_live.cjs."""
import ctypes
import json
import sys
import threading
import time
import urllib.request
from urllib.parse import urlsplit
import webview

windows = [webview.create_window("Lights Out relay verification", url=url, hidden=True,
                                 width=640, height=480) for url in sys.argv[1:3]]
parts = urlsplit(sys.argv[1])
base = "%s://%s" % (parts.scheme, parts.netloc)
passed = False
script = """(async()=>{const result=[]; for(const pc of window.testConnections){
if(pc.connectionState!=='connected')continue;const stats=await pc.getStats();
for(const row of stats.values())if(row.type==='transport'&&row.selectedCandidatePairId){
const pair=stats.get(row.selectedCandidatePairId);result.push({policy:pc.getConfiguration().iceTransportPolicy,
local:stats.get(pair.localCandidateId)?.candidateType,remote:stats.get(pair.remoteCandidateId)?.candidateType});}}
return result;})()"""


def check():
    global passed
    try:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            with urllib.request.urlopen(base + "/smoke-status", timeout=2) as response:
                if json.load(response)["ready"]:
                    break
            time.sleep(0.5)
        else:
            raise RuntimeError("No usable relay measurements in WebView2")
        routes = []
        for window in windows:
            done = threading.Event()
            def callback(value):
                routes.append(value)
                done.set()
            window.evaluate_js(script, callback=callback)
            if not done.wait(5):
                raise RuntimeError("WebView2 statistics callback timed out")
        request = urllib.request.Request(base + "/smoke-result", data=json.dumps(routes).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=2) as response:
            assert response.status == 200
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
        kernel.GetModuleHandleW.restype = ctypes.c_void_p
        assert not kernel.GetModuleHandleW("steam_api64.dll"), "Steam API unexpectedly loaded"
        passed = True
    except Exception as error:
        print(str(error), flush=True)
    finally:
        for window in windows:
            window.destroy()


webview.start(check, gui="edgechromium", http_server=False, private_mode=True)
sys.exit(0 if passed else 1)
