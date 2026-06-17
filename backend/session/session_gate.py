"""Single-pipeline admission gate.

The demo runs ONE SyncTalk GPU pipeline. If several browsers connect at once they
all drive turns through that single pipeline and contend for it — which brings back
the exact frame stutter we fixed at the transport layer. This gate caps the number
of concurrent live sessions (default 1). Extra connections are rejected with a
``busy`` message and closed; the frontend shows a "please wait" overlay and its
normal reconnect loop retries until a slot frees.

An admitted session that goes idle (abandoned tab) is evicted after
``SESSION_IDLE_EVICT_S`` of no real activity, so the slot can never lock forever.
Heartbeat pings do NOT count as activity (see ``touch`` callers); only real client
messages do.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from ..logging_config import log_event
from ..settings import MAX_CONCURRENT_SESSIONS, SESSION_IDLE_EVICT_S

log = logging.getLogger("backend.session_gate")


class _Entry:
    __slots__ = ("session", "last_activity")

    def __init__(self, session) -> None:
        self.session = session
        self.last_activity = time.monotonic()


class SessionGate:
    def __init__(self, max_active: int, idle_evict_s: float) -> None:
        self._max = max(1, int(max_active))
        self._idle = float(idle_evict_s)
        self._active: dict[str, _Entry] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, session) -> bool:
        """Try to claim a slot for ``session``. Returns True if admitted."""
        async with self._lock:
            now = time.monotonic()
            # Evict idle/abandoned holders so they don't hold the slot forever.
            stale = [
                e for sid, e in self._active.items()
                if sid != session.session_id and now - e.last_activity > self._idle
            ]
            for e in stale:
                self._active.pop(e.session.session_id, None)
                log_event(log, "session_evict_idle", session_id=e.session.session_id)
                with contextlib.suppress(Exception):
                    await e.session.force_disconnect("idle")

            existing = self._active.get(session.session_id)
            if existing is not None:
                existing.last_activity = now
                return True
            if len(self._active) >= self._max:
                log_event(log, "session_rejected_busy", session_id=session.session_id, active=len(self._active))
                return False
            self._active[session.session_id] = _Entry(session)
            log_event(log, "session_acquired", session_id=session.session_id, active=len(self._active))
            return True

    def touch(self, session_id: str) -> None:
        """Mark real activity on a session (keeps it from being evicted)."""
        entry = self._active.get(session_id)
        if entry is not None:
            entry.last_activity = time.monotonic()

    async def release(self, session_id: str) -> None:
        async with self._lock:
            if self._active.pop(session_id, None) is not None:
                log_event(log, "session_released", session_id=session_id, active=len(self._active))


GATE = SessionGate(MAX_CONCURRENT_SESSIONS, SESSION_IDLE_EVICT_S)
