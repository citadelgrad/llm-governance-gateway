"""usage_log

Creates the proxy-owned usage_log table: one row per Usage Event, kept
separate from governance's pseudonymized audit_log (ADR 0001).

Revision ID: 002
Revises: 001
Create Date: 2026-08-05
"""

from alembic import op

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE usage_log (
            id UUID NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            tenant_id TEXT NOT NULL,
            api_key_prefix TEXT NOT NULL,
            user_id TEXT,
            model_id TEXT,
            status TEXT NOT NULL CHECK (status IN ('allowed', 'blocked', 'errored')),
            prompt_tokens INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            cost_usd NUMERIC(12, 6) NOT NULL DEFAULT 0,
            latency_ms INTEGER
        )
    """)

    op.execute("""
        CREATE INDEX ix_usage_log_tenant_created
        ON usage_log (tenant_id, created_at)
    """)

    op.execute("""
        CREATE INDEX ix_usage_log_api_key_prefix_created
        ON usage_log (api_key_prefix, created_at)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_usage_log_api_key_prefix_created")
    op.execute("DROP INDEX IF EXISTS ix_usage_log_tenant_created")
    op.execute("DROP TABLE IF EXISTS usage_log")
