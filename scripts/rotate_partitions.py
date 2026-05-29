#!/usr/bin/env python3
"""Standalone script to rotate audit_log partitions. Run nightly via Fly cron machine."""
import asyncio
import os
import sys

# Add the repo root to path so governance package is importable when running standalone
_repo_root = str(__file__).split("/scripts/")[0]
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from governance.app.retention import create_next_partition, advance_partitions

_raw_url = os.environ.get("DATABASE_URL")
if not _raw_url:
    print("[rotate_partitions] ERROR: DATABASE_URL environment variable is not set", file=sys.stderr)
    sys.exit(1)

# Normalise scheme to postgresql+asyncpg://
if _raw_url.startswith("postgres://"):
    DATABASE_URL = _raw_url.replace("postgres://", "postgresql+asyncpg://", 1)
elif _raw_url.startswith("postgresql://"):
    DATABASE_URL = _raw_url.replace("postgresql://", "postgresql+asyncpg://", 1)
else:
    DATABASE_URL = _raw_url


async def main() -> None:
    engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        print("[rotate_partitions] Creating next partition...")
        await create_next_partition(session)

        print("[rotate_partitions] Advancing partition archive state...")
        await advance_partitions(session)

        print("[rotate_partitions] Done.")

    await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        print(f"[rotate_partitions] FATAL: {exc}", file=sys.stderr)
        sys.exit(1)
