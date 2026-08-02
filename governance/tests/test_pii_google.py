from types import SimpleNamespace
from typing import Any

import pytest
from google.api_core.exceptions import ServiceUnavailable

from app import pii
from app.settings import Settings


def _finding(
    info_type: str,
    start: int,
    end: int,
    likelihood: int = 4,
    *,
    byte_range: bool = False,
):
    empty_range = SimpleNamespace(start=0, end=0)
    location = SimpleNamespace(
        codepoint_range=empty_range if byte_range else SimpleNamespace(start=start, end=end),
        byte_range=SimpleNamespace(start=start, end=end) if byte_range else empty_range,
    )
    return SimpleNamespace(
        info_type=SimpleNamespace(name=info_type),
        likelihood=likelihood,
        location=location,
    )


class _FakeDlpClient:
    def __init__(self, findings=None, error=None):
        self.findings = findings or []
        self.error = error
        self.calls = []

    def inspect_content(self, request, retry, timeout):
        self.calls.append({"request": request, "retry": retry, "timeout": timeout})
        if self.error:
            raise self.error
        return SimpleNamespace(result=SimpleNamespace(findings=self.findings))


@pytest.mark.asyncio
async def test_initialize_google_wires_runtime_and_regional_endpoint(monkeypatch):
    client = _FakeDlpClient([_finding("EMAIL_ADDRESS", 0, 17)])
    created_with = {}

    def fake_client_factory(*, client_options: Any = None):
        created_with["endpoint"] = client_options.api_endpoint
        return client

    monkeypatch.setattr(pii, "_initialized_backend", None)
    monkeypatch.setattr(pii, "_backend_name", "presidio")
    monkeypatch.setattr(pii, "_google_client", None)
    monkeypatch.setattr(pii, "_executor_max_workers", 4)
    monkeypatch.setattr(pii, "_semaphore", None)
    monkeypatch.setattr(pii.dlp_v2, "DlpServiceClient", fake_client_factory)

    await pii.initialize(
        backend="google",
        google_project="privacy-project",
        google_location="us",
        google_api_endpoint="us-dlp.googleapis.com",
        google_min_likelihood="LIKELY",
        google_info_types=("EMAIL_ADDRESS",),
        google_timeout_seconds=3.0,
    )
    result = await pii.run("scott@example.com")

    assert created_with == {"endpoint": "us-dlp.googleapis.com"}
    assert result.redacted_text == "[EMAIL_ADDRESS]"
    assert client.calls[0]["request"]["parent"] == "projects/privacy-project/locations/us"
    assert client.calls[0]["request"]["inspect_config"]["min_likelihood"] == "LIKELY"
    assert client.calls[0]["timeout"] == 3.0


@pytest.mark.asyncio
async def test_google_backend_preserves_contract_and_request_policy(monkeypatch):
    text = "Email me at scott@example.com or call 555-867-5309"
    client = _FakeDlpClient(
        [
            _finding("EMAIL_ADDRESS", 12, 29, likelihood=5),
            _finding("PHONE_NUMBER", 38, 50, likelihood=4),
        ]
    )
    monkeypatch.setattr(pii, "_google_client", client)
    monkeypatch.setattr(pii, "_executor_max_workers", 2)
    monkeypatch.setattr(pii, "_semaphore", None)

    result = await pii.run_google(
        text,
        project="privacy-project",
        location="us-central1",
        min_likelihood="POSSIBLE",
        info_types=("EMAIL_ADDRESS", "PHONE_NUMBER"),
        timeout_seconds=4.0,
    )

    assert result.data_classification == "pii"
    assert result.redacted_text == "Email me at [EMAIL_ADDRESS] or call [PHONE_NUMBER]"
    assert result.findings == [
        {"type": "EMAIL_ADDRESS", "start": 12, "end": 29, "score": 0.9},
        {"type": "PHONE_NUMBER", "start": 38, "end": 50, "score": 0.7},
    ]
    call = client.calls[0]
    assert call["request"]["parent"] == "projects/privacy-project/locations/us-central1"
    assert call["request"]["item"] == {"value": text}
    assert call["request"]["inspect_config"]["include_quote"] is False
    assert call["request"]["inspect_config"]["min_likelihood"] == "POSSIBLE"
    assert call["request"]["inspect_config"]["info_types"] == [
        {"name": "EMAIL_ADDRESS"},
        {"name": "PHONE_NUMBER"},
    ]
    assert call["request"]["inspect_config"]["limits"] == {
        "max_findings_per_request": 3000
    }
    assert call["timeout"] == 4.0


@pytest.mark.asyncio
async def test_google_backend_converts_utf8_byte_offsets(monkeypatch):
    text = "🔒 email scott@example.com"
    encoded = text.encode("utf-8")
    start = encoded.index(b"scott")
    end = len(encoded)
    client = _FakeDlpClient([_finding("EMAIL_ADDRESS", start, end, byte_range=True)])
    monkeypatch.setattr(pii, "_google_client", client)
    monkeypatch.setattr(pii, "_executor_max_workers", 1)
    monkeypatch.setattr(pii, "_semaphore", None)

    result = await pii.run_google(
        text,
        project="privacy-project",
        location="global",
        min_likelihood="POSSIBLE",
        info_types=("EMAIL_ADDRESS",),
        timeout_seconds=5.0,
    )

    assert result.findings[0]["start"] == 8
    assert result.findings[0]["end"] == len(text)
    assert result.redacted_text == "🔒 email [EMAIL_ADDRESS]"


@pytest.mark.asyncio
async def test_google_backend_leaves_technical_query_intact_without_findings(monkeypatch):
    text = "what is the latest version of Django web framework"
    monkeypatch.setattr(pii, "_google_client", _FakeDlpClient())
    monkeypatch.setattr(pii, "_executor_max_workers", 1)
    monkeypatch.setattr(pii, "_semaphore", None)

    result = await pii.run_google(
        text,
        project="privacy-project",
        location="global",
        min_likelihood="POSSIBLE",
        info_types=("PERSON_NAME",),
        timeout_seconds=5.0,
    )

    assert result.findings == []
    assert result.data_classification == "none"
    assert result.redacted_text == text


@pytest.mark.asyncio
async def test_google_backend_skips_empty_content(monkeypatch):
    client = _FakeDlpClient()
    monkeypatch.setattr(pii, "_google_client", client)

    result = await pii.run_google(
        "",
        project="privacy-project",
        location="global",
        min_likelihood="POSSIBLE",
        info_types=("EMAIL_ADDRESS",),
        timeout_seconds=5.0,
    )

    assert result == pii.PiiResult([], "none", "")
    assert client.calls == []


@pytest.mark.asyncio
async def test_google_backend_sanitizes_provider_failures(monkeypatch):
    client = _FakeDlpClient(error=ServiceUnavailable("upstream leaked detail"))
    monkeypatch.setattr(pii, "_google_client", client)
    monkeypatch.setattr(pii, "_executor_max_workers", 1)
    monkeypatch.setattr(pii, "_semaphore", None)

    with pytest.raises(pii.PiiBackendError) as exc_info:
        await pii.run_google(
            "secret prompt contents",
            project="privacy-project",
            location="global",
            min_likelihood="POSSIBLE",
            info_types=("EMAIL_ADDRESS",),
            timeout_seconds=1.0,
        )

    assert str(exc_info.value) == "Google DLP inspection failed"
    assert exc_info.value.__cause__ is None


@pytest.mark.asyncio
async def test_google_backend_fails_closed_on_truncated_findings(monkeypatch):
    class TruncatedClient:
        def inspect_content(self, **_kwargs):
            return SimpleNamespace(
                result=SimpleNamespace(findings=[], findings_truncated=True)
            )

    monkeypatch.setattr(pii, "_google_client", TruncatedClient())
    monkeypatch.setattr(pii, "_executor_max_workers", 1)
    monkeypatch.setattr(pii, "_semaphore", None)

    with pytest.raises(pii.PiiBackendError, match="incomplete findings"):
        await pii.run_google(
            "text",
            project="privacy-project",
            location="global",
            min_likelihood="POSSIBLE",
            info_types=("EMAIL_ADDRESS",),
            timeout_seconds=5.0,
        )


def test_overlapping_findings_redact_the_complete_union():
    findings = [
        {"type": "EMAIL_ADDRESS", "start": 0, "end": 7, "score": 0.5},
        {"type": "PHONE_NUMBER", "start": 5, "end": 10, "score": 0.9},
    ]

    deduplicated = pii._deduplicate_findings(findings)

    assert deduplicated == [
        {"type": "PHONE_NUMBER", "start": 0, "end": 10, "score": 0.9}
    ]
    assert pii._redact_with_typed_markers("ABCDEFGHIJ", deduplicated) == "[PHONE_NUMBER]"


@pytest.mark.asyncio
async def test_google_backend_chunks_large_unicode_text_with_overlap(monkeypatch):
    marker = "test@example.com"

    class DetectingClient:
        def __init__(self):
            self.chunks = []

        def inspect_content(self, *, request, retry, timeout):
            chunk = request["item"]["value"]
            self.chunks.append(chunk)
            start = chunk.find(marker)
            findings = [] if start < 0 else [
                _finding("EMAIL_ADDRESS", start, start + len(marker))
            ]
            return SimpleNamespace(
                result=SimpleNamespace(findings=findings, findings_truncated=False)
            )

    text = "🔒" * 8 + marker + "x" * 20
    client = DetectingClient()
    monkeypatch.setattr(pii, "_google_client", client)
    monkeypatch.setattr(pii, "_GOOGLE_MAX_CONTENT_BYTES", 40)
    monkeypatch.setattr(pii, "_GOOGLE_CHUNK_OVERLAP_BYTES", 20)
    monkeypatch.setattr(pii, "_executor_max_workers", 1)
    monkeypatch.setattr(pii, "_semaphore", None)

    result = await pii.run_google(
        text,
        project="privacy-project",
        location="global",
        min_likelihood="POSSIBLE",
        info_types=("EMAIL_ADDRESS",),
        timeout_seconds=5.0,
    )

    assert len(client.chunks) > 1
    assert all(len(chunk.encode("utf-8")) <= 40 for chunk in client.chunks)
    assert result.findings == [{
        "type": "EMAIL_ADDRESS",
        "start": 8,
        "end": 8 + len(marker),
        "score": 0.7,
    }]
    assert result.redacted_text == "🔒" * 8 + "[EMAIL_ADDRESS]" + "x" * 20


def test_google_settings_require_project():
    with pytest.raises(ValueError, match="GOOGLE_CLOUD_PROJECT"):
        Settings(
            _env_file=None,
            internal_token="test-token",
            pseudonym_hmac_key="test-hmac",
            pii_backend="google",
            google_cloud_project=None,
        )


def test_google_settings_parse_info_types():
    settings = Settings(
        _env_file=None,
        internal_token="test-token",
        pseudonym_hmac_key="test-hmac",
        pii_backend="google",
        google_cloud_project="privacy-project",
        google_dlp_info_types="EMAIL_ADDRESS, PHONE_NUMBER,EMAIL_ADDRESS",
    )

    assert settings.google_dlp_info_type_names == ("EMAIL_ADDRESS", "PHONE_NUMBER")


def test_presidio_settings_allow_empty_google_info_types():
    settings = Settings(
        _env_file=None,
        internal_token="test-token",
        pseudonym_hmac_key="test-hmac",
        pii_backend="presidio",
        google_dlp_location="",
        google_dlp_api_endpoint="https://invalid-for-google.example",
        google_dlp_info_types="",
    )

    assert settings.google_dlp_info_type_names == ()
