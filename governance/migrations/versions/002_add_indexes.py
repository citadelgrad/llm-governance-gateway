"""add missing indexes for audit_log and user_pseudonym_map

Revision ID: 002
Revises: 001
Create Date: 2026-05-30
"""

from alembic import op

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # For audit_list queries: WHERE tenant_id = :tenant_id ORDER BY created_at DESC
    op.execute("""
        CREATE INDEX ix_audit_log_tenant_created
        ON audit_log (tenant_id, created_at DESC)
    """)

    # For GDPR erasure: WHERE user_id = ANY(:pseudonyms)
    op.execute("""
        CREATE INDEX ix_audit_log_user_id
        ON audit_log (user_id)
    """)

    # For audit_export keyset pagination: ORDER BY created_at, audit_id
    # The composite PK is (audit_id, created_at) — wrong order for this query.
    op.execute("""
        CREATE INDEX ix_audit_log_export_keyset
        ON audit_log (created_at, audit_id)
    """)

    # For active pseudonym lookups: WHERE real_user_id = :user_id AND tenant_id = :tenant_id AND deleted_at IS NULL
    op.execute("""
        CREATE INDEX ix_user_pseudonym_map_active
        ON user_pseudonym_map (real_user_id, tenant_id)
        WHERE deleted_at IS NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_user_pseudonym_map_active")
    op.execute("DROP INDEX IF EXISTS ix_audit_log_export_keyset")
    op.execute("DROP INDEX IF EXISTS ix_audit_log_user_id")
    op.execute("DROP INDEX IF EXISTS ix_audit_log_tenant_created")
