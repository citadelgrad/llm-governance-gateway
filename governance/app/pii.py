from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal, cast

from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer, RecognizerResult
from presidio_anonymizer import AnonymizerEngine


@dataclass
class PiiResult:
    findings: list[dict]        # [{type, start, end, score}] — NO matched text
    data_classification: str    # "none" | "pii"
    redacted_text: str

_analyzer: AnalyzerEngine | None = None
_anonymizer: AnonymizerEngine | None = None
_lock: asyncio.Lock | None = None
_executor: ThreadPoolExecutor | None = None
_executor_max_workers: int | None = None
_semaphore: asyncio.Semaphore | None = None

def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock

def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        assert _executor_max_workers is not None, "call initialize() first"
        _semaphore = asyncio.Semaphore(_executor_max_workers)
    return _semaphore


# --- Class-aware concurrency scheduling (ai-gateway-pvo) --------------------
#
# `_get_semaphore()` above gates every call into scan()/redact()/run() with
# ONE shared limit and no notion of caller identity. That means a sustained
# burst of bulk/MCP-tool-response scans (app/main.py's /v1/dlp/pii-scan) can
# occupy every permit and make a concurrent, latency-sensitive interactive
# scan (app/pipeline.py's pii_stage(), part of /v1/inspect) queue behind the
# full depth of that burst. `_get_semaphore()`/`_semaphore` are kept above,
# unused by the logic below, only because existing tests assert their
# init-order behavior directly.
#
# Design: split the executor's total worker count N (`_executor_max_workers`)
# into three pools that always sum to exactly N — the aggregate concurrency
# cap is unchanged, only how access to it is scheduled:
#   - an "interactive" home pool  (reserved minimum for chat/inspect traffic)
#   - a "bulk" home pool          (reserved minimum for MCP tool-response traffic)
#   - a "shared" overflow pool that either class may use
#
# A request first tries its own home pool, then the shared pool, then
# *opportunistically borrows* the other class's home pool — but only when
# that pool is currently idle AND nobody is already waiting on it (see
# `_can_acquire_immediately`). That "no waiters" condition is what keeps the
# reservation meaningful under contention: the instant a request begins
# waiting on a home pool it becomes that pool's FIFO head, and no further
# opportunistic borrow is admitted ahead of it — so a genuinely busy class
# can never be starved down to zero concurrency by the other class (AC2),
# while a single idle class can still opportunistically use the whole of N
# when the other class has no traffic (AC4; a plain fixed 50/50 split, with
# no borrowing, would fail this — a reserved-but-unused half is capacity
# permanently denied to a single busy class). If a request can't get in
# anywhere, it queues (blocks) on its OWN home pool specifically — never on
# the other class's pool, and never behind the other class's queue depth
# (see `_split_pool_sizes` for the degenerate all-shared fallback when a
# class has no home pool at all, e.g. N == 1).
#
# Documented wait bound (AC1): `_MAX_SCAN_SECONDS` below is this module's
# documented upper bound on any single scan/redact call's runtime. Because a
# queued request is always the FIFO head of its own home pool (per the
# waiter-check above), it waits for at most ONE currently-held permit in
# that pool to free up — i.e. at most `_MAX_SCAN_SECONDS` — never for the
# depth of a burst on the other class's pool (which would be unbounded,
# O(burst size)). If a future change makes a single call slower than this,
# update the constant to match.
_MAX_SCAN_SECONDS = 5.0

PiiRequestClass = Literal["interactive", "bulk"]
PII_CLASS_INTERACTIVE: PiiRequestClass = "interactive"
PII_CLASS_BULK: PiiRequestClass = "bulk"

_interactive_semaphore: asyncio.Semaphore | None = None
_bulk_semaphore: asyncio.Semaphore | None = None
_shared_semaphore: asyncio.Semaphore | None = None
_pool_sizes: tuple[int, int, int] | None = None  # (interactive, bulk, shared)


def _split_pool_sizes(total: int) -> tuple[int, int, int]:
    """Split `total` executor slots into (interactive_home, bulk_home, shared).

    Each home pool gets ~25% of `total` (minimum 1 when `total` allows); the
    remainder becomes the shared/overflow pool both classes may draw from.
    `2 * home + shared == total` always, so the aggregate concurrency cap
    matches the original single-semaphore design exactly.

    Concrete sizes: total=1 -> (0, 0, 1) [degenerate: no home pools, both
    classes fall back to the shared pool — see `_acquire_scan_slot`];
    total=2 -> (1, 1, 0); total=3 -> (1, 1, 1); total=8 -> (2, 2, 4);
    total=32 -> (8, 8, 16).
    """
    if total <= 1:
        return (0, 0, total)
    home = max(1, int(total * 0.25))
    while home > 0 and 2 * home > total:
        home -= 1
    return (home, home, total - 2 * home)


def _get_pools() -> tuple[asyncio.Semaphore, asyncio.Semaphore, asyncio.Semaphore]:
    global _interactive_semaphore, _bulk_semaphore, _shared_semaphore, _pool_sizes
    if _shared_semaphore is None:
        assert _executor_max_workers is not None, "call initialize() first"
        _pool_sizes = _split_pool_sizes(_executor_max_workers)
        interactive_size, bulk_size, shared_size = _pool_sizes
        _interactive_semaphore = asyncio.Semaphore(interactive_size)
        _bulk_semaphore = asyncio.Semaphore(bulk_size)
        _shared_semaphore = asyncio.Semaphore(shared_size)
    assert _interactive_semaphore is not None
    assert _bulk_semaphore is not None
    assert _shared_semaphore is not None
    return _interactive_semaphore, _bulk_semaphore, _shared_semaphore


def _can_acquire_immediately(sem: asyncio.Semaphore) -> bool:
    """True if `sem.acquire()` is guaranteed to return without suspending.

    Requires a free permit (`_value > 0`) AND an empty waiter queue. The
    waiter check matters even when `_value > 0`: without it, an
    opportunistic borrow could same-tick "jump the FIFO line" ahead of a
    task that is already legitimately queued on this semaphore, undermining
    the AC1/AC2 bound below.
    """
    return sem._value > 0 and not sem._waiters  # noqa: SLF001 - see docstring


@asynccontextmanager
async def _acquire_scan_slot(request_class: PiiRequestClass = PII_CLASS_INTERACTIVE):
    """Admit one scan/redact call under the class-aware pools above.

    Admission order: (1) the caller's own home pool, (2) the shared pool,
    (3) an opportunistic, non-blocking borrow of the OTHER class's home pool
    (only while it is idle with no waiters), (4) otherwise block on the
    caller's own home pool — or on the shared pool in the degenerate case
    where that class has no home pool at all (see `_split_pool_sizes`).
    Step 4 is what bounds AC1's wait and enforces AC2's minimum share; step
    3 is what preserves AC4's full-N throughput for an uncontended class.
    """
    interactive, bulk, shared = _get_pools()
    assert _pool_sizes is not None
    interactive_size, bulk_size, _shared_size = _pool_sizes
    if request_class == PII_CLASS_BULK:
        home, other, home_size = bulk, interactive, bulk_size
    else:
        home, other, home_size = interactive, bulk, interactive_size
    blocking_target = home if home_size > 0 else shared

    if _can_acquire_immediately(home):
        await home.acquire()
        held = home
    elif _can_acquire_immediately(shared):
        await shared.acquire()
        held = shared
    elif _can_acquire_immediately(other):
        await other.acquire()
        held = other
    else:
        await blocking_target.acquire()
        held = blocking_target

    try:
        yield
    finally:
        held.release()


async def initialize(spacy_model: str = "en_core_web_lg") -> None:
    global _analyzer, _anonymizer, _executor, _executor_max_workers
    async with _get_lock():
        if _analyzer is not None:
            return  # already initialized (idempotent)
        if _executor is None:
            _executor_max_workers = min(32, (os.cpu_count() or 1) + 4)
            _executor = ThreadPoolExecutor(max_workers=_executor_max_workers)
            asyncio.get_running_loop().set_default_executor(_executor)
        from presidio_analyzer.nlp_engine import NlpEngineProvider
        provider = NlpEngineProvider(nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "en", "model_name": spacy_model}],
        })
        nlp_engine = await asyncio.to_thread(provider.create_engine)
        _analyzer = await asyncio.to_thread(AnalyzerEngine, nlp_engine=nlp_engine)
        _register_high_recall_ssn_recognizer(_analyzer)
        _anonymizer = await asyncio.to_thread(AnonymizerEngine)


def _register_high_recall_ssn_recognizer(analyzer: AnalyzerEngine) -> None:
    """Presidio's built-in US_SSN recognizer rejects common test/dummy values.

    The gateway should redact SSN-shaped secrets even when they are non-issued
    examples such as 123-45-6789. This local recognizer intentionally favors
    recall for the canonical dashed SSN shape; audit records store only spans
    and entity type, not the matched text.
    """
    analyzer.registry.add_recognizer(
        PatternRecognizer(
            name="GatewayHighRecallUsSsnRecognizer",
            supported_entity="US_SSN",
            patterns=[
                Pattern(
                    name="dashed_ssn_shape",
                    regex=r"\b\d{3}-\d{2}-\d{4}\b",
                    score=0.85,
                )
            ],
            context=["ssn", "social", "security", "taxpayer", "tin"],
        )
    )

async def scan(
    text: str, request_class: PiiRequestClass = PII_CLASS_INTERACTIVE
) -> list[RecognizerResult]:
    assert _analyzer is not None, "call initialize() first"
    async with _acquire_scan_slot(request_class):
        return await asyncio.to_thread(_analyzer.analyze, text=text, language="en")

async def redact(
    text: str,
    results: list[RecognizerResult],
    request_class: PiiRequestClass = PII_CLASS_INTERACTIVE,
) -> str:
    assert _anonymizer is not None, "call initialize() first"
    async with _acquire_scan_slot(request_class):
        anonymized = await asyncio.to_thread(
            _anonymizer.anonymize,
            text=text,
            analyzer_results=cast(Any, results),
        )
    return anonymized.text

async def run(
    text: str, request_class: PiiRequestClass = PII_CLASS_INTERACTIVE
) -> PiiResult:
    results = await scan(text, request_class)
    redacted = text
    if results:
        redacted = await redact(text, results, request_class)
    findings = [
        {"type": r.entity_type, "start": r.start, "end": r.end, "score": r.score}
        for r in results
    ]
    return PiiResult(
        findings=findings,
        data_classification="pii" if results else "none",
        redacted_text=redacted,
    )
