"""initial schema

Revision ID: 001
Revises:
Create Date: 2026-05-27
"""

from alembic import op

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE user_pseudonym_map (
            pseudonym TEXT NOT NULL PRIMARY KEY,
            real_user_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            rotation_id INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            deleted_at TIMESTAMPTZ,
            UNIQUE (real_user_id, tenant_id, rotation_id)
        )
    """)

    op.execute("""
        CREATE TABLE erasure_log (
            erasure_id UUID NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
            pseudonym TEXT NOT NULL REFERENCES user_pseudonym_map(pseudonym),
            audit_row_count BIGINT NOT NULL,
            erased_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            erased_by TEXT
        )
    """)

    op.execute("""
        CREATE TABLE partition_archive_state (
            table_name TEXT NOT NULL PRIMARY KEY,
            month DATE NOT NULL,
            detached_at TIMESTAMPTZ,
            dumped_at TIMESTAMPTZ,
            verified_at TIMESTAMPTZ,
            dropped_at TIMESTAMPTZ
        )
    """)

    op.execute("""
        CREATE TABLE bootstrap_state (
            id INTEGER NOT NULL PRIMARY KEY CHECK (id = 1),
            token_hash TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            rotated_at TIMESTAMPTZ
        )
    """)

    op.execute("""
        CREATE TABLE audit_log (
            audit_id TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            written_at TIMESTAMPTZ,
            user_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            model_id TEXT,
            routing_method TEXT NOT NULL,
            decision TEXT NOT NULL DEFAULT 'allow',
            pii_findings JSONB NOT NULL DEFAULT '[]',
            harm_score DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            violations JSONB NOT NULL DEFAULT '[]',
            phase TEXT NOT NULL DEFAULT 'request',
            PRIMARY KEY (audit_id, created_at)
        ) PARTITION BY RANGE (created_at)
    """)

    # Pre-create 3 monthly partitions (today = 2026-05-27)
    op.execute("""
        CREATE TABLE audit_log_2026_05 PARTITION OF audit_log
            FOR VALUES FROM ('2026-05-01') TO ('2026-06-01')
    """)
    op.execute("""
        CREATE TABLE audit_log_2026_06 PARTITION OF audit_log
            FOR VALUES FROM ('2026-06-01') TO ('2026-07-01')
    """)
    op.execute("""
        CREATE TABLE audit_log_2026_07 PARTITION OF audit_log
            FOR VALUES FROM ('2026-07-01') TO ('2026-08-01')
    """)

    # Role and permissions
    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'gateway_app') THEN
                CREATE ROLE gateway_app;
            END IF;
        END $$
    """)
    op.execute("""
        DO $$ BEGIN
            EXECUTE format('GRANT gateway_app TO %I', current_user);
        END $$
    """)

    for table in ["user_pseudonym_map", "erasure_log", "partition_archive_state", "bootstrap_state"]:
        op.execute(f"GRANT SELECT, INSERT ON {table} TO gateway_app")
        op.execute(f"GRANT UPDATE ON {table} TO gateway_app")

    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM gateway_app")
    op.execute("GRANT INSERT, SELECT ON audit_log TO gateway_app")

    # Row-level security on audit_log
    op.execute("ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE audit_log FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY audit_read ON audit_log
            FOR SELECT
            USING (
                tenant_id = current_setting('app.current_tenant_id', true)
                AND CASE current_setting('app.current_scope', true)
                    WHEN 'PLATFORM' THEN true
                    WHEN 'TENANT' THEN true
                    WHEN 'SELF' THEN user_id = current_setting('app.current_user_id', true)
                    ELSE false
                END
            )
    """)

    # Append-only enforcement
    op.execute("""
        CREATE OR REPLACE FUNCTION deny_audit_mutation()
        RETURNS TRIGGER LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'audit_log is append-only: % operations are not allowed', TG_OP;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER audit_log_no_mutation
            BEFORE UPDATE OR DELETE ON audit_log
            FOR EACH ROW EXECUTE FUNCTION deny_audit_mutation()
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS audit_log_2026_05, audit_log_2026_06, audit_log_2026_07")
    op.execute("DROP TABLE IF EXISTS audit_log")
    op.execute("DROP FUNCTION IF EXISTS deny_audit_mutation()")
    op.execute("DROP TABLE IF EXISTS bootstrap_state")
    op.execute("DROP TABLE IF EXISTS partition_archive_state")
    op.execute("DROP TABLE IF EXISTS erasure_log")
    op.execute("DROP TABLE IF EXISTS user_pseudonym_map")
