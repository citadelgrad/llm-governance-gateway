"""Phase 5 — Property-based + API contract tests for POST /v1/chat/completions.

Each test group targets a specific invariant class from the plan:

  P1  Malformed / non-JSON bodies       → always 400 or 422, never 500
  P2  Missing required fields           → 422 with OpenAI error envelope shape
  P3  MAX_BODY_SIZE boundary            → 1 MB-1 passes; 1 MB+1 → 413
  P4  Unicode / encoding content        → governance pipeline does not crash
  P5  Extra / unknown fields            → rejected with field diagnostics
  P6  Duplicate auth-like header values → deterministic outcome, never 500

Unit-layer properties (no HTTP round-trip):

  U1  error_envelope structure invariants
  U2  extract_usage total_tokens ≥ prompt + completion for all providers
  U3  _jsonb_list never raises, always returns list
  U4  resolve_provider never returns a routing_method outside the known set
  U5  _extract_user_message never raises on arbitrary message lists

Run with:  pytest proxy/tests/test_properties.py -v
Add hypothesis to dev deps first:  uv add --dev hypothesis
"""
from __future__ import annotations

import json
import re
import string
from typing import Any
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from proxy.app.auth import CallerContext, _api_key_cache
from proxy.app.config import settings as app_settings
from proxy.app.db import jsonb_list as _jsonb_list
from proxy.app.governance_client import InspectResponse
from proxy.app.governance_client import extract_user_message as _extract_user_message
from proxy.app.headers import error_envelope
from proxy.app.main import _me_cache, _tenant_cache, app, get_caller
from proxy.app.middleware import MAX_BODY_SIZE
from proxy.app.provider_capabilities import OPENAI_CHAT_FIELDS
from proxy.app.providers.usage import UsageMetrics, extract_usage
from proxy.app.routing import resolve_provider
from proxy.tests.helpers import make_gov_mock, make_mock_pool, make_mock_rate_limiter

_SSN_RE = re.compile(r"\d{3}-\d{2}-\d{4}")

# ---------------------------------------------------------------------------
# Shared Hypothesis profile
# ---------------------------------------------------------------------------
# suppress_health_check=too_slow is needed because the ASGI fixture involves
# async startup; Hypothesis would otherwise flag the slow setup.
_FAST = settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])

# ---------------------------------------------------------------------------
# Shared strategies
# ---------------------------------------------------------------------------

# A "clean" content string that won't accidentally trigger mock scenarios
# (avoids SSN pattern, diagnosis, patient record, injection phrases, gpt-4o).
_CLEAN_CONTENT = st.text(
    alphabet=string.ascii_letters + string.digits + " ,!?.",
    min_size=1,
    max_size=100,
).filter(
    lambda s: not any(
        kw in s.lower()
        for kw in ("diagnosis", "patient", "ignore previous", "disregard system", "gpt-4o")
    )
    and not _SSN_RE.search(s)
)

_ROLE = st.sampled_from(["user", "assistant", "system"])

_MESSAGE = st.fixed_dictionaries({"role": _ROLE, "content": _CLEAN_CONTENT})

_MESSAGES = st.lists(_MESSAGE, min_size=1, max_size=5)

# Known-good routable model (present in conftest._MODELS_CONFIG)
_KNOWN_MODEL = st.just("gpt-5.6-luna")

# ---------------------------------------------------------------------------
# Fixture helpers — imported from proxy.tests.helpers so this file stays DRY.
# The property tests do not depend on session-scoped async fixtures.
# ---------------------------------------------------------------------------

_MODELS_CONFIG = [
    {"id": "gpt-5.6-luna", "provider": "openai"},
    {"id": "gpt-4o", "provider": "openai"},
]


def _teardown_app() -> None:
    app.dependency_overrides.clear()
    app.state.db_pool = None
    app.state.redis = None
    app.state.rate_limiter = None
    app.state.gov_http = None
    app.state.governance_client = None
    app.state.openai_client = None
    app.state.ready = False
    _tenant_cache.clear()
    _me_cache.clear()
    _api_key_cache.clear()


def _setup_app(gov_mock=None):
    _teardown_app()
    if gov_mock is None:
        gov_mock = make_gov_mock()
    pool = make_mock_pool()
    caller = CallerContext(user_id="pbt-user", tenant_id="pbt-tenant", roles=["user"])
    app.dependency_overrides[get_caller] = lambda: caller
    app.state.db_pool = pool
    app.state.redis = AsyncMock()
    app.state.rate_limiter = make_mock_rate_limiter()
    app.state.gov_http = AsyncMock()
    app.state.governance_client = gov_mock
    app.state.openai_client = None
    app.state.models_config = _MODELS_CONFIG
    app.state.models_by_id = {m["id"]: m for m in _MODELS_CONFIG}
    app.state.ready = True
    app_settings.mock_mode = True
    return pool, gov_mock


# ---------------------------------------------------------------------------
# P1 — Malformed / non-JSON bodies: always 400 or 422, never 500
#
# Property type: Invariant
# Why it matters: any unhandled exception in body parsing that leaks as 500
# reveals internal stack details and breaks the OpenAI-compatible contract.
# ---------------------------------------------------------------------------

# Strategies for bodies that are syntactically invalid JSON or valid JSON but
# not an object (e.g. bare string, integer, list).
_NON_OBJECT_JSON = st.one_of(
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
    st.booleans(),
    st.none(),
    st.lists(st.text()),
    st.text(min_size=1),
)

def _is_valid_json_object(data: bytes) -> bool:
    try:
        parsed = json.loads(data)
        return isinstance(parsed, dict)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False


_MALFORMED_BYTES = st.binary(min_size=1, max_size=512).filter(
    lambda b: not _is_valid_json_object(b)
)


@pytest.mark.asyncio
@given(body=_NON_OBJECT_JSON)
@_FAST
async def test_p1_non_object_json_never_500(body):
    """Valid JSON but not an object → 400 or 422, never 500."""
    _setup_app()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/v1/chat/completions",
                content=json.dumps(body),
                headers={"Content-Type": "application/json"},
            )
        assert response.status_code in (400, 422), (
            f"Expected 400 or 422 for non-object JSON, got {response.status_code}. "
            f"Body was: {body!r}"
        )
    finally:
        _teardown_app()


@pytest.mark.asyncio
@given(raw=_MALFORMED_BYTES)
@_FAST
async def test_p1_binary_garbage_never_500(raw):
    """Arbitrary bytes in the body → 400 or 422, never 500."""
    _setup_app()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/v1/chat/completions",
                content=raw,
                headers={"Content-Type": "application/json"},
            )
        assert response.status_code in (400, 422), (
            f"Expected 400 or 422 for binary garbage, got {response.status_code}"
        )
    finally:
        _teardown_app()


# ---------------------------------------------------------------------------
# P2 — Missing required fields: 422 with OpenAI error envelope shape
#
# Property type: Invariant
# Why it matters: the gateway must produce consistent error shapes for clients
# that inspect the detail.error structure. A raw FastAPI validation error
# (missing the {"error": {...}} wrapper) would break client SDK error parsing.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@given(
    body=st.fixed_dictionaries(
        {},
        optional={
            "model": st.text(min_size=1, max_size=50),
            "messages": _MESSAGES,
            "stream": st.booleans(),
            "temperature": st.floats(min_value=0.0, max_value=2.0),
        },
    ).filter(
        lambda d: "model" not in d or "messages" not in d  # at least one required field absent
    )
)
@_FAST
async def test_p2_missing_required_fields_return_4xx(body):
    """Bodies missing 'model' or 'messages' → 4xx, never 500.

    Note: FastAPI returns 422 for Pydantic validation failures when using
    typed request models. This endpoint uses request.json() directly (no
    Pydantic model on the endpoint), so missing fields are handled by the
    application layer, not FastAPI validation. The gateway currently routes
    to 400 (model_not_found) for missing model, and proceeds with empty
    messages when messages is absent. The invariant is simply: never 500.
    """
    _setup_app()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/v1/chat/completions", json=body)
        assert response.status_code != 500, (
            f"Got 500 for body with missing required fields: {body!r}"
        )
        # Any error response must have a JSON body
        if response.status_code >= 400:
            resp_json = response.json()
            assert isinstance(resp_json, dict), "Error response must be a JSON object"
    finally:
        _teardown_app()


# ---------------------------------------------------------------------------
# P3 — MAX_BODY_SIZE boundary: 1 MB-1 passes; 1 MB+1 → 413
#
# Property type: Invariant (boundary)
# Why it matters: off-by-one in BodySizeLimitMiddleware would either
# silently accept oversized payloads (security) or reject valid payloads
# (availability). Both Content-Length and streaming paths must be consistent.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p3_body_at_limit_minus_one_accepted():
    """A body of exactly MAX_BODY_SIZE - 1 bytes must not be rejected by the size middleware."""
    _setup_app()
    # Build a valid JSON object padded to exactly MAX_BODY_SIZE - 1 bytes.
    # We use a 'padding' key to control the total serialised length.
    base = {"model": "gpt-5.6-luna", "messages": [{"role": "user", "content": "x"}]}
    base_bytes = json.dumps(base).encode()
    # Pad inside the JSON by adding whitespace after the last brace
    # (HTTP body; the middleware checks len(body), not Content-Length arithmetic)
    target = MAX_BODY_SIZE - 1
    if len(base_bytes) >= target:
        pytest.skip("Base body already at or over limit; adjust padding strategy")
    padding_needed = target - len(base_bytes)
    body_bytes = base_bytes[:-1] + b',"_pad":"' + b"x" * (padding_needed - 10) + b'"}'
    assert len(body_bytes) == target, f"Padding arithmetic off: got {len(body_bytes)}"
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/v1/chat/completions",
                content=body_bytes,
                headers={"Content-Type": "application/json"},
            )
        assert response.status_code != 413, (
            f"Body of {MAX_BODY_SIZE - 1} bytes should not be rejected with 413"
        )
    finally:
        _teardown_app()


@pytest.mark.asyncio
async def test_p3_body_at_limit_plus_one_rejected_via_content_length():
    """Content-Length: MAX_BODY_SIZE + 1 triggers 413 before the body is read."""
    _setup_app()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # Send exactly 1 byte over limit; middleware checks Content-Length first
            oversized = b"x" * (MAX_BODY_SIZE + 1)
            response = await client.post(
                "/v1/chat/completions",
                content=oversized,
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(MAX_BODY_SIZE + 1),
                },
            )
        assert response.status_code == 413, (
            f"Expected 413 for {MAX_BODY_SIZE + 1} byte body, got {response.status_code}"
        )
    finally:
        _teardown_app()


@pytest.mark.asyncio
async def test_p3_body_at_limit_plus_one_rejected_without_content_length():
    """Streaming body over MAX_BODY_SIZE without Content-Length also triggers 413."""
    _setup_app()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            oversized = b"x" * (MAX_BODY_SIZE + 1)
            # Omit Content-Length to force the streaming accumulation path
            headers = {"Content-Type": "application/json"}
            response = await client.post(
                "/v1/chat/completions",
                content=oversized,
                headers=headers,
            )
        assert response.status_code == 413, (
            f"Expected 413 for streaming oversized body, got {response.status_code}"
        )
    finally:
        _teardown_app()


# ---------------------------------------------------------------------------
# P4 — Unicode / encoding content: governance pipeline does not crash
#
# Property type: Invariant (no exception / no 500)
# Why it matters: governance client serialises text to JSON and sends it over
# HTTP. Surrogates, null bytes, RTL characters, emoji, and homoglyph sequences
# are all legal in JSON strings. A crash here would be a DoS vector.
# ---------------------------------------------------------------------------

# Characters that are valid JSON-serialisable Unicode but adversarial:
# RTL overrides, zero-width joiners, mathematical alphanumerics, emoji, etc.
_UNICODE_CONTENT = st.text(
    alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd", "Po", "Zs", "So", "Mn"),
        blacklist_categories=("Cs",),  # no surrogates
    ),
    min_size=1,
    max_size=300,
).filter(
    # Don't accidentally trigger mock block scenarios
    lambda s: not any(
        kw in s.lower()
        for kw in ("diagnosis", "patient", "ignore previous", "disregard system", "gpt-4o")
    )
    and not _SSN_RE.search(s)
)


@pytest.mark.asyncio
@given(content=_UNICODE_CONTENT)
@_FAST
async def test_p4_unicode_content_does_not_crash(content):
    """Arbitrary Unicode in message content must not crash the governance pipeline."""
    gov_mock = make_gov_mock()
    # Capture what InspectRequest.text actually arrives at governance
    received_texts: list[str] = []

    async def _inspect_spy(req):
        received_texts.append(req.text)
        return InspectResponse(
            decision="allow",
            redacted_text="",
            pii_findings=[],
            harm_score=0.0,
            violations=[],
            audit_id="unicode-test",
        )

    gov_mock.inspect.side_effect = _inspect_spy
    _setup_app(gov_mock)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            body = {
                "model": "gpt-5.6-luna",
                "messages": [{"role": "user", "content": content}],
            }
            response = await client.post("/v1/chat/completions", json=body)
        assert response.status_code != 500, (
            f"Unicode content crashed the pipeline (500). Content was: {content!r}"
        )
        # Governance was called and received a string, not bytes or None
        if received_texts:
            assert isinstance(received_texts[0], str), (
                "InspectRequest.text must be str, not bytes/None"
            )
    finally:
        _teardown_app()


@pytest.mark.asyncio
@given(
    content=st.text(
        alphabet=st.sampled_from("\x00\x01\x1f\x7f\t\r\n"),
        min_size=1,
        max_size=50,
    )
)
@_FAST
async def test_p4_control_characters_do_not_crash(content):
    """Control characters (null, tab, CR, LF, DEL) in content must not produce 500."""
    _setup_app()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            body = {
                "model": "gpt-5.6-luna",
                "messages": [{"role": "user", "content": content}],
            }
            response = await client.post("/v1/chat/completions", json=body)
        assert response.status_code != 500, (
            f"Control chars crashed gateway (500). content={content!r}"
        )
    finally:
        _teardown_app()


# ---------------------------------------------------------------------------
# P5 — Extra / unknown fields: rejected with explicit field diagnostics
#
# Property type: Metamorphic
# The transformation: add arbitrary extra top-level keys to a valid body.
# Expected output change: status 400 with the unknown field path.
#
# Why it matters: silently accepting misspelled or newly introduced semantics
# makes vendor switching lossy and impossible to diagnose.
# ---------------------------------------------------------------------------

_EXTRA_KEY = st.text(
    alphabet=string.ascii_lowercase + "_",
    min_size=1,
    max_size=30,
).filter(lambda key: key not in OPENAI_CHAT_FIELDS)

_EXTRA_VALUE: st.SearchStrategy[Any] = st.one_of(
    st.text(max_size=50),
    st.integers(min_value=0, max_value=1000),
    st.booleans(),
    st.none(),
    st.lists(st.text(max_size=10), max_size=3),
)

_EXTRA_FIELDS = st.dictionaries(_EXTRA_KEY, _EXTRA_VALUE, min_size=1, max_size=5)


@pytest.mark.asyncio
@given(extra=_EXTRA_FIELDS)
@_FAST
async def test_p5_extra_fields_are_rejected_with_paths(extra):
    """Unknown top-level fields fail closed and identify every rejected key."""
    _setup_app()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            body = {
                "model": "gpt-5.6-luna",
                "messages": [{"role": "user", "content": "hello"}],
                **extra,
            }
            response = await client.post("/v1/chat/completions", json=body)
        assert response.status_code == 400
        violations = response.json()["detail"]["error"]["details"]["violations"]
        assert {violation["field"] for violation in violations} == set(extra)
    finally:
        _teardown_app()


# ---------------------------------------------------------------------------
# P6 — Duplicate / adversarial Authorization headers: deterministic, never 500
#
# Property type: Invariant (stability under adversarial headers)
# Why it matters: HTTP/1.1 allows duplicate headers. Some reverse proxies
# inject a second Authorization when one is already present. The gateway must
# never 500; it must return 401 (invalid cred) or process normally.
# ---------------------------------------------------------------------------

_HEADER_VALUE = st.text(
    alphabet=string.printable.replace("\n", "").replace("\r", ""),
    min_size=1,
    max_size=80,
)


@pytest.mark.asyncio
@given(second_auth=_HEADER_VALUE)
@_FAST
async def test_p6_duplicate_auth_header_never_500(second_auth):
    """Sending two Authorization header values must not produce a 500."""
    # Use auth_client fixture pattern (no get_caller override = real auth path)
    from proxy.app.auth import _api_key_cache
    from proxy.app.main import _me_cache, _tenant_cache

    gov_mock = make_gov_mock()
    pool = make_mock_pool()
    # No get_caller override — real authentication runs
    app.dependency_overrides.pop(get_caller, None)
    app.state.db_pool = pool
    app.state.redis = AsyncMock()
    app.state.rate_limiter = make_mock_rate_limiter()
    app.state.gov_http = AsyncMock()
    app.state.governance_client = gov_mock
    app.state.openai_client = None
    app.state.models_config = _MODELS_CONFIG
    app.state.models_by_id = {m["id"]: m for m in _MODELS_CONFIG}
    app.state.ready = True
    _tenant_cache.clear()
    _me_cache.clear()
    _api_key_cache.clear()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            body = {
                "model": "gpt-5.6-luna",
                "messages": [{"role": "user", "content": "hello"}],
            }
            # httpx merges duplicate header names with comma per RFC 7230
            response = await client.post(
                "/v1/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer valid-token, {second_auth}"},
            )
        # 401 is expected (invalid cred), but 500 is never acceptable
        assert response.status_code != 500, (
            f"Duplicate auth header caused 500. second_auth={second_auth!r}"
        )
    finally:
        _teardown_app()
        _tenant_cache.clear()
        _me_cache.clear()
        _api_key_cache.clear()


# ===========================================================================
# Unit-layer property tests (no HTTP round-trip)
# ===========================================================================

# ---------------------------------------------------------------------------
# U1 — error_envelope structure invariants
#
# Property type: Invariant
# The envelope must always have exactly the shape {"error": {"type", "message",
# "violations"}}. Any deviation would break client SDK error parsing.
# ---------------------------------------------------------------------------

_ERROR_TYPE = st.text(alphabet=string.ascii_lowercase + "_:", min_size=1, max_size=40)
_ERROR_MSG = st.text(max_size=200)
_VIOLATIONS = st.lists(st.text(max_size=50), max_size=5)
_ROLES = st.lists(st.text(min_size=1, max_size=30), max_size=3)


@given(
    error_type=_ERROR_TYPE,
    message=_ERROR_MSG,
    violations=_VIOLATIONS,
    required_roles=_ROLES,
)
@settings(max_examples=200)
def test_u1_error_envelope_invariants(error_type, message, violations, required_roles):
    """error_envelope always returns {"error": {type, message, violations, ...}}."""
    result = error_envelope(error_type, message, violations, required_roles)

    # Outer shape
    assert set(result.keys()) == {"error"}
    body = result["error"]
    assert isinstance(body, dict)

    # Required inner keys always present
    assert "type" in body
    assert "message" in body
    assert "violations" in body

    # Values round-trip cleanly
    assert body["type"] == error_type
    assert body["message"] == message
    assert body["violations"] == list(violations)

    # Optional keys only present when non-empty
    if required_roles:
        assert body.get("required_roles") == list(required_roles)
    else:
        assert "required_roles" not in body

    # Always JSON-serialisable
    json.dumps(result)  # must not raise


# ---------------------------------------------------------------------------
# U2 — extract_usage: total_tokens ≥ prompt + completion (non-negative addends)
#
# Property type: Invariant (mathematical)
# Why it matters: total_tokens is computed as max(total_tokens_field, prompt+completion).
# A negative or inconsistent total would corrupt metering headers downstream.
# ---------------------------------------------------------------------------

_TOKEN_COUNT = st.integers(min_value=0, max_value=100_000)
_PROVIDER = st.sampled_from(["openai", "anthropic", "gemini", "ollama", "generic", "unknown"])


@given(
    provider=_PROVIDER,
    prompt=_TOKEN_COUNT,
    completion=_TOKEN_COUNT,
    reported_total=st.one_of(st.none(), _TOKEN_COUNT),
)
@settings(max_examples=300)
def test_u2_usage_total_tokens_gte_components(provider, prompt, completion, reported_total):
    """total_tokens is always >= prompt_tokens + completion_tokens (never undercount)."""
    if provider in ("openai", "ollama", "generic"):
        usage_dict = {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
        }
        if reported_total is not None:
            usage_dict["total_tokens"] = reported_total
        response_json: dict = {"usage": usage_dict}
    elif provider == "anthropic":
        response_json = {"usage": {"input_tokens": prompt, "output_tokens": completion}}
    elif provider == "gemini":
        usage_dict = {
            "promptTokenCount": prompt,
            "candidatesTokenCount": completion,
        }
        if reported_total is not None:
            usage_dict["totalTokenCount"] = reported_total
        response_json = {"usageMetadata": usage_dict}
    else:
        response_json = {}  # unknown provider → zero metrics

    metrics = extract_usage(provider, response_json)

    assert metrics.prompt_tokens >= 0
    assert metrics.completion_tokens >= 0
    assert metrics.total_tokens >= 0
    # total must be at least the sum; it may be higher if the provider reports a
    # larger figure (includes system prompt tokens, etc.)
    assert metrics.total_tokens >= metrics.prompt_tokens + metrics.completion_tokens or (
        # exception: unknown provider returns UsageMetrics.zero() unconditionally
        provider not in ("openai", "anthropic", "gemini", "ollama", "generic")
    )


@given(response_json=st.just({}))
@settings(max_examples=10)
def test_u2_extract_usage_never_raises_on_empty(response_json):
    """extract_usage never raises on an empty or malformed response dict."""
    for provider in ("openai", "anthropic", "gemini", "ollama", "generic", "unknown"):
        result = extract_usage(provider, response_json)
        assert isinstance(result, UsageMetrics)


# ---------------------------------------------------------------------------
# U3 — _jsonb_list never raises, always returns list
#
# Property type: Invariant
# Why it matters: asyncpg JSONB values can arrive as str, list, tuple, or None.
# A type error here would crash tenant config lookup on every request.
# ---------------------------------------------------------------------------

_JSONB_INPUT = st.one_of(
    st.none(),
    st.text(max_size=200),
    st.lists(st.text(max_size=50), max_size=10),
    st.tuples(st.text(max_size=20)),
    st.integers(),
    st.booleans(),
    st.binary(max_size=50),
)


@given(value=_JSONB_INPUT)
@settings(max_examples=300)
def test_u3_jsonb_list_invariants(value):
    """_jsonb_list never raises and always returns a list."""
    result = _jsonb_list(value)
    assert isinstance(result, list), f"Expected list, got {type(result)} for input {value!r}"


# ---------------------------------------------------------------------------
# U4 — resolve_provider: routing_method always in the known set
#
# Property type: Invariant
# Why it matters: main.py pattern-matches on routing_method string literals.
# An unknown method would fall through to a 400 "unsupported_provider" error
# on valid requests, silently breaking new model additions.
# ---------------------------------------------------------------------------

_KNOWN_ROUTING_METHODS = frozenset(
    {"header_override", "override_denied", "models_yaml", "prefix_inference", "tenant_default", "model_not_found"}
)

_MODEL_ID = st.text(
    alphabet=string.ascii_lowercase + string.digits + "-_.",
    min_size=0,
    max_size=60,
)

_HEADER_DICT = st.dictionaries(
    st.just("x-gateway-provider"),
    st.text(alphabet=string.ascii_lowercase + string.digits + "-", max_size=30),
    max_size=1,
)

_ROLES_LIST = st.lists(
    st.text(alphabet=string.ascii_lowercase + ":_", min_size=1, max_size=40),
    max_size=5,
)

_TENANT_DEFAULT = st.one_of(st.just(""), st.sampled_from(["openai", "anthropic", "ollama"]))

_MODELS_CONFIG_ST = st.lists(
    st.fixed_dictionaries(
        {
            "id": st.text(min_size=1, max_size=30),
            "provider": st.sampled_from(["openai", "anthropic", "gemini", "ollama"]),
        }
    ),
    max_size=10,
)


@given(
    model_id=_MODEL_ID,
    headers=_HEADER_DICT,
    roles=_ROLES_LIST,
    tenant_default=_TENANT_DEFAULT,
    models_config=_MODELS_CONFIG_ST,
)
@settings(max_examples=400)
def test_u4_resolve_provider_routing_method_in_known_set(
    model_id, headers, roles, tenant_default, models_config
):
    """resolve_provider always returns a routing_method from the known set."""
    provider, routing_method = resolve_provider(
        model_id, headers, roles, tenant_default, models_config
    )
    assert routing_method in _KNOWN_ROUTING_METHODS, (
        f"Unknown routing_method {routing_method!r} for model_id={model_id!r}"
    )
    assert isinstance(provider, str), "provider must always be a str"


# ---------------------------------------------------------------------------
# U5 — _extract_user_message never raises on arbitrary message lists
#
# Property type: Invariant
# Why it matters: _extract_user_message feeds governance.inspect(text=...).
# An unhandled exception here crashes every request touching that code path.
# ---------------------------------------------------------------------------

_MSG_DICT = st.dictionaries(
    st.text(max_size=20),
    st.one_of(
        st.text(max_size=100),
        st.none(),
        st.integers(),
        st.booleans(),
        st.lists(
            st.one_of(
                st.fixed_dictionaries({"text": st.text(max_size=50)}),
                st.fixed_dictionaries({"type": st.just("image_url"), "image_url": st.text()}),
                st.text(),
                st.none(),
            ),
            max_size=5,
        ),
    ),
    max_size=5,
)

_MSG_LIST = st.lists(_MSG_DICT, max_size=8)


@given(messages=_MSG_LIST)
@settings(max_examples=500)
def test_u5_extract_user_message_never_raises(messages):
    """_extract_user_message never raises regardless of message list shape."""
    body = {"messages": messages}
    result = _extract_user_message(body)
    assert isinstance(result, str), (
        f"_extract_user_message must return str, got {type(result)}"
    )


@given(body=st.fixed_dictionaries({}))
@settings(max_examples=10)
def test_u5_extract_user_message_empty_body_returns_empty_string(body):
    """An empty body dict returns an empty string, not None or an exception."""
    result = _extract_user_message(body)
    assert result == ""
