from __future__ import annotations

import mcpproxy.app.circuit_breaker as circuit_breaker_module
from mcpproxy.app.circuit_breaker import CircuitBreaker, CircuitState


class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def test_new_breaker_starts_closed():
    breaker = CircuitBreaker()

    assert breaker.is_closed()
    assert breaker.state is CircuitState.CLOSED


def test_failures_below_threshold_do_not_trip():
    breaker = CircuitBreaker()

    for _ in range(4):
        breaker.record_failure()

    assert breaker.is_closed()


def test_fifth_consecutive_failure_trips_the_breaker():
    breaker = CircuitBreaker()

    for _ in range(5):
        breaker.record_failure()

    assert breaker.state is CircuitState.OPEN


def test_record_success_resets_the_failure_count():
    breaker = CircuitBreaker()
    for _ in range(4):
        breaker.record_failure()

    breaker.record_success()
    for _ in range(4):
        breaker.record_failure()

    assert breaker.is_closed()


def test_try_start_probe_returns_false_while_closed():
    breaker = CircuitBreaker()

    assert breaker.try_start_probe() is False


def test_try_start_probe_returns_false_before_cooldown_elapses(monkeypatch):
    breaker = CircuitBreaker()
    clock = _FakeClock(start=100.0)
    monkeypatch.setattr(circuit_breaker_module.time, "monotonic", clock)
    for _ in range(5):
        breaker.record_failure()

    clock.now = 129.0  # 29s later - cooldown is 30s

    assert breaker.try_start_probe() is False
    assert breaker.state is CircuitState.OPEN


def test_try_start_probe_returns_true_after_cooldown_elapses(monkeypatch):
    breaker = CircuitBreaker()
    clock = _FakeClock(start=100.0)
    monkeypatch.setattr(circuit_breaker_module.time, "monotonic", clock)
    for _ in range(5):
        breaker.record_failure()

    clock.now = 130.0  # exactly 30s later

    assert breaker.try_start_probe() is True
    assert breaker.state is CircuitState.HALF_OPEN


def test_only_the_first_try_start_probe_call_succeeds(monkeypatch):
    """AC6: try_start_probe is synchronous - the first caller past cooldown
    claims the probe slot, every subsequent caller gets False until the
    probe resolves. This is what guarantees exactly one probe per cooldown."""
    breaker = CircuitBreaker()
    clock = _FakeClock(start=100.0)
    monkeypatch.setattr(circuit_breaker_module.time, "monotonic", clock)
    for _ in range(5):
        breaker.record_failure()
    clock.now = 200.0

    assert breaker.try_start_probe() is True
    assert breaker.try_start_probe() is False
    assert breaker.try_start_probe() is False


def test_successful_probe_closes_the_breaker():
    breaker = CircuitBreaker()
    for _ in range(5):
        breaker.record_failure()
    breaker.try_start_probe()

    breaker.record_success()

    assert breaker.is_closed()


def test_failed_probe_reopens_and_restarts_the_cooldown(monkeypatch):
    breaker = CircuitBreaker()
    clock = _FakeClock(start=100.0)
    monkeypatch.setattr(circuit_breaker_module.time, "monotonic", clock)
    for _ in range(5):
        breaker.record_failure()
    clock.now = 130.0
    breaker.try_start_probe()

    breaker.record_probe_failure()

    assert breaker.state is CircuitState.OPEN
    # Cooldown restarted at clock.now == 130.0, not the original 100.0 -
    # only 29s have passed since the restart, so no new probe is due yet.
    clock.now = 159.0
    assert breaker.try_start_probe() is False
    clock.now = 160.0
    assert breaker.try_start_probe() is True


def test_two_breaker_instances_are_independent():
    """AC9: one replica's breaker state has no effect on another's."""
    breaker_a = CircuitBreaker()
    breaker_b = CircuitBreaker()

    for _ in range(5):
        breaker_a.record_failure()

    assert breaker_a.state is CircuitState.OPEN
    assert breaker_b.is_closed()
