#!/usr/bin/env python3
"""Idempotent IaC provisioner — upserts config into Postgres and emits OPA data docs."""

import hashlib
import json
import os
import secrets
import sys
from base64 import urlsafe_b64encode
from contextlib import closing
from pathlib import Path

import psycopg2
import psycopg2.extras
import yaml
import bcrypt

ROOT = Path(__file__).parent.parent
CONFIG = ROOT / "config"
POLICIES_DATA = ROOT / "policies" / "data"

def load_config():
    tenants = yaml.safe_load((CONFIG / "tenants.yaml").read_text())["tenants"]
    users = yaml.safe_load((CONFIG / "users.yaml").read_text())["users"]
    models = yaml.safe_load((CONFIG / "models.yaml").read_text())["models"]
    return tenants, users, models


def config_hash():
    combined = ""
    for f in ["tenants.yaml", "users.yaml", "models.yaml"]:
        combined += (CONFIG / f).read_text()
    return hashlib.sha256(combined.encode()).hexdigest()


def generate_key():
    raw = secrets.token_bytes(32)
    encoded = urlsafe_b64encode(raw).rstrip(b"=").decode()
    return f"gw_{encoded}"


def main():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)

    # psycopg2 expects postgresql:// not postgresql+asyncpg://
    db_url = db_url.replace("postgresql+asyncpg://", "postgresql://")

    with closing(psycopg2.connect(db_url)) as conn:
        conn.autocommit = False
        cur = conn.cursor()

        # Create tables — column names must match what the proxy queries.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tenants (
                tenant_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                allowed_models JSONB NOT NULL DEFAULT '[]',
                rate_limit_requests_per_minute INTEGER NOT NULL DEFAULT 1000,
                pii_action TEXT NOT NULL DEFAULT 'redact',
                pii_redaction_notification TEXT NOT NULL DEFAULT 'header',
                default_provider TEXT NOT NULL,
                contact_email TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
                roles JSONB NOT NULL DEFAULT '[]'
            )
        """)
        # api_keys schema must match proxy/app/auth.py (SELECT hash, user_id, tenant_id, roles
        # WHERE prefix = $1) and proxy/app/main.py create_key INSERT.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS api_keys (
                prefix TEXT PRIMARY KEY,
                hash TEXT NOT NULL,
                user_id TEXT NOT NULL REFERENCES users(id),
                tenant_id TEXT NOT NULL,
                roles TEXT[] NOT NULL DEFAULT '{}',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        # provisioner_state is separate from governance's bootstrap_state (created by Alembic).
        cur.execute("""
            CREATE TABLE IF NOT EXISTS provisioner_state (
                id TEXT PRIMARY KEY DEFAULT 'singleton',
                provisioned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                config_hash TEXT NOT NULL
            )
        """)
        conn.commit()

        # Idempotency check
        current_hash = config_hash()
        cur.execute("SELECT config_hash FROM provisioner_state WHERE id = 'singleton'")
        row = cur.fetchone()
        if row and row[0] == current_hash:
            print("✓ No changes detected. Provisioner is a no-op.")
            return

        tenants, users, models = load_config()

        # Upsert tenants
        for t in tenants:
            cur.execute("""
                INSERT INTO tenants (tenant_id, name, allowed_models, rate_limit_requests_per_minute,
                                     pii_action, pii_redaction_notification, default_provider, contact_email)
                VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    allowed_models = EXCLUDED.allowed_models,
                    rate_limit_requests_per_minute = EXCLUDED.rate_limit_requests_per_minute,
                    pii_action = EXCLUDED.pii_action,
                    pii_redaction_notification = EXCLUDED.pii_redaction_notification,
                    default_provider = EXCLUDED.default_provider,
                    contact_email = EXCLUDED.contact_email
            """, (
                t["id"], t["name"],
                psycopg2.extras.Json(t.get("allowed_models", [])),
                t.get("rate_limit", 1000),
                t.get("pii_action", "redact"),
                t.get("pii_redaction_notification", "header"),
                t["default_provider"],
                t.get("contact_email"),
            ))

        # Upsert users and generate API keys
        generated_keys = []
        POLICIES_DATA.mkdir(parents=True, exist_ok=True)

        for u in users:
            cur.execute("""
                INSERT INTO users (id, tenant_id, roles)
                VALUES (%s, %s, %s::jsonb)
                ON CONFLICT (id) DO UPDATE SET
                    tenant_id = EXCLUDED.tenant_id,
                    roles = EXCLUDED.roles
            """, (u["id"], u["tenant_id"], psycopg2.extras.Json(u.get("roles", []))))

            # Check if key already exists for this user
            cur.execute("SELECT prefix FROM api_keys WHERE user_id = %s", (u["id"],))
            if not cur.fetchone():
                plaintext = generate_key()
                key_hash = bcrypt.hashpw(plaintext.encode(), bcrypt.gensalt()).decode()
                key_prefix = plaintext[:8]
                cur.execute("""
                    INSERT INTO api_keys (prefix, hash, user_id, tenant_id, roles)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (prefix) DO NOTHING
                """, (key_prefix, key_hash, u["id"], u["tenant_id"], u.get("roles", [])))
                generated_keys.append((u["id"], plaintext))

        # Update provisioner_state
        cur.execute("""
            INSERT INTO provisioner_state (id, config_hash)
            VALUES ('singleton', %s)
            ON CONFLICT (id) DO UPDATE SET
                config_hash = EXCLUDED.config_hash,
                provisioned_at = NOW()
        """, (current_hash,))

        conn.commit()

        # Emit OPA data documents
        users_doc = {
            "users": {
                u["id"]: {"tenant_id": u["tenant_id"], "roles": u.get("roles", [])}
                for u in users
            }
        }
        tenants_doc = {
            "tenants": {
                t["id"]: {
                    "allowed_models": t.get("allowed_models", []),
                    "rate_limit": t.get("rate_limit", 1000),
                    "pii_action": t.get("pii_action", "redact"),
                }
                for t in tenants
            }
        }

        (POLICIES_DATA / "users.json").write_text(json.dumps(users_doc, indent=2))
        (POLICIES_DATA / "tenants.json").write_text(json.dumps(tenants_doc, indent=2))
        print(f"✓ OPA data documents written to {POLICIES_DATA}")

    if generated_keys:
        if os.environ.get("SUPPRESS_GENERATED_KEYS") == "true":
            print(f"✓ Generated {len(generated_keys)} API key(s); plaintext output suppressed.")
        else:
            print("\n" + "=" * 60)
            print("WARNING: GENERATED API KEYS — STORE THESE NOW, NOT SHOWN AGAIN")
            print("=" * 60)
            for user_id, key in generated_keys:
                print(f"  {user_id}: {key}")
            print("=" * 60 + "\n")
    else:
        print("✓ All keys already exist. No new keys generated.")

    print("✓ Provisioning complete.")


if __name__ == "__main__":
    main()
