#!/usr/bin/env python3
"""Safe Google ADC identity/refresh preflight for local DLP development."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC

from app.google_credentials import GoogleCredentialPreflightError, preflight


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--expected-service-account",
        default=os.environ.get("GOOGLE_DLP_EXPECTED_SERVICE_ACCOUNT"),
    )
    args = parser.parse_args()
    if not args.expected_service_account:
        print(
            "ERROR: set GOOGLE_DLP_EXPECTED_SERVICE_ACCOUNT to require the intended identity",
            file=sys.stderr,
        )
        return 2
    try:
        result = preflight(expected_service_account=args.expected_service_account)
    except GoogleCredentialPreflightError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    expiry = "unknown"
    if result.expiry is not None:
        expiry = result.expiry.astimezone(UTC).isoformat()
    print(
        "PASS Google ADC preflight: "
        f"service_account={result.service_account_email} token_expiry={expiry}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
