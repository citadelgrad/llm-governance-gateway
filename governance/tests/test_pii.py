import pytest
from fastapi.testclient import TestClient
from presidio_analyzer import RecognizerResult
from presidio_anonymizer import AnonymizerEngine

from app import pii
from app.main import app
from app.settings import settings


@pytest.mark.asyncio
async def test_entity_placeholders_are_visible_in_html_renderers(monkeypatch):
    monkeypatch.setattr(pii, "_anonymizer", AnonymizerEngine())
    monkeypatch.setattr(pii, "_executor_max_workers", 1)

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


# Regression corpus for ai-gateway-qi5: Presidio's spaCy NER model classifies
# ordinary technical/product proper nouns as PERSON with high confidence,
# which would corrupt search and tool-dispatch queries before they ever reach
# a provider. `en_core_web_lg` is the production default (settings.spacy_model)
# and the model that actually reproduces the false positives below, so force
# a fresh init against it regardless of whatever backend/model an earlier
# test module already initialized (initialize() is a no-op once a backend
# name is locked in).
_TECHNICAL_PROPER_NOUN_QUERIES = [
    "Django is a great choice for this project.",
    "I use Gemini and Claude every day.",
    "Kubernetes, Redis, and Postgres are all open source.",
    "what is the latest version of Django web framework",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("text", _TECHNICAL_PROPER_NOUN_QUERIES)
async def test_technical_proper_nouns_are_not_misdetected_as_person(monkeypatch, text):
    monkeypatch.setattr(pii, "_initialized_backend", None)
    await pii.initialize("en_core_web_lg")

    result = await pii.run(text)

    assert "PERSON" not in {finding["type"] for finding in result.findings}


@pytest.mark.asyncio
async def test_django_query_reaches_downstream_dispatch_intact(monkeypatch):
    text = "what is the latest version of Django web framework"
    monkeypatch.setattr(pii, "_initialized_backend", None)
    await pii.initialize("en_core_web_lg")

    result = await pii.run(text)

    assert result.findings == []
    assert result.data_classification == "none"
    assert result.redacted_text == text
    assert "Django" in result.redacted_text


def test_pii_scan_endpoint_leaves_django_query_intact(monkeypatch):
    monkeypatch.setattr(pii, "_initialized_backend", None)

    with TestClient(app) as client:
        response = client.post(
            "/v1/dlp/pii-scan",
            json={"text": "what is the latest version of Django web framework"},
            headers={"X-Internal-Token": settings.internal_token},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["redacted_text"] == "what is the latest version of Django web framework"
    assert body["pii_findings"] == []
    assert body["data_classification"] == "none"
