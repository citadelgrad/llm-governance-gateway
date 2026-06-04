from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, cast

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

def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock

async def initialize(spacy_model: str = "en_core_web_lg") -> None:
    global _analyzer, _anonymizer
    async with _get_lock():
        if _analyzer is not None:
            return  # already initialized (idempotent)
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

async def scan(text: str) -> list[RecognizerResult]:
    assert _analyzer is not None, "call initialize() first"
    return await asyncio.to_thread(_analyzer.analyze, text=text, language="en")

async def redact(text: str, results: list[RecognizerResult]) -> str:
    assert _anonymizer is not None, "call initialize() first"
    anonymized = await asyncio.to_thread(
        _anonymizer.anonymize,
        text=text,
        analyzer_results=cast(Any, results),
    )
    return anonymized.text

async def run(text: str) -> PiiResult:
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
