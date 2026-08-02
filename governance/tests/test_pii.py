import pytest
from presidio_analyzer import RecognizerResult
from presidio_anonymizer import AnonymizerEngine

from app import pii


@pytest.mark.asyncio
async def test_entity_placeholders_are_visible_in_html_renderers(monkeypatch):
    monkeypatch.setattr(pii, "_anonymizer", AnonymizerEngine())
    monkeypatch.setattr(pii, "_executor_max_workers", 1)
    monkeypatch.setattr(pii, "_semaphore", None)

    redacted = await pii.redact(
        "Scott emailed scott@example.com",
        [
            RecognizerResult(entity_type="PERSON", start=0, end=5, score=0.9),
            RecognizerResult(entity_type="EMAIL_ADDRESS", start=14, end=31, score=0.9),
        ],
    )

    assert redacted == "[PERSON] emailed [EMAIL_ADDRESS]"
    assert "<PERSON>" not in redacted
    assert "<EMAIL_ADDRESS>" not in redacted


@pytest.mark.asyncio
async def test_ssn_shaped_test_value_is_detected_and_redacted():
    await pii.initialize("en_core_web_sm")

    result = await pii.run("My SSN is 123-45-6789, can you help?")

    assert result.findings
    assert {finding["type"] for finding in result.findings} == {"US_SSN"}
    assert "123-45-6789" not in result.redacted_text
    assert result.data_classification == "pii"
