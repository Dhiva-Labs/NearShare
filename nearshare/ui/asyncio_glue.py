"""Bridges GTK's GLib main loop and the core service's asyncio world.

NearShareService (and everything under nearshare.core) is asyncio-
native: the TCP receive server, mDNS advertiser/browser, and control
socket all run as coroutines on an event loop. GTK4/libadwaita instead
run their own loop (GLib's main context) on the thread that calls
`Adw.Application.run()`, and GTK widgets may only be touched from that
thread. The two loops don't share a thread, so this module is the one
place that crosses between them, in both directions:

  * UI thread -> asyncio thread: `AsyncioThread.run_coroutine()` uses
    `asyncio.run_coroutine_threadsafe`, the one asyncio entry point
    documented safe to call from a different thread than the loop runs
    on. It returns a `concurrent.futures.Future`; if given `on_done`, that
    callback is scheduled back onto the GTK thread with `GLib.idle_add`
    once the coroutine completes, so UI code can update widgets safely.

  * asyncio thread -> UI thread: core callbacks (`Events.on_progress`,
    `service.on_peers_changed`, etc.) fire synchronously on the asyncio
    thread. Handlers must never touch a GTK widget directly from there;
    wrap the widget mutation in `idle(fn, *args)` (see app.py, where
    every `Events(...)` callback is a one-line lambda that does exactly
    that).

  * A third case, used only for `Events.on_transfer_request`: that
    callback is `async` and the core *awaits its result* on the asyncio
    thread (it needs the accept/decline boolean before continuing the
    handshake). But the accept/decline decision comes from a GTK dialog
    response, which fires on the UI thread. `bridge_future_to_asyncio`
    creates an `asyncio.Future` already bound to the right loop plus a
    thread-safe setter, so the UI thread can resolve a value the asyncio
    thread is `await`-ing.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from typing import Any, Callable, Coroutine, TypeVar

from gi.repository import GLib

log = logging.getLogger("nearshare.ui.glue")

T = TypeVar("T")


class AsyncioThread:
    """Owns a dedicated background thread running its own asyncio loop."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="nearshare-asyncio")
        self._started = threading.Event()

    def start(self) -> None:
        self._thread.start()
        self._started.wait()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.call_soon(self._started.set)
        self.loop.run_forever()

    def run_coroutine(
        self,
        coro: Coroutine[Any, Any, T],
        on_done: Callable[[T | None, BaseException | None], None] | None = None,
    ) -> concurrent.futures.Future:
        """Schedule `coro` on the asyncio loop from any thread.

        Returns the `concurrent.futures.Future` immediately. If `on_done`
        is given it is invoked as `on_done(result, exception)` on the GTK
        thread once the coroutine finishes (exactly one of the two
        arguments is non-None).
        """
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        if on_done is not None:
            def _relay(f: concurrent.futures.Future) -> None:
                # A cancelled future raises from .exception() instead of
                # returning it; hand the cancellation on like any error.
                if f.cancelled():
                    exc: BaseException | None = concurrent.futures.CancelledError()
                else:
                    exc = f.exception()
                result = None if exc is not None else f.result()
                GLib.idle_add(on_done, result, exc)
            fut.add_done_callback(_relay)
        return fut

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5)


def idle(fn: Callable[..., None], *args: Any) -> None:
    """Marshal a core-thread callback onto the GTK main loop.

    `GLib.idle_add` re-invokes its callback forever unless it returns
    False, which isn't the calling convention any of our handlers use --
    this wraps `fn` so callers can write plain `idle(self._update_x, a, b)`
    without remembering the `return False`.
    """
    def _call() -> bool:
        fn(*args)
        return False
    GLib.idle_add(_call)


class Coalescer:
    """Collapse a burst of keyed updates from the asyncio thread into at
    most one GTK-thread call per key every `interval_ms`.

    Progress callbacks fire once per 512 KB chunk; on a fast link that is
    hundreds of main-loop wake-ups a second, each relabelling a row, for
    changes nobody can see. Only the newest value per key is kept.
    """

    def __init__(self, deliver: Callable[[Any, Any], None],
                 interval_ms: int = 100) -> None:
        self._deliver = deliver
        self._interval_ms = interval_ms
        self._lock = threading.Lock()
        self._pending: dict[Any, Any] = {}
        self._scheduled = False

    def push(self, key: Any, value: Any) -> None:
        with self._lock:
            self._pending[key] = value
            if self._scheduled:
                return
            self._scheduled = True
        GLib.timeout_add(self._interval_ms, self._flush)

    def drop(self, key: Any) -> None:
        """Forget a pending update, e.g. once its transfer has finished."""
        with self._lock:
            self._pending.pop(key, None)

    def _flush(self) -> bool:
        with self._lock:
            pending, self._pending = self._pending, {}
            self._scheduled = False
        for key, value in pending.items():
            self._deliver(key, value)
        return False


def bridge_future_to_asyncio(
    loop: asyncio.AbstractEventLoop,
) -> tuple[asyncio.Future, Callable[[Any], None]]:
    """An asyncio.Future bound to `loop`, plus a thread-safe setter.

    Lets a GTK callback running on the UI thread hand a result to a
    coroutine that is `await`-ing this future on the asyncio thread (see
    app.py's transfer-request handling).
    """
    future: asyncio.Future = loop.create_future()

    def set_result(value: Any) -> None:
        loop.call_soon_threadsafe(_safe_set_result, future, value)

    return future, set_result


def _safe_set_result(future: asyncio.Future, value: Any) -> None:
    # The dialog could theoretically respond twice (e.g. a stray second
    # signal emission); asyncio.Future.set_result on an already-done
    # future raises InvalidStateError, so guard it.
    if not future.done():
        future.set_result(value)
