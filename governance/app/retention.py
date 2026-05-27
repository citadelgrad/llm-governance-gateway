from __future__ import annotations

import re
import sys
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_PARTITION_NAME_RE = re.compile(r"^audit_log_\d{4}_(?:0[1-9]|1[0-2])$")


def _next_month(d: date) -> date:
    if d.month == 12:
        return date(d.year + 1, 1, 1)
    return date(d.year, d.month + 1, 1)


def _partition_name(d: date) -> str:
    return f"audit_log_{d.year:04d}_{d.month:02d}"


def _safe_partition_name(name: str) -> str:
    """Validate partition name before DDL interpolation (Postgres DDL can't use bind params)."""
    if not _PARTITION_NAME_RE.fullmatch(name):
        raise ValueError(f"Refusing DDL: unexpected partition name {name!r}")
    return name


async def create_next_partition(session: AsyncSession) -> None:
    today = date.today()
    next_month = _next_month(today)
    end_month = _next_month(next_month)
    table_name = _partition_name(next_month)

    exists = await session.execute(
        text("SELECT 1 FROM information_schema.tables WHERE table_name = :name"),
        {"name": table_name},
    )
    if exists.scalar():
        return

    try:
        safe_name = _safe_partition_name(table_name)
        await session.execute(text(f"""
            CREATE TABLE {safe_name} PARTITION OF audit_log
                FOR VALUES FROM ('{next_month}') TO ('{end_month}')
        """))
        await session.commit()
    except Exception as exc:
        print(f"[retention] create_next_partition failed: {exc}", file=sys.stderr)
        await session.rollback()


async def advance_partitions(session: AsyncSession) -> None:
    result = await session.execute(
        text("""
            SELECT table_name, detached_at, dumped_at, verified_at, dropped_at
            FROM partition_archive_state
            WHERE dropped_at IS NULL
            ORDER BY month
        """)
    )
    rows = result.fetchall()

    for row in rows:
        table_name = _safe_partition_name(row.table_name)
        now = datetime.now(timezone.utc)

        if row.detached_at is None:
            # Detach the partition
            try:
                await session.execute(
                    text(f"ALTER TABLE audit_log DETACH PARTITION {table_name} CONCURRENTLY")
                )
                await session.execute(
                    text("UPDATE partition_archive_state SET detached_at = :now WHERE table_name = :name"),
                    {"now": now, "name": table_name},
                )
                await session.commit()
            except Exception as exc:
                print(f"[retention] detach {table_name} failed: {exc}", file=sys.stderr)
                await session.rollback()

        elif row.dumped_at is None:
            # Dump to S3 (stubbed — real impl calls s3 copy)
            print(f"[retention] {table_name} awaiting dump (detached {row.detached_at})", file=sys.stderr)

        elif row.verified_at is None:
            # Verify dump integrity (stubbed)
            try:
                await session.execute(
                    text("UPDATE partition_archive_state SET verified_at = :now WHERE table_name = :name"),
                    {"now": now, "name": table_name},
                )
                await session.commit()
            except Exception as exc:
                print(f"[retention] verify {table_name} failed: {exc}", file=sys.stderr)
                await session.rollback()

        elif row.dropped_at is None:
            # Drop the partition table
            try:
                await session.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
                await session.execute(
                    text("UPDATE partition_archive_state SET dropped_at = :now WHERE table_name = :name"),
                    {"now": now, "name": table_name},
                )
                await session.commit()
            except Exception as exc:
                print(f"[retention] drop {table_name} failed: {exc}", file=sys.stderr)
                await session.rollback()


async def count_stuck_partitions(session: AsyncSession) -> int:
    """Count partitions detached >48h without a dump — used by /health."""
    result = await session.execute(
        text("""
            SELECT COUNT(*) FROM partition_archive_state
            WHERE detached_at < NOW() - INTERVAL '48 hours'
            AND dumped_at IS NULL
        """)
    )
    return result.scalar() or 0
