"""Serve policies/mcp/authz.rego's `entitlements` value as an OPA bundle.

Reads the Rego source directly rather than querying a live OPA instance —
the ingress `opa` and the MCP `opa-sidecar` are two separate processes that
must not share a data plane (docs/auth-architecture.md, "Policy enforcement:
two evaluation points, two separate OPA processes"). This module extracts
the `entitlements := {...}` literal with a targeted regex + brace-match, not
a general Rego parser: it depends on that value staying a pure JSON-compatible
literal (strings, null, objects, arrays, trailing commas), which is true today
and is the narrow contract this task was scoped to.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import json
import re
import tarfile
import unicodedata

ENTITLEMENTS_MARKER = re.compile(r"^\s*entitlements\s*:=\s*", re.MULTILINE)


class EntitlementsError(Exception):
    """Raised when authz.rego cannot be read or its entitlements value cannot be extracted."""


def _extract_entitlements(rego_text: str) -> dict:
    match = ENTITLEMENTS_MARKER.search(rego_text)
    if not match:
        raise EntitlementsError("no 'entitlements :=' assignment found in authz.rego")

    start = rego_text.find("{", match.end())
    if start == -1:
        raise EntitlementsError("no opening '{' found after 'entitlements :='")

    depth = 0
    in_string = False
    escape = False
    end = None
    for i in range(start, len(rego_text)):
        ch = rego_text[i]
        if in_string:
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    if end is None:
        raise EntitlementsError("unbalanced braces in entitlements value")

    snippet = rego_text[start:end]
    snippet = re.sub(r",\s*([}\]])", r"\1", snippet)

    try:
        return json.loads(snippet)
    except json.JSONDecodeError as exc:
        raise EntitlementsError(f"entitlements value is not valid JSON after cleanup: {exc}") from exc


def _normalize_nfc(value):
    """Recursively NFC-normalizes every string in an entitlements value.

    The MCP Reverse Proxy NFC-normalizes input.context.resource before OPA
    ever sees it (mcpproxy/app/main.py's _normalize_context); if a
    resource_pattern here were authored in a non-NFC form (e.g. NFD), it
    would silently fail to glob-match an equivalent, differently-encoded
    resource string. Normalizing here keeps both sides of that comparison
    in the same normal form regardless of how authz.rego was typed.
    """
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, dict):
        return {k: _normalize_nfc(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_nfc(v) for v in value]
    return value


def _build_bundle(entitlements: dict, revision: str) -> bytes:
    data = json.dumps({"mcp": {"authz": {"entitlements": entitlements}}}, sort_keys=True).encode()
    manifest = json.dumps(
        {"revision": revision, "roots": ["mcp/authz/entitlements"]}, sort_keys=True
    ).encode()

    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w") as tar:
        for name, content in ((".manifest", manifest), ("data.json", data)):
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            info.mtime = 0
            tar.addfile(info, io.BytesIO(content))
    tar_bytes = tar_buf.getvalue()

    gz_buf = io.BytesIO()
    with gzip.GzipFile(fileobj=gz_buf, mode="wb", mtime=0) as gz:
        gz.write(tar_bytes)
    return gz_buf.getvalue()


def _read_and_build(rego_path: str) -> bytes:
    try:
        with open(rego_path, "rb") as f:
            raw = f.read()
    except OSError as exc:
        raise EntitlementsError(f"cannot read {rego_path}: {exc}") from exc

    try:
        text = raw.decode()
    except UnicodeDecodeError as exc:
        raise EntitlementsError(f"{rego_path} is not valid UTF-8: {exc}") from exc

    entitlements = _normalize_nfc(_extract_entitlements(text))
    revision = hashlib.sha256(raw).hexdigest()
    return _build_bundle(entitlements, revision)


async def get_bundle(rego_path: str) -> bytes:
    return await asyncio.to_thread(_read_and_build, rego_path)


__all__ = ["EntitlementsError", "get_bundle"]
