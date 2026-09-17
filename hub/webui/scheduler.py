"""The web UI's single UI thread.

The Tk app has one: `root.after`/`root.mainloop` on the main thread, and every worker hops
onto it through `app.q` + the 100 ms pump (hub/app.py). pywebview has no such per-frame
scheduler exposed to Python, so the web UI provides its own: ONE background thread that owns a
posted-callback queue and a heap of timers. It IS the "UI thread" the session assumes — every
`panel.post(fn)` and `panel.after(ms, fn)` runs here, so all session state is mutated on this
one thread and never races (the SSE reader, sign-in and action POSTs all `post` onto it).

The only thing that crosses back to the real GUI thread is `window.evaluate_js` (WebPanel does
that), which pywebview marshals internally and is safe to call from any thread.

Two implementations:
  * `UiScheduler`    — the real background-thread scheduler used by the app.
  * `InlineScheduler`— a deterministic stand-in for tests: `post` runs now, `after` queues for
                       an explicit `pump()`. No thread, no clock.
"""
import heapq
import queue
import threading
import time

# Sentinel put on the queue to wake the loop when a timer is added from another thread, so the
# blocking get() recomputes its timeout instead of sleeping past the new, sooner timer.
_WAKE = object()


class UiScheduler:
    """A single daemon thread: a callback queue plus scheduled timers."""

    def __init__(self, on_error=None):
        self._q = queue.Queue()
        self._timers = []            # heap of [due_monotonic, seq, fn]
        self._lock = threading.Lock()
        self._seq = 0
        self._stop = threading.Event()
        self._thread = None
        self._on_error = on_error or (lambda exc: None)

    # ------------------------------------------------------------ lifecycle
    def start(self):
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="hub-ui", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._q.put(_WAKE)           # unblock the loop so it can see the stop flag
        self._thread = None

    # ------------------------------------------------------------ the panel protocol tail
    def post(self, fn):
        """Run fn on the UI thread as soon as possible."""
        self._q.put(fn)

    def after(self, ms, fn):
        """Run fn on the UI thread after ms milliseconds. Returns an opaque handle."""
        due = time.monotonic() + max(0, int(ms)) / 1000.0
        with self._lock:
            self._seq += 1
            item = [due, self._seq, fn]
            heapq.heappush(self._timers, item)
        self._q.put(_WAKE)           # recompute the wait: this timer may fire before the old next
        return item

    # ------------------------------------------------------------ the loop
    def _next_timeout(self):
        with self._lock:
            if not self._timers:
                return None
            return max(0.0, self._timers[0][0] - time.monotonic())

    def _due_callbacks(self):
        now = time.monotonic()
        ready = []
        with self._lock:
            while self._timers and self._timers[0][0] <= now:
                ready.append(heapq.heappop(self._timers)[2])
        return ready

    def _fire(self, fn):
        try:
            fn()
        except Exception as exc:     # noqa: BLE001 — one bad callback must not kill the UI thread
            self._on_error(exc)

    def _run(self):
        while not self._stop.is_set():
            timeout = self._next_timeout()
            try:
                item = self._q.get(timeout=timeout)
            except queue.Empty:
                item = None
            if self._stop.is_set():
                return
            if item is not None and item is not _WAKE:
                self._fire(item)
            for fn in self._due_callbacks():
                if self._stop.is_set():
                    return
                self._fire(fn)


class InlineScheduler:
    """Deterministic scheduler for tests: post() runs now, after() queues for pump()."""

    def __init__(self):
        self.pending = []            # list of (fn) queued by after(), oldest first

    def start(self):
        pass

    def stop(self):
        pass

    def post(self, fn):
        fn()

    def after(self, ms, fn):
        self.pending.append(fn)
        return fn

    def pump(self, limit=1):
        """Fire up to `limit` queued timers, oldest first. Returns how many fired."""
        n = 0
        while self.pending and n < limit:
            self.pending.pop(0)()
            n += 1
        return n
