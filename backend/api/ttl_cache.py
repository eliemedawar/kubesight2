"""Shared thread-safe TTL cache with single-flight computation.

Used to absorb repeated expensive work (kubectl subprocesses, external HTTP
calls) across concurrent requests and users. Concurrent callers for the same
key share one in-flight computation instead of each spawning their own.

Caching is disabled while the Flask app runs with TESTING=True so unit tests
that patch providers never see stale values.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple


def caching_disabled() -> bool:
    try:
        from flask import current_app

        return bool(getattr(current_app, "config", {}).get("TESTING"))
    except Exception:
        return False


class _Flight:
    __slots__ = ("event", "ok", "value", "error")

    def __init__(self):
        self.event = threading.Event()
        self.ok = False
        self.value: Any = None
        self.error: Optional[BaseException] = None


class TTLCache:
    """TTL cache with per-key single-flight and optional stale-while-revalidate.

    - ``get_or_compute(key, ttl, compute)`` returns a fresh cached value when
      available; otherwise exactly one caller computes while concurrent callers
      for the same key wait on the same result.
    - ``stale_ttl`` extends how long an *expired* value may still be served:
      the caller gets the stale value immediately while one background thread
      recomputes. The compute callable must not depend on request/app context
      when ``stale_ttl`` is used — it runs on a plain daemon thread.
    - Failed computations are never cached; the leader's exception propagates
      to every waiter of that flight.
    - ``invalidate(prefix)`` drops entries whose string key starts with the
      prefix, so mutation endpoints can precisely evict one cluster/namespace.
    """

    def __init__(self, name: str = ""):
        self.name = name
        self._lock = threading.Lock()
        # key -> (fresh_until, stale_until, value)
        self._entries: Dict[Any, Tuple[float, float, Any]] = {}
        self._inflight: Dict[Any, _Flight] = {}

    def get(self, key: Any) -> Optional[Any]:
        with self._lock:
            entry = self._entries.get(key)
            if entry and entry[0] > time.monotonic():
                return entry[2]
        return None

    def set(self, key: Any, value: Any, ttl: float, stale_ttl: float = 0) -> None:
        now = time.monotonic()
        with self._lock:
            self._entries[key] = (now + ttl, now + ttl + max(stale_ttl, 0), value)

    def _spawn_background_refresh(
        self, key: Any, ttl: float, stale_ttl: float, compute: Callable[[], Any]
    ) -> None:
        """Recompute an expired-but-servable entry off the request thread."""
        flight = _Flight()

        def _refresh() -> None:
            try:
                value = compute()
            except BaseException:
                pass  # keep serving stale until it ages out of the stale window
            else:
                self.set(key, value, ttl, stale_ttl)
            finally:
                with self._lock:
                    if self._inflight.get(key) is flight:
                        del self._inflight[key]
                flight.event.set()

        thread = threading.Thread(
            target=_refresh, name=f"ttl-cache-refresh-{self.name}", daemon=True
        )
        with self._lock:
            if key in self._inflight:
                return  # someone else is already refreshing
            self._inflight[key] = flight
        thread.start()

    def get_or_compute(
        self,
        key: Any,
        ttl: float,
        compute: Callable[[], Any],
        stale_ttl: float = 0,
    ) -> Any:
        if caching_disabled():
            return compute()

        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry and entry[0] > now:
                return entry[2]
            serve_stale = bool(entry and stale_ttl > 0 and entry[1] > now)
            stale_value = entry[2] if serve_stale else None
            flight = self._inflight.get(key)
            if flight is None and not serve_stale:
                flight = _Flight()
                self._inflight[key] = flight
                leader = True
            else:
                leader = False

        if serve_stale:
            # Instant response from the stale window; refresh in the background.
            self._spawn_background_refresh(key, ttl, stale_ttl, compute)
            return stale_value

        if not leader:
            # Bounded wait so a stuck leader (e.g. hung kubectl beyond its own
            # timeout) cannot pile up waiters forever; fall back to computing.
            if flight.event.wait(timeout=120) and flight.ok:
                return flight.value
            if flight.error is not None:
                raise flight.error
            return compute()

        try:
            value = compute()
        except BaseException as exc:
            flight.error = exc
            raise
        else:
            flight.ok = True
            flight.value = value
            self.set(key, value, ttl, stale_ttl)
            return value
        finally:
            with self._lock:
                if self._inflight.get(key) is flight:
                    del self._inflight[key]
            flight.event.set()

    def invalidate(self, prefix: Any = None) -> None:
        with self._lock:
            if prefix is None:
                self._entries.clear()
                return
            if isinstance(prefix, str):
                stale = [
                    key
                    for key in self._entries
                    if isinstance(key, str) and key.startswith(prefix)
                ]
            else:
                stale = [key for key in self._entries if key == prefix]
            for key in stale:
                self._entries.pop(key, None)
