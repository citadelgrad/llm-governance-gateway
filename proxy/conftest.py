from __future__ import annotations

import os
import sys

# Add project root so both proxy.* and tests.* imports resolve
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Required env vars must be set before proxy modules import Settings (Pydantic reads
# them at class instantiation, which happens on first import of proxy.app.config).
# JWT_SECRET is force-set (not setdefault) so tests stay hermetic even when a
# developer shell already exports a real JWT_SECRET (e.g. via the repo .envrc) —
# test JWTs are always signed with this exact value in tests/conftest.py.
os.environ["JWT_SECRET"] = "test-jwt-secret-for-tests-only-32chars!!"
os.environ.setdefault("GOVERNANCE_INTERNAL_TOKEN", "test-gov-token")
os.environ.setdefault("MOCK_PROVIDERS", "true")
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://gateway:gateway@localhost:5432/gateway"
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
