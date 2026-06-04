from __future__ import annotations

import json


def jsonb_list(value: object) -> list:
    """Normalize asyncpg JSONB values.

    asyncpg returns JSON/JSONB as strings unless a custom codec is registered.
    Tenant config callers need the decoded list, not a list of string characters.
    """
    if value is None:
        return []
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return []
        return decoded if isinstance(decoded, list) else []
    if isinstance(value, list):
        return value
    return list(value) if isinstance(value, tuple) else []
