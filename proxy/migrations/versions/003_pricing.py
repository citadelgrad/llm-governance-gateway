"""pricing

Creates the proxy-owned pricing table: versioned $/token input+output
rates per model, each with an effective_from date, so historical cost
never changes retroactively (ADR 0002).

Revision ID: 003
Revises: 002
Create Date: 2026-08-05
"""

from alembic import op

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE pricing (
            id UUID NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
            model_id TEXT NOT NULL,
            input_rate_usd_per_token NUMERIC(14, 10) NOT NULL,
            output_rate_usd_per_token NUMERIC(14, 10) NOT NULL,
            effective_from DATE NOT NULL,
            UNIQUE (model_id, effective_from)
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS pricing")
