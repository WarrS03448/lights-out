"""WebPanel — the pywebview stand-in for CompetitivePanel.

It implements the five-method panel protocol (see hub/competitive.py, "the panel protocol") so
an unchanged `LiveSession` drives it exactly as it drives the Tk panel:

    post(fn)  after(ms, fn)  on_change()  save_auth(payload)  map_pool()

The tail of each seam is the only thing that differs from Tk:
  * post/after      -> the UiScheduler's single UI thread (not root.after / app.q)
  * on_change       -> build a JSON snapshot and push it to JS via evaluate_js (not _draw_body)
  * save_auth       -> state.json, same as the Tk panel
  * map_pool        -> the ranked pool from the catalogue, same rule as the Tk panel

pywebview is NOT imported here: the `window` is injected (a duck-typed object with
`evaluate_js`), so this module is fully testable headless with `window=None`.
"""
import json
import os
import queue
import threading
import time

from .. import competitive as competitive_mod
from .. import sounds as sounds_mod
from .. import telemetry
from .. import state as state_mod
from . import snapshot as snapshot_mod
from .scheduler import UiScheduler


# How many pushes may pile up before the oldest STATE push is dropped. The page re-syncs from GET
# /state every 300 ms, so a dropped state push costs nothing; events are never dropped, which is why
# they are tagged and skipped by the eviction below.
PUSH_QUEUE_MAX = 64
# HUB_PUSH_TRACE=1 prints any evaluate_js slower than HUB_PUSH_TRACE_MS (default 250) to the log.
# Off by default: this is a diagnostic for "the hub feels laggy", not something to spam a release.
PUSH_TRACE = os.environ.get("HUB_PUSH_TRACE") == "1"
try:
    PUSH_TRACE_MS = float(os.environ.get("HUB_PUSH_TRACE_MS") or 250)
except ValueError:
    PUSH_TRACE_MS = 250.0

# THE REFRESH TICK. How often the UI thread rebuilds the snapshot with nobody having asked it to.
#
# Everything the page shows comes from `last_payload`, and `last_payload` is only rebuilt inside
# on_change() — so a screen is only as live as the number of things that think to call it. Most of
# the app's state does call it (the session on every event, the verbs, the progress callbacks), but
# some of what a screen draws is read from OUTSIDE the session at snapshot time: what state.json
# says is installed, whether Bodycam is running, whether the catalogue has landed. Nothing tells
# the panel when those change, so a screen that was correct when it was last emitted simply stayed
# wrong until something unrelated happened to re-emit — and switching tab and back was the reliable
# way to force one. Sam, 2026-09-16: "when there's an install or update, the status of the item
# after it finishes doesn't change visually until you leave and come back."
#
# So the UI thread re-derives the snapshot once a second, forever. It is NOT a push: on_change
# de-dups on the serialised payload, so a tick that finds nothing new costs one snapshot build and
# stops there — no evaluate_js, no new /state body, no re-render in the page. A second is well
# inside "did not notice": the page polls /state every 300 ms, and during a match the session
# already re-emits several times a second on its own.
REFRESH_MS = 1000


class WebPanel:
    """Backs one LiveSession and forwards its state to the webview.

    `app`       : anything with `.state` (dict, persisted via state.py) and `.catalogue`
                  (dict|None). The Tk `HubApp` shape, minus Tk. Used for save_auth/map_pool
                  and to know whether the ranked gamemode is installed.
    `scheduler` : a UiScheduler (real) or InlineScheduler (tests). If None, a UiScheduler is made.
    `window`    : a pywebview Window (or any object with `.evaluate_js(str)`), or None headless.
    """

    def __init__(self, app, scheduler=None, window=None):
        self.app = app
        self.scheduler = scheduler if scheduler is not None else UiScheduler()
        self.window = window
        self.chrome = None                   # a window.WindowControl when the frame is ours to
                                             # draw (Windows); None means a native title bar
        # The CloseToTray that owns the window's X (shell.run sets it). None headless, and None is
        # why quit() below has a fallback: without it the X handler swallows a destroy() and the
        # hub would merely hide when something asked it to end.
        self.closer = None
        self.party_code_hidden = False       # the streamer toggle; Copy still works while hidden
        self.last_state = None               # the most recent snapshot dict (tests read this)
        self.last_payload = None             # its JSON string (the HTTP bridge serves this verbatim)
        self._sig = None                     # fingerprint of the last snapshot pushed
        self._closed = False
        # Transient cues (toasts/sounds) the page pulls via GET /events?since=N. A bounded ring with
        # monotonic seqs, since polling can miss the moment an event fires (see httpbridge.py).
        self._events = []
        self._event_seq = 0
        self._events_lock = threading.Lock()
        # THE PUSH QUEUE. evaluate_js is SYNCHRONOUS - pywebview's edgechromium backend blocks on
        # `semaphore.acquire()` with NO timeout until the page answers - so calling it from the UI
        # thread means one slow JS round trip stalls EVERY posted callback behind it: clicks, SSE
        # events, timers, the lot. Measured on Sam's machine 2026-09-15: a UI-thread round trip
        # took 3.0-4.2 s, thirty times out of thirty, while the same evaluate_js in an empty
        # pywebview window took 0.4 ms and the page's own onState() render took 0.2 ms.
        #
        # The push is BELT AND BRACES anyway. httpbridge.py exists precisely because this channel
        # "proved unreliable in the packaged app", and core.js says so itself: the 300 ms GET /state
        # poll "is NOT the primary channel". So the worst case of pushing late, or dropping a state
        # push entirely, is up to 300 ms of extra latency - against a UI that currently freezes for
        # three seconds a click.
        self._push_q = queue.Queue(maxsize=PUSH_QUEUE_MAX)
        self._push_thread = None
        self._push_busy = False              # an evaluate_js is running right now
        self._refreshing = False             # the REFRESH_MS tick is booked (see _start_refresh)
        # Rolling evaluate_js timings, newest last, exposed through push_stats() so a laggy hub can
        # be diagnosed from the outside instead of by guesswork.
        self._push_ms = []
        self.view = "competitive"            # the active screen; the nav switches it (set_view)
        # The match-found cue fires on the EDGE into "found", never on the redraws inside it -
        # on_change runs several times a second during the accept window (each tick, each
        # accept), so a level test here would ring twenty times. None means "no phase seen
        # yet", which is deliberately NOT "found": a hub that starts up straight into a
        # running accept window (a restart, a reconnect) should ring, because that player has
        # seconds to act and has not heard it.
        self._last_phase_seen = None
        # App self-update runtime state, mirrored into the snapshot's `update` slice by
        # snapshot.state_snapshot. Only the download/launch progress lives here; whether an
        # update is available/forced is derived from the catalogue each snapshot. "idle" until
        # the user clicks Update (apply_hub_update -> start_hub_update).
        self.update_state = {"status": "idle", "progress": 0, "error": ""}
        # LiveSession reaches its view only through this object (the five methods below).
        self.session = competitive_mod.LiveSession(self)

    # ------------------------------------------------------------ lifecycle
    def start(self):
        """Start the scheduler, then do all startup work ON the scheduler thread.

        pywebview calls this from its `on_started` WORKER thread, not the scheduler thread. If we
        restored the account here, `_restore_account()` -> `adopt_account()` -> `_connect()` would
        start the SSE reader, which `post`s events the scheduler runs CONCURRENTLY with this worker
        still finishing start() — two threads mutating one session. So we only start the scheduler
        here and hand the rest to it; every session mutation then happens on the one UI thread."""
        self.scheduler.start()
        self.scheduler.post(self._start_on_ui_thread)

    def _start_on_ui_thread(self):
        """Restore a saved account, start the watchdog and the refresh tick, and emit the first
        snapshot. Runs on the scheduler (UI) thread, so it never races the SSE reader it may
        spin up."""
        self._restore_account()
        self.session._start_watchdog()
        self._start_refresh()
        self.on_change()

    # ------------------------------------------------------------ the refresh tick
    def _start_refresh(self):
        """Begin re-deriving the snapshot on a timer (see REFRESH_MS). Idempotent."""
        if self._closed or self._refreshing:
            return
        self._refreshing = True
        self.after(REFRESH_MS, self._refresh_tick)

    def _refresh_tick(self):
        """UI thread: rebuild the snapshot, then book the next tick — whatever happened.

        The reschedule is in a `finally` on purpose. This is the only thing keeping the screens
        live, so one bad snapshot must not be the end of it: on_change already swallows its own
        failures, and anything that gets past it would otherwise stop the heartbeat for the rest
        of the session and put the app straight back to needing a tab switch."""
        if self._closed:
            self._refreshing = False
            return
        try:
            self.on_change()
        finally:
            if self._closed:
                self._refreshing = False
            else:
                self.after(REFRESH_MS, self._refresh_tick)

    def shutdown(self):
        self._closed = True
        session = getattr(self, "session", None)
        if session is not None and hasattr(session, "_disconnect"):
            try:
                session._disconnect()
            except Exception:            # noqa: BLE001
                pass
        self.scheduler.stop()

    def quit(self):
        """End the hub for real (not hide it to the tray). True when something took the request.

        The tray menu's "Close Lights Out" is the only other real quit in the web UI, and this goes
        through the same door: CloseToTray.quit() sets the flag that lets the window's `closing`
        handler through. Falling back to window.destroy() without it would just HIDE the window,
        because the X handler is still installed.

        Headless (no window, no closer) it returns False rather than killing the process, so a test
        that drives the uninstall verb does not take the test runner with it."""
        closer = getattr(self, "closer", None)
        if closer is not None and hasattr(closer, "quit"):
            try:
                closer.quit()
                return True
            except Exception:            # noqa: BLE001 — fall through to the window
                pass
        window = getattr(self, "window", None)
        if window is not None and hasattr(window, "destroy"):
            try:
                window.destroy()
                return True
            except Exception:            # noqa: BLE001
                pass
        return False

    # ------------------------------------------------------------ the panel protocol
    def post(self, fn):
        """Run fn on the UI thread. Workers (sign-in, action POSTs, the SSE reader) use this."""
        self.scheduler.post(fn)

    def after(self, ms, fn):
        """Schedule fn on the UI thread after ms milliseconds (the session's timers)."""
        return self.scheduler.after(ms, fn)

    def on_change(self):
        """The session moved: (re)build the snapshot and push it to JS."""
        self._phase_changed()
        try:
            snap = snapshot_mod.state_snapshot(self.session, self)
        except Exception as exc:         # noqa: BLE001 — a bad snapshot must not kill the tick
            self._log("snapshot", exc)
            return
        # last_state always reflects the current snapshot so ready()/get_state() are accurate even
        # when we skip the push below.
        self.last_state = snap
        payload = json.dumps(snap)
        self.last_payload = payload      # the HTTP /state endpoint serves this string verbatim
        # De-dup: periodic stats/online pushes call on_change() with an unchanged snapshot. Skipping
        # the evaluate_js when nothing changed spares JS a full innerHTML rebuild (which would wipe a
        # half-typed party join-code input; core.js also guards that, but this avoids the churn).
        if payload == self._sig:
            return
        self._sig = payload
        self._push_state(payload)

    # ------------------------------------------------------------ the match-found cue
    def _phase_changed(self):
        """Fire anything that belongs to ENTERING a phase rather than being in it.

        The Tk CompetitivePanel has had this since the cue existed; the web panel did not, which
        is why the hub went silent on a found match the moment the web UI became the default
        (Sam, 2026-09-15). The Test button in Settings kept working the whole time, because that
        calls sounds.play_match_found directly - so the sound was never the broken part."""
        phase = getattr(self.session, "phase", None)
        if phase == self._last_phase_seen:
            return
        was, self._last_phase_seen = self._last_phase_seen, phase
        if phase == "found" and was != "found":
            self.play_match_found()

    def play_match_found(self):
        """The cue, at whatever the slider says now. Never lets audio cost a match."""
        try:
            volume = sounds_mod.volume_for_state(getattr(self.app, "state", None))
            if volume <= 0:
                return False
            # `self.after` is the scheduler's, so any repeat lands on the UI thread and dies
            # with the panel. play_once itself is winsound's ASYNC flag - it returns at once
            # and does not block this thread on the audio device.
            return sounds_mod.play_match_found(self.after, volume)
        except Exception as exc:         # noqa: BLE001 - no audio device must never cost a match
            # ...but say so somewhere. A bare pass here once hid a TypeError, and the only
            # symptom was silence (2026-09-14).
            self._log("sound", exc)
            return False

    def save_auth(self, payload):
        """Persist (dict) or clear (None) the signed-in account in state.json (as the Tk panel)."""
        try:
            state_mod.update_fields({"auth": payload})
            self.app.state["auth"] = payload
            return True
        except Exception:                # noqa: BLE001 — a read-only state dir must not break sign-in
            return False

    def map_pool(self):
        """The ranked pool: the gamemode's maps (from the catalogue when listed), minus the
        excluded maps — the same rule as CompetitivePanel.map_pool."""
        if getattr(self.session, 'ranked_mode', competitive_mod.COMPETITIVE_MODE_ID) == 'BB1':
            return ['Paintball', 'Airsoft', 'BombHouse']
        cat = getattr(self.app, "catalogue", None) or {}
        for e in (cat.get("gamemodes") or []):
            if e.get("id") == competitive_mod.COMPETITIVE_MODE_ID:
                maps = e.get("maps") or (e.get("manifest") or {}).get("maps")
                if isinstance(maps, list) and len(maps) >= 3:
                    pool = competitive_mod.competitive_pool([str(m) for m in maps])
                    if len(pool) >= 3:
                        return pool
        return competitive_mod.competitive_pool(competitive_mod.DEFAULT_MAPS)

    # ------------------------------------------------------------ view-only helpers
    def toggle_hide_code(self):
        self.party_code_hidden = not self.party_code_hidden
        self.on_change()

    def set_view(self, name):
        """Switch the active screen (nav) and re-emit so the router shows it."""
        name = str(name or "")
        if name and name != self.view:
            self.view = name
            self.on_change()

    def set_language(self, code):
        """Change language and re-emit the snapshot with the new strings (plan: i18n)."""
        from .. import i18n
        if code not in i18n.CODES or code == i18n.get_language():
            return
        i18n.set_language(code)
        try:
            self.app.state["language"] = code
            state_mod.update_fields({"language": code})
        except Exception:                # noqa: BLE001
            pass
        self.on_change()

    # ------------------------------------------------------------ app self-update
    def start_hub_update(self):
        """Download the newer hub installer on a worker thread, then launch it (UI-thread entry).

        Mirrors the Tk path (hub/app.py _start_update_download + _launch_update): the download is
        safe at any moment — it writes one file into <state>/updates and touches nothing else — so
        it runs on a daemon thread and never blocks the UI thread. Progress is posted back onto the
        UI thread (like the Tk queue's ``upd_progress``) so on_change re-emits the snapshot. When the
        installer launches it closes this hub, replaces the files and relaunches the new hub itself
        (/LAUNCHHUB=1); the match survives because it lives on the service, not this process.

        Self-update only makes sense from the installed exe: on a dev checkout ``own_exe()`` is None
        and there is no installed hub for the installer to replace, so we refuse."""
        from .. import update as update_mod
        from ..catalogue import version_newer
        from ..version import HUB_VERSION
        if self.update_state.get("status") == "downloading":
            return                                 # already in flight
        if update_mod.own_exe() is None:
            return                                 # dev checkout: nothing installed to update
        info = ((getattr(self.app, "catalogue", None) or {}).get("hub")) or {}
        url = info.get("download_url")
        if not (url and version_newer(info.get("version", ""), HUB_VERSION)):
            return
        self.update_state = {"status": "downloading", "progress": 0, "error": ""}
        telemetry.emit("update.start", action="hub_update")
        self.on_change()

        def work():
            last = {"pct": -1}

            def progress(done, total):
                pct = (done * 100 // total) if total else 0
                if pct != last["pct"]:
                    last["pct"] = pct
                    self.post(lambda p=pct: self._set_update_progress(p))

            try:
                dest = update_mod.dest_path(url, info.get("version", ""))
                path = update_mod.download(url, dest, progress=progress,
                                           expect_sha=info.get("sha256"))
                telemetry.emit("update.downloaded", action="hub_update")
            except Exception as e:                 # noqa: BLE001 — offline / bad sha / disk full
                self.post(lambda msg=str(e): self._set_update_error(msg))
                return
            try:
                update_mod.launch(path, info.get("kind"))
                telemetry.emit("update.launched", action="hub_update")
                telemetry.shutdown()
            except Exception as e:                 # noqa: BLE001 — Defender quarantine, etc.
                self.post(lambda msg=str(e): self._set_update_error(msg))
                return
            # The installer is now running; it closes this hub and relaunches the new one.

        threading.Thread(target=work, name="hub-update", daemon=True).start()

    def _set_update_progress(self, pct):
        self.update_state = {"status": "downloading", "progress": int(pct), "error": ""}
        self.on_change()

    def _set_update_error(self, msg):
        telemetry.emit("update.failed", action="hub_update", severity="error")
        self.update_state = {"status": "error",
                             "progress": self.update_state.get("progress", 0),
                             "error": str(msg)}
        self.on_change()

    # ------------------------------------------------------------ Python -> JS
    def _push_state(self, payload):
        self._evaluate("window.__hub && window.__hub.onState(%s)" % payload)

    def push_event(self, event: dict):
        """A transient cue (a toast, a sound) — not durable state. Buffered for the HTTP bridge's
        GET /events poll, and also pushed via evaluate_js (harmless if that channel is dead)."""
        with self._events_lock:
            self._event_seq += 1
            self._events.append(dict(event, seq=self._event_seq))
            if len(self._events) > 50:
                self._events = self._events[-50:]
        self._evaluate("window.__hub && window.__hub.onEvent(%s)" % json.dumps(event), kind="event")

    def events_since(self, seq):
        """Events with a seq greater than `seq`, for the HTTP bridge's GET /events?since=N."""
        try:
            seq = int(seq)
        except (TypeError, ValueError):
            seq = 0
        with self._events_lock:
            return [e for e in self._events if e.get("seq", 0) > seq]

    def _evaluate(self, script, kind="state"):
        """Hand a script to the pusher thread. NEVER blocks the caller.

        The caller is the UI thread, and it is the only one there is: everything the session does
        is serialised on it. Blocking it on a webview round trip is what made every click cost
        three seconds."""
        if self.window is None or self._closed:
            return
        self._start_pusher()
        try:
            self._push_q.put_nowait((kind, script))
        except queue.Full:
            # Full means the webview is not draining. Drop the OLDEST state push - the page's poll
            # will carry that state anyway - and never drop an event, which has no other channel
            # that guarantees delivery ordering with its seq.
            try:
                dropped = []
                while True:
                    item = self._push_q.get_nowait()
                    if item[0] == "state":
                        break
                    dropped.append(item)
                for item in dropped:
                    self._push_q.put_nowait(item)
                self._push_q.put_nowait((kind, script))
            except queue.Empty:
                pass
            except queue.Full:
                pass

    def _start_pusher(self):
        if self._push_thread is not None or self._closed:
            return
        self._push_thread = threading.Thread(target=self._push_loop, name="hub-push", daemon=True)
        self._push_thread.start()

    def _push_loop(self):
        while not self._closed:
            try:
                item = self._push_q.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                return
            _kind, script = item
            self._push_busy = True
            started = time.monotonic()
            try:
                self.window.evaluate_js(script)
            except Exception as exc:     # noqa: BLE001 — the webview may be gone mid-shutdown
                self._log("evaluate_js", exc)
            ms = (time.monotonic() - started) * 1000.0
            self._push_busy = False
            self._push_ms.append(ms)
            if len(self._push_ms) > 50:
                del self._push_ms[:-50]
            if PUSH_TRACE and ms >= PUSH_TRACE_MS:
                print("[hub-push] evaluate_js took %.0f ms (queue=%d)"
                      % (ms, self._push_q.qsize()), flush=True)

    def flush_pushes(self, timeout=2.0):
        """Wait for the pusher to drain. For shutdown and for tests, which must be able to assert
        on a channel that is now asynchronous - never called on the UI thread in normal running,
        because waiting for the webview there is the whole bug this queue exists to fix."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        while time.monotonic() < deadline:
            # empty is not enough: the pusher pops an item BEFORE it calls evaluate_js, so a
            # queue that has just drained may still have a call in flight.
            if self._push_q.empty() and not self._push_busy:
                return True
            time.sleep(0.005)
        return self._push_q.empty() and not self._push_busy

    def push_stats(self):
        """What the webview channel is costing, for the HTTP bridge's GET /debug/push."""
        ms = list(self._push_ms)
        ms_sorted = sorted(ms)
        return {
            "samples": len(ms),
            "queued": self._push_q.qsize(),
            "last_ms": round(ms[-1], 1) if ms else None,
            "p50_ms": round(ms_sorted[len(ms_sorted) // 2], 1) if ms_sorted else None,
            "max_ms": round(ms_sorted[-1], 1) if ms_sorted else None,
        }

    # ------------------------------------------------------------ account restore
    def _restore_account(self):
        self.session.restore_account((getattr(self.app, "state", {}) or {}).get("auth") or {})

    # ------------------------------------------------------------ logging
    def _log(self, where, exc):
        try:
            import traceback
            from .. import paths
            with open(paths.log_file(), "a", encoding="utf-8") as f:
                f.write("\n[webui:%s] %s" % (where, "".join(
                    traceback.format_exception(type(exc), exc, exc.__traceback__))))
        except Exception:                # noqa: BLE001
            pass
