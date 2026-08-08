from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from typing import Any, Literal, cast

from google.api_core import exceptions as google_exceptions
from google.api_core.client_options import ClientOptions
from google.cloud import dlp_v2
from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer, RecognizerResult
from presidio_anonymizer import AnonymizerEngine
from presidio_anonymizer.entities import OperatorConfig

from .google_credentials import GoogleCredentialPreflightError, preflight


@dataclass
class PiiResult:
    findings: list[dict]        # [{type, start, end, score}] — NO matched text
    data_classification: str    # "none" | "pii"
    redacted_text: str


class PiiBackendError(RuntimeError):
    """A sanitized fail-closed error from the configured PII backend."""

_analyzer: AnalyzerEngine | None = None
_anonymizer: AnonymizerEngine | None = None
_lock: asyncio.Lock | None = None
_executor: ThreadPoolExecutor | None = None
_executor_max_workers: int | None = None
_google_client: Any | None = None
_initialized_backend: str | None = None
_backend_name = "presidio"
_google_project: str | None = None
_google_location = "global"
_google_min_likelihood = "POSSIBLE"
_google_info_types: tuple[str, ...] = ()
_google_timeout_seconds = 5.0

# Google limits synchronous content-inspection requests to 0.5 MB. Leave room
# for protobuf/config overhead and overlap chunks so a structured value at a
# boundary is still inspected as one value.
_GOOGLE_MAX_CONTENT_BYTES = 450_000
_GOOGLE_CHUNK_OVERLAP_BYTES = 4_096

_GOOGLE_TYPE_MAP = {
    "PERSON_NAME": "PERSON",
    "US_SOCIAL_SECURITY_NUMBER": "US_SSN",
}
_LIKELIHOOD_SCORES = {
    0: 0.0,
    1: 0.1,
    2: 0.3,
    3: 0.5,
    4: 0.7,
    5: 0.9,
}

def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock

# --- Class-aware concurrency scheduling (ai-gateway-pvo) --------------------
#
# A single shared asyncio.Semaphore gating every call into scan()/redact()/
# run() with ONE limit and no notion of caller identity would mean a
# sustained burst of bulk/MCP-tool-response scans (app/main.py's
# /v1/dlp/pii-scan) can occupy every permit and make a concurrent,
# latency-sensitive interactive scan (app/pipeline.py's pii_stage(), part of
# /v1/inspect) queue behind the full depth of that burst. That single-limit
# design (formerly `_get_semaphore()`/`_semaphore`) has been removed entirely
# (ai-gateway-fob) now that the tests below assert against the pools
# directly instead of that removed function.
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
# Documented wait bound (AC1): `_max_scan_seconds` below is this module's
# documented upper bound on any single scan/redact call's runtime. Because a
# queued request is always the FIFO head of its own home pool (per the
# waiter-check above), it waits for at most ONE currently-held permit in
# that pool to free up — i.e. at most `_max_scan_seconds` — never for the
# depth of a burst on the other class's pool (which would be unbounded,
# O(burst size)). For the Google backend a single call is itself bounded by
# the configured `google_dlp_timeout_seconds` (up to 30s, see settings.py),
# so `initialize()` sets this to that value instead of leaving it a fixed
# constant that could silently fall out of sync with it. The presidio
# backend has no per-call timeout knob, so it keeps the historical 5s
# expectation.
_max_scan_seconds = 5.0

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


# Why three pools and not one semaphore (ai-gateway-vzs code-review follow-up):
# AC2 requires a genuine, *guaranteed* minimum concurrency floor per request
# class under sustained dual contention — not merely "usually gets some
# slots." A single shared asyncio.Semaphore has no notion of caller identity:
# it hands out permits strictly in arrival order, so a sustained burst from
# one class can occupy every permit indefinitely, leaving the other class
# with zero enforced concurrency — that is a hard AC2 violation, not an edge
# case. Only a semaphore reserved per class can make that floor a guarantee
# rather than a coincidence of scheduling order, which is what the
# `interactive`/`bulk` home pools below provide. The third, `shared` pool is
# not part of that guarantee; it exists purely so an uncontended class can
# still opportunistically reach the full aggregate N (AC4) instead of being
# capped at its own reserved fraction. A single shared semaphore was
# evaluated against AC1-AC5 and fails AC1 and AC2 for exactly this reason; a
# naive fixed 50/50 split with no shared/borrow capacity was also evaluated
# and fails AC4 — see the module-level comment above for that comparison.
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

    Uses the public `Semaphore.locked()` API instead of reading the
    private `_value`/`_waiters` fields directly -- those are CPython
    implementation details, not part of the public API, and are not
    guaranteed stable across versions. `locked()` is true when there is
    no free permit OR at least one non-cancelled waiter is already
    queued; negating it gives exactly "a permit is free AND nobody
    legitimately queued is ahead of us" -- the same condition the old
    field-based check enforced, including the "don't jump the FIFO line
    ahead of an already-queued task" requirement the AC1/AC2 bound below
    depends on. (A waiter that has already been cancelled but not yet
    pruned from the internal queue by its own coroutine no longer counts
    under `locked()` -- correctly so, since a cancelled waiter has
    abandoned the queue and is not "legitimately queued" anymore.)
    """
    return not sem.locked()


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


async def initialize(
    spacy_model: str = "en_core_web_lg",
    *,
    backend: str = "presidio",
    google_project: str | None = None,
    google_location: str = "global",
    google_api_endpoint: str | None = None,
    google_expected_service_account: str | None = None,
    google_min_likelihood: str = "POSSIBLE",
    google_info_types: tuple[str, ...] = (),
    google_timeout_seconds: float = 5.0,
) -> None:
    global _analyzer, _anonymizer, _executor, _executor_max_workers
    global _backend_name, _google_client, _google_project, _google_location
    global _google_min_likelihood, _google_info_types, _google_timeout_seconds
    global _initialized_backend, _max_scan_seconds
    async with _get_lock():
        if _initialized_backend == backend:
            return  # already initialized (idempotent)
        if _initialized_backend is not None:
            raise RuntimeError("PII backend cannot be changed after initialization")
        if _executor is None:
            _executor_max_workers = min(32, (os.cpu_count() or 1) + 4)
            _executor = ThreadPoolExecutor(max_workers=_executor_max_workers)
            asyncio.get_running_loop().set_default_executor(_executor)
        _backend_name = backend
        if backend == "google":
            if not google_project:
                raise ValueError("Google PII backend requires a project")
            _google_project = google_project
            _google_location = google_location
            _google_min_likelihood = google_min_likelihood
            _google_info_types = google_info_types
            _google_timeout_seconds = google_timeout_seconds
            _max_scan_seconds = google_timeout_seconds
            try:
                credential_preflight = await asyncio.to_thread(
                    preflight,
                    expected_service_account=google_expected_service_account,
                )
            except GoogleCredentialPreflightError as exc:
                raise PiiBackendError(str(exc)) from None
            client_factory = (
                partial(
                    dlp_v2.DlpServiceClient,
                    credentials=credential_preflight.credentials,
                    client_options=ClientOptions(api_endpoint=google_api_endpoint),
                )
                if google_api_endpoint
                else partial(
                    dlp_v2.DlpServiceClient,
                    credentials=credential_preflight.credentials,
                )
            )
            _google_client = await asyncio.to_thread(client_factory)
            _initialized_backend = backend
            return
        if backend != "presidio":
            raise ValueError(f"Unsupported PII backend: {backend}")
        from presidio_analyzer.nlp_engine import NlpEngineProvider
        provider = NlpEngineProvider(nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "en", "model_name": spacy_model}],
        })
        nlp_engine = await asyncio.to_thread(provider.create_engine)
        _analyzer = await asyncio.to_thread(AnalyzerEngine, nlp_engine=nlp_engine)
        _register_high_recall_ssn_recognizer(_analyzer)
        _anonymizer = await asyncio.to_thread(AnonymizerEngine)
        _initialized_backend = backend


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

# Presidio's spaCy NER recognizer classifies ordinary technical/product proper
# nouns (Django, Gemini, Claude, ...) as PERSON with high confidence, which
# would corrupt search and tool-dispatch queries before they ever reach a
# provider. Mirror the Google DLP backend's policy decision (see the
# "PERSON_NAME is intentionally not enabled by default" note in
# docs/google-sensitive-data-protection.md) and exclude PERSON from the
# default detection set entirely rather than special-casing individual terms.
# Every other supported entity type (US_SSN, EMAIL_ADDRESS, PHONE_NUMBER,
# CREDIT_CARD, ...) is unaffected. Filtering the results (rather than passing
# an `entities=` allowlist into analyze()) keeps this independent of the
# analyzer's supported-entity introspection.
#
# This is a process-wide default applied identically to every tenant and
# every request -- no field on config/tenants.yaml, config/users.yaml,
# InspectRequest, or PipelineContext feeds into this filter today, so there
# is no per-tenant or per-request way to re-enable PERSON. A feasibility
# assessment for adding such an opt-in (ai-gateway-6xx) concluded it is
# feasible -- config/tenants.yaml already carries other per-tenant PII policy
# fields (pii_action, pii_redaction_notification) reconciled the same way,
# and could be extended the same way -- but is deferred rather than
# implemented: opting a tenant in would only expose it to the same
# false-positive corpus described above, not fix it, and Presidio itself is
# a rollback-only backend slated for removal after the Google DLP production
# soak (ai-gateway-fcr). See "Per-tenant PERSON/name-detection opt-in:
# feasibility assessment (ai-gateway-6xx)" in
# docs/google-sensitive-data-protection.md for the full assessment.
_DISABLED_PRESIDIO_ENTITIES = frozenset({"PERSON"})

# The PERSON-level exclusion above assumes the false positive is confined to
# one entity type. It is not: ai-gateway-10vg found the same spaCy NER model
# nondeterministically tagging "Django" as LOCATION instead of PERSON on some
# CI runs (same input, same model -- the tie-breaking floating-point sums in
# the neural net forward pass differ slightly by platform/BLAS backend, and
# "Django" sits close enough to the decision boundary for that to flip its
# label). Disabling LOCATION wholesale like PERSON would silently stop
# flagging genuine location PII (e.g. "I am flying to Paris tomorrow" must
# still produce a LOCATION finding -- see ai-gateway-10vg's acceptance
# criteria), so the entity-type-level approach above does not generalize
# here. Instead, suppress specific known false-positive terms regardless of
# whichever entity type the model happens to assign them on a given run.
# This list is exactly the regression corpus already exercised by
# test_technical_proper_nouns_are_not_misdetected_as_person and
# docs/google-sensitive-data-protection.md's technical-proper-noun corpus.
_TECHNICAL_TERM_FALSE_POSITIVES = frozenset({
    "django", "flask", "fastapi", "postgres", "postgresql",
    "kubernetes", "redis", "react", "gemini", "claude",
})


def _is_technical_term_false_positive(text: str, result: RecognizerResult) -> bool:
    span = text[result.start:result.end].strip().strip(",.;:!?\"'()").lower()
    return span in _TECHNICAL_TERM_FALSE_POSITIVES


async def scan(
    text: str, request_class: PiiRequestClass = PII_CLASS_INTERACTIVE
) -> list[RecognizerResult]:
    assert _analyzer is not None, "call initialize() first"
    async with _acquire_scan_slot(request_class):
        results = await asyncio.to_thread(_analyzer.analyze, text=text, language="en")
    return [
        r
        for r in results
        if r.entity_type not in _DISABLED_PRESIDIO_ENTITIES
        and not _is_technical_term_false_positive(text, r)
    ]

async def redact(
    text: str,
    results: list[RecognizerResult],
    request_class: PiiRequestClass = PII_CLASS_INTERACTIVE,
) -> str:
    assert _anonymizer is not None, "call initialize() first"
    operators = {
        result.entity_type: OperatorConfig(
            "replace",
            {"new_value": f"[{result.entity_type}]"},
        )
        for result in results
    }
    async with _acquire_scan_slot(request_class):
        anonymized = await asyncio.to_thread(
            _anonymizer.anonymize,
            text=text,
            analyzer_results=cast(Any, results),
            operators=operators,
        )
    return anonymized.text


def _range_is_set(location: Any, field_name: str) -> bool:
    protobuf = getattr(location, "_pb", None)
    if protobuf is not None:
        return bool(protobuf.HasField(field_name))
    value = getattr(location, field_name, None)
    return value is not None and int(value.end) > int(value.start)


def _byte_to_codepoint_range(text: str, start: int, end: int) -> tuple[int, int]:
    encoded = text.encode("utf-8")
    if start < 0 or end <= start or end > len(encoded):
        raise PiiBackendError("Google DLP returned an invalid text range")
    try:
        return (
            len(encoded[:start].decode("utf-8")),
            len(encoded[:end].decode("utf-8")),
        )
    except UnicodeDecodeError as exc:
        raise PiiBackendError("Google DLP returned an invalid text range") from exc


def _finding_range(text: str, finding: Any) -> tuple[int, int]:
    location = finding.location
    if _range_is_set(location, "codepoint_range"):
        return int(location.codepoint_range.start), int(location.codepoint_range.end)
    if _range_is_set(location, "byte_range"):
        return _byte_to_codepoint_range(
            text,
            int(location.byte_range.start),
            int(location.byte_range.end),
        )
    raise PiiBackendError("Google DLP returned a finding without a text range")


def _deduplicate_findings(findings: list[dict]) -> list[dict]:
    findings.sort(key=lambda item: (item["start"], -item["end"], -item["score"]))
    deduplicated: list[dict] = []
    for item in findings:
        if deduplicated and item["start"] < deduplicated[-1]["end"]:
            previous = deduplicated[-1]
            # Never discard part of an overlapping sensitive span. Keep the
            # strongest label but redact the union so weaker/nested findings
            # cannot leave sensitive prefixes or suffixes exposed.
            if item["score"] > previous["score"]:
                previous["type"] = item["type"]
                previous["score"] = item["score"]
            previous["end"] = max(previous["end"], item["end"])
            continue
        deduplicated.append(item)
    return deduplicated


def _normalize_google_findings(text: str, findings: Any) -> list[dict]:
    normalized: list[dict] = []
    for finding in findings:
        start, end = _finding_range(text, finding)
        if start < 0 or end <= start or end > len(text):
            raise PiiBackendError("Google DLP returned an invalid text range")
        provider_type = str(finding.info_type.name)
        normalized.append({
            "type": _GOOGLE_TYPE_MAP.get(provider_type, provider_type),
            "start": start,
            "end": end,
            "score": _LIKELIHOOD_SCORES.get(int(finding.likelihood), 0.0),
        })

    # A custom and built-in info type may report the same span. Keep the
    # strongest finding so local replacement never creates nested markers.
    return _deduplicate_findings(normalized)


def _google_text_chunks(text: str) -> list[tuple[int, str]]:
    encoded = text.encode("utf-8")
    if len(encoded) <= _GOOGLE_MAX_CONTENT_BYTES:
        return [(0, text)]
    if _GOOGLE_CHUNK_OVERLAP_BYTES >= _GOOGLE_MAX_CONTENT_BYTES:
        raise RuntimeError("Google DLP chunk overlap must be smaller than chunk size")

    chunks: list[tuple[int, str]] = []
    start_byte = 0
    start_codepoint = 0
    while start_byte < len(encoded):
        end_byte = min(start_byte + _GOOGLE_MAX_CONTENT_BYTES, len(encoded))
        while end_byte < len(encoded) and encoded[end_byte] & 0xC0 == 0x80:
            end_byte -= 1
        chunk = encoded[start_byte:end_byte].decode("utf-8")
        chunks.append((start_codepoint, chunk))
        if end_byte == len(encoded):
            break

        next_start = end_byte - _GOOGLE_CHUNK_OVERLAP_BYTES
        while encoded[next_start] & 0xC0 == 0x80:
            next_start -= 1
        start_codepoint += len(encoded[start_byte:next_start].decode("utf-8"))
        start_byte = next_start
    return chunks


def _redact_with_typed_markers(text: str, findings: list[dict]) -> str:
    redacted = text
    for finding in reversed(findings):
        redacted = (
            redacted[:finding["start"]]
            + f"[{finding['type']}]"
            + redacted[finding["end"]:]
        )
    return redacted


# Backoff schedule for `_inspect_content_with_backoff`, replicated from the
# `google.api_core.retry.Retry(initial=0.2, maximum=1.0, multiplier=2.0, ...)`
# object this module used to hand the WHOLE inspect_content call (including
# all retry sleeps) to. Kept as bare constants (rather than a Retry instance)
# because the retry loop is now driven from the async side - see
# `_inspect_content_with_backoff` for why.
_GOOGLE_RETRY_INITIAL_SECONDS = 0.2
_GOOGLE_RETRY_MAX_SECONDS = 1.0
_GOOGLE_RETRY_MULTIPLIER = 2.0
_GOOGLE_RETRYABLE_EXCEPTIONS = (
    google_exceptions.DeadlineExceeded,
    google_exceptions.ServiceUnavailable,
)


async def _inspect_content_with_backoff(
    request: dict,
    *,
    timeout_seconds: float,
    request_class: PiiRequestClass,
) -> Any:
    """Run one or more `inspect_content` attempts, retrying the same
    transient-error set and backoff schedule this module previously
    delegated to `google.api_core.retry.Retry` - but acquiring the
    class-aware pool slot (`_acquire_scan_slot`) only for the duration of
    EACH synchronous attempt, never across a backoff sleep.

    Previously the `Retry` object wrapped the whole `inspect_content` call,
    so its internal backoff sleeps ran *inside* the thread-pool worker while
    the async pool slot stayed held for the entire retry/backoff window (up
    to `timeout_seconds`). On a small pool (1-2 slots) that let one retrying
    request hold its class's only slot through every backoff sleep, starving
    the other request class of its guaranteed minimum (ai-gateway-ypb).
    Releasing the slot here (by exiting `_acquire_scan_slot` before each
    `asyncio.sleep`) lets a concurrent request of the other class claim its
    home pool immediately during that wait, while the retryable-exception
    set, backoff schedule, and overall `timeout_seconds` deadline are
    unchanged from before.
    """
    client = _google_client
    if client is None:
        raise RuntimeError("call initialize() first")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    backoff = _GOOGLE_RETRY_INITIAL_SECONDS
    while True:
        try:
            async with _acquire_scan_slot(request_class):
                return await asyncio.to_thread(
                    partial(
                        client.inspect_content,
                        request=request,
                        retry=None,
                        timeout=timeout_seconds,
                    )
                )
        except _GOOGLE_RETRYABLE_EXCEPTIONS:
            if loop.time() >= deadline:
                raise
            await asyncio.sleep(backoff)
            backoff = min(backoff * _GOOGLE_RETRY_MULTIPLIER, _GOOGLE_RETRY_MAX_SECONDS)


async def run_google(
    text: str,
    *,
    project: str,
    location: str,
    min_likelihood: str,
    info_types: tuple[str, ...],
    timeout_seconds: float,
    request_class: PiiRequestClass = PII_CLASS_INTERACTIVE,
) -> PiiResult:
    if _google_client is None:
        raise RuntimeError("call initialize() first")
    if not text:
        return PiiResult(findings=[], data_classification="none", redacted_text="")
    findings: list[dict] = []
    for codepoint_offset, chunk in _google_text_chunks(text):
        request = {
            "parent": f"projects/{project}/locations/{location}",
            "inspect_config": {
                "info_types": [{"name": name} for name in info_types],
                "min_likelihood": min_likelihood,
                "include_quote": False,
                "limits": {"max_findings_per_request": 3_000},
            },
            "item": {"value": chunk},
        }
        try:
            response = await _inspect_content_with_backoff(
                request,
                timeout_seconds=timeout_seconds,
                request_class=request_class,
            )
        except Exception:
            # Do not leak provider diagnostics or prompt content through API errors.
            raise PiiBackendError("Google DLP inspection failed") from None
        if getattr(response.result, "findings_truncated", False):
            raise PiiBackendError("Google DLP returned incomplete findings")
        for finding in _normalize_google_findings(chunk, response.result.findings):
            findings.append({
                **finding,
                "start": finding["start"] + codepoint_offset,
                "end": finding["end"] + codepoint_offset,
            })

    findings = _deduplicate_findings(findings)
    return PiiResult(
        findings=findings,
        data_classification="pii" if findings else "none",
        redacted_text=_redact_with_typed_markers(text, findings),
    )

async def run(
    text: str, request_class: PiiRequestClass = PII_CLASS_INTERACTIVE
) -> PiiResult:
    if _backend_name == "google":
        assert _google_project is not None, "call initialize() first"
        return await run_google(
            text,
            project=_google_project,
            location=_google_location,
            min_likelihood=_google_min_likelihood,
            info_types=_google_info_types,
            timeout_seconds=_google_timeout_seconds,
            request_class=request_class,
        )
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
