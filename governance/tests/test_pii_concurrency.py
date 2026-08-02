import asyncio
import threading
import time

import pytest

from app import pii

TEST_SEMAPHORE_SIZE = 3


class _ConcurrencyTrackingAnalyzer:
    """Fake analyzer whose synchronous .analyze() records how many calls are
    executing at once, so tests can assert the semaphore actually bounds
    concurrency rather than trusting its configured size."""

    def __init__(self, delay: float = 0.05):
        self._delay = delay
        self._call_lock = threading.Lock()
        self._current = 0
        self.max_concurrent = 0

    def analyze(self, text: str, language: str) -> list:
        with self._call_lock:
            self._current += 1
            self.max_concurrent = max(self.max_concurrent, self._current)
        try:
            time.sleep(self._delay)
            return [_FakeResult()] if text else []
        finally:
            with self._call_lock:
                self._current -= 1


class _FakeResult:
    def __init__(self, entity_type="FAKE", start=0, end=1, score=0.9):
        self.entity_type = entity_type
        self.start = start
        self.end = end
        self.score = score


class _PassthroughAnonymizer:
    def anonymize(self, text, analyzer_results, operators=None):
        return _FakeAnonymizedResult(f"[REDACTED]{text}")


class _FakeAnonymizedResult:
    def __init__(self, text):
        self.text = text


@pytest.fixture
def fake_presidio(monkeypatch):
    analyzer = _ConcurrencyTrackingAnalyzer()
    monkeypatch.setattr(pii, "_analyzer", analyzer)
    monkeypatch.setattr(pii, "_anonymizer", _PassthroughAnonymizer())
    monkeypatch.setattr(pii, "_executor_max_workers", TEST_SEMAPHORE_SIZE)
    monkeypatch.setattr(pii, "_semaphore", None)
    return analyzer


def test_semaphore_sized_to_executor_worker_count(monkeypatch):
    monkeypatch.setattr(pii, "_executor_max_workers", 7)
    monkeypatch.setattr(pii, "_semaphore", None)

    semaphore = pii._get_semaphore()

    assert semaphore._value == 7


def test_get_semaphore_returns_the_same_instance(monkeypatch):
    monkeypatch.setattr(pii, "_executor_max_workers", TEST_SEMAPHORE_SIZE)
    monkeypatch.setattr(pii, "_semaphore", None)

    first = pii._get_semaphore()
    second = pii._get_semaphore()

    assert first is second


@pytest.mark.asyncio
async def test_burst_of_scans_queues_beyond_semaphore_size(fake_presidio):
    burst = TEST_SEMAPHORE_SIZE * 3

    results = await asyncio.wait_for(
        asyncio.gather(*(pii.scan("mcp tool response text") for _ in range(burst))),
        timeout=5,
    )

    assert len(results) == burst
    assert all(results)  # every queued call still produced a real result (AC4)
    assert fake_presidio.max_concurrent == TEST_SEMAPHORE_SIZE  # never exceeded (AC3)


@pytest.mark.asyncio
async def test_ingress_scan_completes_after_mcp_burst_saturates_semaphore(fake_presidio):
    mcp_calls = [
        asyncio.create_task(pii.scan("mcp tool response"))
        for _ in range(TEST_SEMAPHORE_SIZE)
    ]
    await asyncio.sleep(0.01)  # let the burst acquire every semaphore slot first

    ingress_call = asyncio.create_task(pii.scan("ingress inspect call"))

    mcp_results, ingress_result = await asyncio.wait_for(
        asyncio.gather(asyncio.gather(*mcp_calls), ingress_call),
        timeout=5,
    )

    assert all(mcp_results)
    assert ingress_result  # completed once a slot freed, not dropped or errored


@pytest.mark.asyncio
async def test_single_call_completes_unaffected_by_semaphore(fake_presidio):
    result = await pii.run("some text with pii")

    assert result.data_classification == "pii"
    assert result.findings
    assert result.redacted_text == "[REDACTED]some text with pii"
