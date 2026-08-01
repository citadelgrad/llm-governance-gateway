from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.retention as retention


class _FrozenDateTime:
    @classmethod
    def now(cls, tz):
        assert tz is UTC
        return datetime(2026, 8, 1, 0, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_ensure_write_partitions_creates_current_and_next_month(monkeypatch):
    monkeypatch.setattr(retention, "datetime", _FrozenDateTime)
    missing_current = MagicMock()
    missing_current.scalar.return_value = None
    missing_next = MagicMock()
    missing_next.scalar.return_value = None
    session = MagicMock()
    session.execute = AsyncMock(side_effect=[missing_current, None, missing_next, None])
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    await retention.ensure_write_partitions(session)

    statements = [str(call.args[0]) for call in session.execute.await_args_list]
    assert "CREATE TABLE audit_log_2026_08 PARTITION OF audit_log" in statements[1]
    assert "FOR VALUES FROM ('2026-08-01') TO ('2026-09-01')" in statements[1]
    assert "CREATE TABLE audit_log_2026_09 PARTITION OF audit_log" in statements[3]
    assert "FOR VALUES FROM ('2026-09-01') TO ('2026-10-01')" in statements[3]
    assert session.commit.await_count == 2
    session.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_write_partitions_fails_closed_on_ddl_error(monkeypatch):
    monkeypatch.setattr(retention, "datetime", _FrozenDateTime)
    missing = MagicMock()
    missing.scalar.return_value = None
    session = MagicMock()
    session.execute = AsyncMock(side_effect=[missing, RuntimeError("ddl failed")])
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    with pytest.raises(RuntimeError, match="ddl failed"):
        await retention.ensure_write_partitions(session)

    session.rollback.assert_awaited_once()
