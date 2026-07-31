from __future__ import annotations

import time
from enum import Enum

# Per docs/auth-architecture.md "Break-glass path": 5 consecutive sidecar
# failures trip the breaker; it stays open for 30s before a single half-open
# probe is allowed.
FAILURE_THRESHOLD = 5
COOLDOWN_SECONDS = 30.0


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Per-replica, in-process breaker around the colocated OPA Sidecar.

    Callers, not this class, decide what counts as a failure: an explicit
    OPA allow/deny is always a successful round trip (`record_success`),
    even when the decision itself is a deny - only a transport error,
    timeout, or 5xx (`OpaCheckError`) is a failure (`record_failure`).
    """

    def __init__(self) -> None:
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> CircuitState:
        return self._state

    def is_closed(self) -> bool:
        return self._state is CircuitState.CLOSED

    def record_success(self) -> None:
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failure_count += 1
        if self._failure_count >= FAILURE_THRESHOLD:
            self._trip()

    def try_start_probe(self) -> bool:
        """Claims the single half-open probe slot for this call, if due.

        Must stay fully synchronous (no `await` between the state read and
        the write) - that's what makes "exactly one probe per cooldown"
        hold under concurrent requests on a single asyncio event loop,
        without needing a lock.
        """
        if self._state is not CircuitState.OPEN:
            return False
        assert self._opened_at is not None
        if time.monotonic() - self._opened_at < COOLDOWN_SECONDS:
            return False
        self._state = CircuitState.HALF_OPEN
        return True

    def record_probe_failure(self) -> None:
        self._trip()

    def _trip(self) -> None:
        self._state = CircuitState.OPEN
        self._opened_at = time.monotonic()
