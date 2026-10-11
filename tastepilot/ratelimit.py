"""Per-client rate limiting for the demo server's POST routes.

Design choice — fixed window, not a token bucket. A token bucket
refills continuously, so the answer to "how many requests may this
client still make, and when may it try again?" depends on fractional
token arithmetic; a fixed window gives the demo an exact, auditable
contract instead: at most ``limit`` limited requests per client per
``window_seconds``, and ``Retry-After`` is exactly the time until the
current window rolls over. The known fixed-window trade-off — a
client can burst ``limit`` requests at the end of one window and
``limit`` more at the start of the next — is accepted here: the
window is short (60 s by default), the limiter exists to keep one
runaway or abusive client from monopolising a public demo (and its
upstream Qloo/LLM calls), not to shape traffic precisely.

The limiter is deterministic under an injectable clock (the same
``clock=`` convention as the server's stores), thread-safe, and
bounded: at most ``max_clients`` client windows are tracked, the
least-recently-seen client evicted first — the bounded-FIFO doctrine
the evidence cache and the share/feed stores already follow, so a
long-lived hosted demo cannot grow this state without limit either.
Only the client key (the remote IP the server sees) is stored; no
request content is ever recorded.
"""
from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict

DEFAULT_RATE_LIMIT = 30            # limited POSTs per client per window
DEFAULT_RATE_WINDOW_SECONDS = 60.0  # the window those POSTs are counted in
MAX_RATE_CLIENTS = 512             # client windows tracked at once
RATE_LIMIT_ENV_VAR = "TASTEPILOT_RATE_LIMIT"  # serve()-level override


class RateLimiter:
    """Fixed-window per-client request counter.

    ``check(client)`` records one attempted request and returns
    ``None`` when it is allowed, or the whole-second ``Retry-After``
    delay (>= 1) when the client has exhausted this window's quota.
    A refused attempt does not consume quota and does not extend the
    window, so the delay a client is told is the delay it will
    actually wait. Windows are anchored at each client's first
    request after the previous window expired — there is no global
    wall-clock grid for clients to synchronise against.
    """

    def __init__(self, limit: int = DEFAULT_RATE_LIMIT,
                 window_seconds: float = DEFAULT_RATE_WINDOW_SECONDS,
                 max_clients: int = MAX_RATE_CLIENTS,
                 clock=time.time):
        if limit < 1:
            raise ValueError("rate limit must be >= 1")
        if window_seconds <= 0:
            raise ValueError("rate window must be > 0")
        if max_clients < 1:
            raise ValueError("max_clients must be >= 1")
        self.limit = limit
        self.window_seconds = float(window_seconds)
        self.max_clients = max_clients
        self._clock = clock
        self._lock = threading.Lock()
        # client -> [window_start, count], least-recently-seen first.
        self._windows: OrderedDict[str, list] = OrderedDict()

    def check(self, client: str) -> int | None:
        """Record one request from ``client``; None = allowed, else
        the Retry-After delay in whole seconds (>= 1)."""
        with self._lock:
            now = self._clock()
            entry = self._windows.get(client)
            if entry is not None and now >= entry[0] + self.window_seconds:
                entry = None  # the window rolled over: start a fresh one
            if entry is None:
                self._windows[client] = [now, 1]
                self._windows.move_to_end(client)
                while len(self._windows) > self.max_clients:
                    self._windows.popitem(last=False)
                return None
            self._windows.move_to_end(client)
            if entry[1] < self.limit:
                entry[1] += 1
                return None
            retry = math.ceil(entry[0] + self.window_seconds - now)
            return max(1, int(retry))

    def info(self) -> dict:
        """Live limiter state, in the style of the stores' ``info()``:
        the configured quota plus how many client windows are
        currently tracked (expired windows drop out lazily)."""
        with self._lock:
            now = self._clock()
            expired = [c for c, e in self._windows.items()
                       if now >= e[0] + self.window_seconds]
            for client in expired:
                del self._windows[client]
            return {"enabled": True,
                    "limit": self.limit,
                    "window_seconds": self.window_seconds,
                    "max_clients": self.max_clients,
                    "clients": len(self._windows)}
