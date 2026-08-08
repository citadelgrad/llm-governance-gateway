from __future__ import annotations

from proxy.app.provider_capabilities import (
    GEMINI_CHAT_TRANSLATION_FIELDS,
    PROVIDER_CAPABILITIES,
    unsupported_chat_fields,
)
from proxy.app.providers._gemini_common import DEVELOPER_API_DIALECT, VERTEX_DIALECT


def test_gemini_vertex_capabilities_derive_from_gemini_entry():
    gemini_vertex = PROVIDER_CAPABILITIES["gemini-vertex"]

    assert gemini_vertex.chat_translation_fields == GEMINI_CHAT_TRANSLATION_FIELDS
    assert gemini_vertex.extra_finish_reasons == VERTEX_DIALECT.extra_finish_reasons


def test_unsupported_chat_fields_no_keyerror_for_gemini_vertex():
    body = {"messages": [], "model": "gemini-3.1-pro-vertex", "user": "abc"}

    assert unsupported_chat_fields("gemini-vertex", body) == ["user"]


def test_gemini_capabilities_unchanged_by_gemini_vertex_addition():
    gemini = PROVIDER_CAPABILITIES["gemini"]

    assert gemini.chat_translation_fields == GEMINI_CHAT_TRANSLATION_FIELDS
    assert gemini.extra_finish_reasons == DEVELOPER_API_DIALECT.extra_finish_reasons

    body = {"messages": [], "model": "gemini-3.1-flash", "user": "abc"}
    assert unsupported_chat_fields("gemini", body) == ["user"]
