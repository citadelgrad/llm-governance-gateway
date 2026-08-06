"""usage_log_cost_nullable

Allows usage_log.cost_usd to be NULL for rows where no pricing table
entry is effective for the resolved model at request time.

Revision ID: 004
Revises: 003
Create Date: 2026-08-05
"""

from alembic import op

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE usage_log ALTER COLUMN cost_usd DROP NOT NULL")


def downgrade() -> None:
    op.execute("UPDATE usage_log SET cost_usd = 0 WHERE cost_usd IS NULL")
    op.execute("ALTER TABLE usage_log ALTER COLUMN cost_usd SET NOT NULL")
