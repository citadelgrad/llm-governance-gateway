"""baseline

No proxy-owned tables exist yet; this establishes the proxy's own Alembic
version-tracking table. usage_log and pricing land in later revisions
(ai-gateway-wirs.3, ai-gateway-wirs.4).

Revision ID: 001
Revises:
Create Date: 2026-08-05
"""

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
