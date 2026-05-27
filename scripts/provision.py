#!/usr/bin/env python3
"""Idempotent IaC provisioner — upserts config into Postgres and emits OPA data docs."""

import hashlib
import json
import os
import secrets
import sys
from base64 import urlsafe_b64encode
from pathlib import Path

import psycopg2
import psycopg2.extras
import yaml
from passlib.context import CryptContext

ROOT = Path(__file__).parent.parent
CONFIG = ROOT / "config"
POLICIES_DATA = ROOT / "policies" / "data"

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


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

    conn = psycopg2.connect(db_url)
    conn.autocommit = False
    cur = conn.cursor()

    # Create tables
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tenants (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            allowed_models JSONB NOT NULL DEFAULT '[]',
            rate_limit INTEGER NOT NULL DEFAULT 1000,
            pii_action TEXT NOT NULL DEFAULT 'redact',
            pii_redaction_notification BOOLEAN NOT NULL DEFAULT true,
            default_provider TEXT NOT NULL,
            contact_email TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL REFERENCES tenants(id),
            roles JSONB NOT NULL DEFAULT '[]'
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS api_keys (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id),
            key_hash TEXT NOT NULL,
            key_prefix TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bootstrap_state (
            id TEXT PRIMARY KEY DEFAULT 'singleton',
            provisioned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            config_hash TEXT NOT NULL
        )
    """)
    conn.commit()

    # Idempotency check
    current_hash = config_hash()
    cur.execute("SELECT config_hash FROM bootstrap_state WHERE id = 'singleton'")
    row = cur.fetchone()
    if row and row[0] == current_hash:
        print("✓ No changes detected. Provisioner is a no-op.")
        conn.close()
        return

    tenants, users, models = load_config()

    # Upsert tenants
    for t in tenants:
        cur.execute("""
            INSERT INTO tenants (id, name, allowed_models, rate_limit, pii_action,
                                 pii_redaction_notification, default_provider, contact_email)
            VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name,
                allowed_models = EXCLUDED.allowed_models,
                rate_limit = EXCLUDED.rate_limit,
                pii_action = EXCLUDED.pii_action,
                pii_redaction_notification = EXCLUDED.pii_redaction_notification,
                default_provider = EXCLUDED.default_provider,
                contact_email = EXCLUDED.contact_email
        """, (
            t["id"], t["name"],
            json.dumps(t.get("allowed_models", [])),
            t.get("rate_limit", 1000),
            t.get("pii_action", "redact"),
            t.get("pii_redaction_notification", True),
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
        """, (u["id"], u["tenant_id"], json.dumps(u.get("roles", []))))

        # Check if key already exists for this user
        cur.execute("SELECT id FROM api_keys WHERE user_id = %s", (u["id"],))
        if not cur.fetchone():
            plaintext = generate_key()
            key_hash = pwd_ctx.hash(plaintext)
            key_prefix = plaintext[:12]
            key_id = f"key_{u['id']}"
            cur.execute("""
                INSERT INTO api_keys (id, user_id, key_hash, key_prefix)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
            """, (key_id, u["id"], key_hash, key_prefix))
            generated_keys.append((u["id"], plaintext))

    # Update bootstrap_state
    cur.execute("""
        INSERT INTO bootstrap_state (id, config_hash)
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

    conn.close()

    if generated_keys:
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
