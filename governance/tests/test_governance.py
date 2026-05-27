import asyncio
import hmac
import hashlib

import pytest

from app.pseudonym import _compute
from app.audit import uuid7
from app.context import PipelineContext


def test_pseudonym_deterministic():
    key = "test-key"
    p1 = _compute(key, "tenant-a", "user-1", 0)
    p2 = _compute(key, "tenant-a", "user-1", 0)
    assert p1 == p2

def test_pseudonym_cross_tenant_isolation():
    key = "test-key"
    p_a = _compute(key, "tenant-a", "user-1", 0)
    p_b = _compute(key, "tenant-b", "user-1", 0)
    assert p_a != p_b

def test_pseudonym_rotation():
    key = "test-key"
    p0 = _compute(key, "tenant-a", "user-1", 0)
    p1 = _compute(key, "tenant-a", "user-1", 1)
    assert p0 != p1

def test_uuid7_version_nibble():
    u = uuid7()
    # Version nibble at bits 76-79 (the 13th hex digit of the UUID string)
    hex_str = u.hex
    version_nibble = int(hex_str[12], 16)
    assert version_nibble == 7

def test_pipeline_context_defaults():
    ctx = PipelineContext(
        text="hello",
        tenant_id="t1",
        user_id="u1",
        model_id="gpt-4o",
        routing_method="direct",
    )
    assert ctx.decision == "allow"
    assert ctx.phase == "request"
    assert ctx.pii_findings == []
    assert ctx.violations == []
