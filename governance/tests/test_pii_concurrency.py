import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import ServiceUnavailable

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
    # Reset the class-aware pools too (ai-gateway-pvo) so each test builds a
    # fresh set sized off TEST_SEMAPHORE_SIZE instead of reusing whatever an
    # earlier test's _get_pools() call constructed.
    monkeypatch.setattr(pii, "_interactive_semaphore", None)
    monkeypatch.setattr(pii, "_bulk_semaphore", None)
    monkeypatch.setattr(pii, "_shared_semaphore", None)
    monkeypatch.setattr(pii, "_pool_sizes", None)
    return analyzer


class _ClassConcurrencyTracker:
    """Wraps pii.scan() to record, per request_class, how many calls of that
    class are executing at once — independent of the shared
    _ConcurrencyTrackingAnalyzer's global counter, so tests can tell classes
    apart (AC2/AC5) instead of only observing the aggregate."""

    def __init__(self):
        self._lock = threading.Lock()
        self.current = {"interactive": 0, "bulk": 0}
        self.max_concurrent = {"interactive": 0, "bulk": 0}

    async def scan(self, text: str, request_class):
        with self._lock:
            self.current[request_class] += 1
            self.max_concurrent[request_class] = max(
                self.max_concurrent[request_class], self.current[request_class]
            )
        try:
            return await pii.scan(text, request_class)
        finally:
            with self._lock:
                self.current[request_class] -= 1


def test_pools_sized_to_executor_worker_count(monkeypatch):
    monkeypatch.setattr(pii, "_executor_max_workers", 7)
    monkeypatch.setattr(pii, "_interactive_semaphore", None)
    monkeypatch.setattr(pii, "_bulk_semaphore", None)
    monkeypatch.setattr(pii, "_shared_semaphore", None)
    monkeypatch.setattr(pii, "_pool_sizes", None)

    interactive, bulk, shared = pii._get_pools()

    assert pii._pool_sizes == pii._split_pool_sizes(7)
    assert interactive._value + bulk._value + shared._value == 7


def test_get_pools_returns_the_same_instances(monkeypatch):
    monkeypatch.setattr(pii, "_executor_max_workers", TEST_SEMAPHORE_SIZE)
    monkeypatch.setattr(pii, "_interactive_semaphore", None)
    monkeypatch.setattr(pii, "_bulk_semaphore", None)
    monkeypatch.setattr(pii, "_shared_semaphore", None)
    monkeypatch.setattr(pii, "_pool_sizes", None)

    first_interactive, first_bulk, first_shared = pii._get_pools()
    second_interactive, second_bulk, second_shared = pii._get_pools()

    assert first_interactive is second_interactive
    assert first_bulk is second_bulk
    assert first_shared is second_shared


# ai-gateway-98o: `_can_acquire_immediately` used to read the private
# `_value`/`_waiters` asyncio.Semaphore fields directly. It now goes through
# the public `Semaphore.locked()` API instead; this test exercises the
# boolean it reports across a pool with 0, 1, and N-1 of N slots free.
@pytest.mark.asyncio
async def test_can_acquire_immediately_reflects_free_slot_count():
    n = 4
    sem = asyncio.Semaphore(n)

    for _ in range(n):
        await sem.acquire()
    assert pii._can_acquire_immediately(sem) is False  # 0 of N free

    sem.release()
    assert pii._can_acquire_immediately(sem) is True  # 1 of N free

    for _ in range(n - 2):
        sem.release()
    assert pii._can_acquire_immediately(sem) is True  # N-1 of N free


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
async def test_single_call_completes_unaffected_by_semaphore(fake_presidio):
    result = await pii.run("some text with pii")

    assert result.data_classification == "pii"
    assert result.findings
    assert result.redacted_text == "[REDACTED]some text with pii"


# --- ai-gateway-pvo: class-aware concurrency scheduling ---------------------
#
# Replaces the old, weak `test_ingress_scan_completes_after_mcp_burst_saturates_semaphore`
# (it only proved eventual completion, never a bounded wait or a minimum
# share) with tests that directly exercise AC1-AC5 from the task.

@pytest.mark.asyncio
async def test_interactive_scan_bounded_wait_during_sustained_bulk_burst(fake_presidio):
    """AC1: a sustained bulk burst that fully occupies the shared pools must
    not make a concurrently-arriving interactive scan queue behind the
    burst's full depth. Per `_acquire_scan_slot`'s docstring, a queued
    request is always the FIFO head of its OWN home pool, so its wait is
    bounded by `_max_scan_seconds` (one in-flight call draining) - never by
    O(burst size * call delay), which is what the old single shared
    semaphore produced.
    """
    delay = fake_presidio._delay
    burst_size = TEST_SEMAPHORE_SIZE * 4  # far deeper than any single pool

    bulk_calls = [
        asyncio.create_task(pii.scan("mcp tool response", pii.PII_CLASS_BULK))
        for _ in range(burst_size)
    ]
    await asyncio.sleep(0.01)  # let the burst claim every pool and start queuing

    start = time.monotonic()
    interactive_call = asyncio.create_task(
        pii.scan("ingress inspect call", pii.PII_CLASS_INTERACTIVE)
    )
    interactive_result = await asyncio.wait_for(interactive_call, timeout=pii._max_scan_seconds)
    interactive_elapsed = time.monotonic() - start

    assert interactive_result  # admitted and completed, not dropped or errored
    # The old single-semaphore design would make this wait roughly
    # (burst_size / TEST_SEMAPHORE_SIZE) * delay before even starting its own
    # call. Assert we are nowhere near that - bounded by ~one call, not the
    # burst depth.
    old_design_wait_estimate = (burst_size / TEST_SEMAPHORE_SIZE) * delay
    assert interactive_elapsed < old_design_wait_estimate
    assert interactive_elapsed < 3 * delay

    bulk_results = await asyncio.wait_for(asyncio.gather(*bulk_calls), timeout=10)
    assert all(bulk_results)
    assert fake_presidio.max_concurrent == TEST_SEMAPHORE_SIZE  # never exceeded (AC3)


@pytest.mark.asyncio
async def test_neither_class_can_starve_the_other_to_zero_slots(monkeypatch):
    """AC2: each class has an enforced minimum share of concurrency slots.
    Load-test both classes simultaneously with far more traffic than either
    reserved pool can hold, and confirm each class still reaches genuine
    *simultaneous* concurrency at least equal to its reserved home-pool
    size - i.e. neither class's burst reduces the other's slot availability
    to zero, even under sustained dual contention. Uses a larger pool
    (total=8 -> home pools of 2 each; see `_split_pool_sizes`) so the
    reserved minimum being exercised is non-trivial.
    """
    total = 8
    analyzer = _ConcurrencyTrackingAnalyzer(delay=0.08)
    monkeypatch.setattr(pii, "_analyzer", analyzer)
    monkeypatch.setattr(pii, "_anonymizer", _PassthroughAnonymizer())
    monkeypatch.setattr(pii, "_executor_max_workers", total)
    monkeypatch.setattr(pii, "_interactive_semaphore", None)
    monkeypatch.setattr(pii, "_bulk_semaphore", None)
    monkeypatch.setattr(pii, "_shared_semaphore", None)
    monkeypatch.setattr(pii, "_pool_sizes", None)

    interactive_size, bulk_size, _shared = pii._split_pool_sizes(total)
    tracker = _ClassConcurrencyTracker()

    interactive_calls = (
        tracker.scan("interactive burst", pii.PII_CLASS_INTERACTIVE)
        for _ in range(total * 3)
    )
    bulk_calls = (
        tracker.scan("bulk burst", pii.PII_CLASS_BULK) for _ in range(total * 3)
    )

    await asyncio.wait_for(
        asyncio.gather(*interactive_calls, *bulk_calls), timeout=10
    )

    assert tracker.max_concurrent["interactive"] >= interactive_size
    assert tracker.max_concurrent["bulk"] >= bulk_size


@pytest.mark.asyncio
async def test_low_load_latency_matches_pre_pvo_behavior(fake_presidio):
    """AC3 regression guard: with no contention, a single scan of either
    class should take about as long as the underlying analyze() call
    itself - the class-aware pools must not add queueing overhead when
    nothing is contending for slots.
    """
    delay = fake_presidio._delay

    start = time.monotonic()
    await pii.scan("solo interactive scan", pii.PII_CLASS_INTERACTIVE)
    interactive_elapsed = time.monotonic() - start

    start = time.monotonic()
    await pii.scan("solo bulk scan", pii.PII_CLASS_BULK)
    bulk_elapsed = time.monotonic() - start

    # Generous margin over the fake analyzer's own delay: this is checking
    # for *added* queueing/scheduling overhead, not asserting a tight bound.
    assert interactive_elapsed < delay * 4
    assert bulk_elapsed < delay * 4


@pytest.mark.asyncio
async def test_throughput_not_reduced_at_or_below_previous_limit(fake_presidio):
    """AC4 regression guard: with total concurrent requests at the previous
    single semaphore's limit (TEST_SEMAPHORE_SIZE), concurrency must still
    reach that full limit - including when ALL of that load is a single
    class. A naive fixed reserved-pool split with no borrowing would fail
    this: a single busy class would be capped below the old limit by
    however much capacity sits reserved (and idle) for the other class.
    """
    all_bulk_results = await asyncio.wait_for(
        asyncio.gather(
            *(pii.scan("bulk", pii.PII_CLASS_BULK) for _ in range(TEST_SEMAPHORE_SIZE))
        ),
        timeout=5,
    )
    assert all(all_bulk_results)
    assert fake_presidio.max_concurrent == TEST_SEMAPHORE_SIZE

    fake_presidio.max_concurrent = 0  # reset before the second sub-case

    all_interactive_results = await asyncio.wait_for(
        asyncio.gather(
            *(
                pii.scan("chat", pii.PII_CLASS_INTERACTIVE)
                for _ in range(TEST_SEMAPHORE_SIZE)
            )
        ),
        timeout=5,
    )
    assert all(all_interactive_results)
    assert fake_presidio.max_concurrent == TEST_SEMAPHORE_SIZE


@pytest.mark.asyncio
async def test_interactive_only_saturation_still_queues_new_requests(fake_presidio):
    """AC5 boundary: a single class can opportunistically use the whole
    pool (AC4), but that pool is still bounded at the original total - a
    class saturating it alone must still make a NEW same-class request
    queue behind the existing calls, rather than bypassing backpressure via
    some separate, effectively-unbounded per-class allocation. Confirms
    isolation was added on top of the shared cap, not just a bigger pool.
    """
    saturating = [
        asyncio.create_task(pii.scan("chat", pii.PII_CLASS_INTERACTIVE))
        for _ in range(TEST_SEMAPHORE_SIZE)
    ]
    await asyncio.sleep(0.01)  # let the saturating calls claim every pool

    new_request = asyncio.create_task(pii.scan("chat", pii.PII_CLASS_INTERACTIVE))
    await asyncio.sleep(0.01)  # give it a chance to run if it were (wrongly) unbounded

    assert not new_request.done()  # queued, not bypassed

    results = await asyncio.wait_for(
        asyncio.gather(*saturating, new_request), timeout=5
    )
    assert all(results)
    assert fake_presidio.max_concurrent == TEST_SEMAPHORE_SIZE


# --- ai-gateway-ypb: run_google retry backoff must not hold a pool slot ----


class _FlakyForTextDlpClient:
    """Fake Google DLP client that fails a fixed number of times with a
    retryable transient error, but only for ONE specific triggering request
    text - any other text always succeeds immediately. This lets a test run
    a genuinely retrying call and an unrelated concurrent call through the
    SAME `pii._google_client` without the two calls' scripted behavior
    interfering with each other.
    """

    def __init__(self, trigger_text: str, fail_times: int):
        self._trigger_text = trigger_text
        self._remaining_failures = fail_times
        self.trigger_calls = 0

    def inspect_content(self, request, retry, timeout):
        if request["item"]["value"] == self._trigger_text:
            self.trigger_calls += 1
            if self._remaining_failures > 0:
                self._remaining_failures -= 1
                raise ServiceUnavailable("transient upstream error")
        return SimpleNamespace(
            result=SimpleNamespace(findings=[], findings_truncated=False)
        )


@pytest.mark.asyncio
async def test_run_google_retry_backoff_releases_slot_for_other_class(monkeypatch):
    """ai-gateway-ypb: a retrying `run_google` call must release its pool
    slot during backoff sleeps instead of holding it for the whole
    retry/backoff window - otherwise a concurrent request of the other
    class can be starved of its guaranteed home-pool slot on a small pool.

    Reproduces the ticket's scenario on a 2-slot pool (`_split_pool_sizes(2)
    == (interactive=1, bulk=1, shared=0)`): the bulk-home slot is held by
    other in-flight bulk traffic (simulated by manually acquiring it), which
    forces a retrying bulk call to opportunistically borrow the (otherwise
    idle) INTERACTIVE home slot - exactly what a second bulk request would
    do under sustained bulk load (see `_acquire_scan_slot`). A genuine
    interactive request that arrives while that borrower is mid-backoff must
    still get its home slot promptly, not wait for the borrower's entire
    retry window.

    ai-gateway-4hb: synchronization is deterministic, not wall-clock-based.
    `asyncio.sleep` (the only sleep `_inspect_content_with_backoff` reaches
    for) is monkeypatched so entering a backoff sleep signals
    `backoff_entered` and then parks on `release_backoff` until the test
    lets it continue - no real time ever elapses. Because
    `_acquire_scan_slot`'s `finally: held.release()` runs synchronously,
    with no `await` in between, before `_inspect_content_with_backoff`
    reaches its `await asyncio.sleep(backoff)` call, observing
    `backoff_entered` is proof the slot was already released - regardless
    of how long that actually took on the clock. And because
    `release_backoff` is deliberately left unset while we assert the
    interactive call's outcome, a regression that holds the slot across the
    sleep would deadlock the interactive call right here (caught by the
    `wait_for` hang-guard below) instead of racing it against a tuned
    timeout.
    """
    total = 2
    monkeypatch.setattr(pii, "_executor_max_workers", total)
    monkeypatch.setattr(pii, "_interactive_semaphore", None)
    monkeypatch.setattr(pii, "_bulk_semaphore", None)
    monkeypatch.setattr(pii, "_shared_semaphore", None)
    monkeypatch.setattr(pii, "_pool_sizes", None)

    trigger_text = "bulk retry probe"
    client = _FlakyForTextDlpClient(trigger_text, fail_times=2)
    monkeypatch.setattr(pii, "_google_client", client)

    interactive_sem, bulk_sem, _shared_sem = pii._get_pools()
    assert (interactive_sem._value, bulk_sem._value) == (1, 1)

    # Simulate other in-flight bulk traffic already holding the bulk home
    # slot, so the retrying bulk call below must borrow the interactive
    # home slot instead of using its own (busy) home pool.
    await bulk_sem.acquire()

    backoff_entered = asyncio.Event()
    release_backoff = asyncio.Event()

    async def fake_backoff_sleep(delay, result=None):
        # Replaces the real backoff wait with a controllable signal instead
        # of consuming real wall-clock time. See the test docstring for why
        # this call happening at all proves the slot was already released.
        backoff_entered.set()
        await release_backoff.wait()
        return result

    monkeypatch.setattr(asyncio, "sleep", fake_backoff_sleep)

    async def run_google_call(text: str, request_class: pii.PiiRequestClass):
        return await pii.run_google(
            text,
            project="privacy-project",
            location="global",
            min_likelihood="POSSIBLE",
            info_types=("EMAIL_ADDRESS",),
            timeout_seconds=5.0,
            request_class=request_class,
        )

    retrier = asyncio.create_task(
        run_google_call(trigger_text, pii.PII_CLASS_BULK)
    )
    # Deterministically wait for the retrier to make its first (failing)
    # attempt, borrow-and-release the interactive slot, and enter its first
    # backoff sleep - no fixed-duration guess at how long that takes.
    await asyncio.wait_for(backoff_entered.wait(), timeout=5)

    real_interactive = asyncio.create_task(
        run_google_call("real interactive request", pii.PII_CLASS_INTERACTIVE)
    )
    # `release_backoff` is still unset here, so the retrier cannot progress
    # past its backoff sleep. This `wait_for` timeout is a hang guard for a
    # regressed/deadlocked test run, not a race margin: it is far larger
    # than anything this call should ever need, and the test's actual proof
    # is the ordering assertion below, not how fast this returns.
    await asyncio.wait_for(real_interactive, timeout=5)

    # The retrier has at least one more scripted failure + backoff sleep
    # ahead of it (fail_times=2, only its first attempt has happened so
    # far). Asserting this *before* releasing the backoff is the proof the
    # interactive request completed independently of - and without waiting
    # for - the retrier's full retry window: if the slot-release-before-
    # sleep behavior regressed, `real_interactive` above would have
    # deadlocked instead of reaching this line at all.
    assert not retrier.done()

    release_backoff.set()  # let the retrier's remaining scripted work run
    bulk_sem.release()  # release the manually-held slot from this test
    retrier_result = await asyncio.wait_for(retrier, timeout=5)

    assert retrier_result.data_classification == "none"
    assert client.trigger_calls == 3  # 2 failures + 1 success - unchanged retry count
