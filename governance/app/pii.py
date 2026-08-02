from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from typing import Any, cast

from google.api_core import exceptions as google_exceptions
from google.api_core.client_options import ClientOptions
from google.api_core.retry import Retry, if_exception_type
from google.cloud import dlp_v2
from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer, RecognizerResult
from presidio_anonymizer import AnonymizerEngine
from presidio_anonymizer.entities import OperatorConfig


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
_semaphore: asyncio.Semaphore | None = None
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

def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        assert _executor_max_workers is not None, "call initialize() first"
        _semaphore = asyncio.Semaphore(_executor_max_workers)
    return _semaphore

async def initialize(
    spacy_model: str = "en_core_web_lg",
    *,
    backend: str = "presidio",
    google_project: str | None = None,
    google_location: str = "global",
    google_api_endpoint: str | None = None,
    google_min_likelihood: str = "POSSIBLE",
    google_info_types: tuple[str, ...] = (),
    google_timeout_seconds: float = 5.0,
) -> None:
    global _analyzer, _anonymizer, _executor, _executor_max_workers
    global _backend_name, _google_client, _google_project, _google_location
    global _google_min_likelihood, _google_info_types, _google_timeout_seconds
    global _initialized_backend
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
            client_factory = (
                partial(
                    dlp_v2.DlpServiceClient,
                    client_options=ClientOptions(api_endpoint=google_api_endpoint),
                )
                if google_api_endpoint
                else dlp_v2.DlpServiceClient
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

async def scan(text: str) -> list[RecognizerResult]:
    assert _analyzer is not None, "call initialize() first"
    async with _get_semaphore():
        return await asyncio.to_thread(_analyzer.analyze, text=text, language="en")

async def redact(text: str, results: list[RecognizerResult]) -> str:
    assert _anonymizer is not None, "call initialize() first"
    operators = {
        result.entity_type: OperatorConfig(
            "replace",
            {"new_value": f"[{result.entity_type}]"},
        )
        for result in results
    }
    async with _get_semaphore():
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


async def run_google(
    text: str,
    *,
    project: str,
    location: str,
    min_likelihood: str,
    info_types: tuple[str, ...],
    timeout_seconds: float,
) -> PiiResult:
    if _google_client is None:
        raise RuntimeError("call initialize() first")
    if not text:
        return PiiResult(findings=[], data_classification="none", redacted_text="")
    retry = Retry(
        predicate=if_exception_type(
            google_exceptions.DeadlineExceeded,
            google_exceptions.ServiceUnavailable,
        ),
        initial=0.2,
        maximum=1.0,
        multiplier=2.0,
        deadline=timeout_seconds,
    )
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
            async with _get_semaphore():
                response = await asyncio.to_thread(
                    partial(
                        _google_client.inspect_content,
                        request=request,
                        retry=retry,
                        timeout=timeout_seconds,
                    )
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

async def run(text: str) -> PiiResult:
    if _backend_name == "google":
        assert _google_project is not None, "call initialize() first"
        return await run_google(
            text,
            project=_google_project,
            location=_google_location,
            min_likelihood=_google_min_likelihood,
            info_types=_google_info_types,
            timeout_seconds=_google_timeout_seconds,
        )
    results = await scan(text)
    redacted = text
    if results:
        redacted = await redact(text, results)
    findings = [
        {"type": r.entity_type, "start": r.start, "end": r.end, "score": r.score}
        for r in results
    ]
    return PiiResult(
        findings=findings,
        data_classification="pii" if results else "none",
        redacted_text=redacted,
    )
