from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from threading import RLock
from typing import Any, Callable, TypeVar

T = TypeVar("T")


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """Raised when a circuit is open and calls should fail fast."""


@dataclass(slots=True)
class CircuitBreaker:
    """Three-state circuit breaker for one provider.

    CLOSED: calls pass through and consecutive failures are counted.
    OPEN: calls fail fast until the reset timeout elapses.
    HALF_OPEN: one probe is allowed; success closes, failure re-opens.
    """

    name: str
    failure_threshold: int
    reset_timeout_seconds: float
    success_threshold: int = 1
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    success_count: int = 0
    opened_at: float | None = None
    transition_log: list[dict[str, str | float]] = field(default_factory=list)
    _probe_in_flight: bool = False
    _lock: Any = field(default_factory=RLock, repr=False, compare=False)

    def allow_request(self) -> bool:
        """Return whether a request should be attempted right now."""
        with self._lock:
            if self.state == CircuitState.OPEN:
                timeout_elapsed = (
                    self.opened_at is not None
                    and time.monotonic() - self.opened_at >= self.reset_timeout_seconds
                )
                if not timeout_elapsed:
                    return False
                self._transition(CircuitState.HALF_OPEN, "reset_timeout_elapsed")
                self.success_count = 0
                self._probe_in_flight = True
                return True

            if self.state == CircuitState.HALF_OPEN:
                if self._probe_in_flight:
                    return False
                self._probe_in_flight = True
                return True

            return True

    def call(self, fn: Callable[..., T], *args: object, **kwargs: object) -> T:
        """Call a function through the circuit breaker."""
        if not self.allow_request():
            raise CircuitOpenError(f"circuit {self.name} is open")
        try:
            result = fn(*args, **kwargs)
        except Exception:
            self.record_failure()
            raise
        self.record_success()
        return result

    def record_success(self) -> None:
        """Record success and close from HALF_OPEN if enough probes pass."""
        with self._lock:
            self.failure_count = 0
            self._probe_in_flight = False

            if self.state == CircuitState.HALF_OPEN:
                self.success_count += 1
                if self.success_count >= self.success_threshold:
                    self._transition(CircuitState.CLOSED, "probe_success")
                    self.success_count = 0
                    self.opened_at = None
            else:
                self.success_count = 0

    def record_failure(self) -> None:
        """Record failure and open when the threshold is reached."""
        with self._lock:
            self.failure_count += 1
            self.success_count = 0
            self._probe_in_flight = False

            if self.state == CircuitState.HALF_OPEN:
                self._open("half_open_probe_failed")
                return

            if self.state == CircuitState.CLOSED and self.failure_count >= self.failure_threshold:
                self._open("failure_threshold")

    def _open(self, reason: str) -> None:
        self.opened_at = time.monotonic()
        self._transition(CircuitState.OPEN, reason)

    def _transition(self, new_state: CircuitState, reason: str) -> None:
        if self.state == new_state:
            return
        self.transition_log.append(
            {"from": self.state.value, "to": new_state.value, "reason": reason, "ts": time.time()}
        )
        self.state = new_state
