#!/usr/bin/env python3
"""Standalone script to rotate audit_log partitions. Run nightly via Fly cron machine."""
import asyncio
import os
import sys
from pathlib import Path

# Add the repo root to path so governance package is importable when running standalone.
# Using pathlib.Path.resolve() is robust to symlinks and any deployment path.
_repo_root = str(Path(__file__).resolve().parent.parent)
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from governance.app.retention import advance_partitions, ensure_write_partitions

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
        print("[rotate_partitions] Ensuring current and next partitions...")
        await ensure_write_partitions(session)

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
