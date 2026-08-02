#!/usr/bin/env python3
"""Gated live smoke test for Google Sensitive Data Protection.

Uses Application Default Credentials and makes two billable inspectContent calls.
It prints only entity metadata, never matched text or complete prompts.
"""

from __future__ import annotations

import asyncio
import os
import sys

from app import pii


async def main() -> int:
    project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT_ID")
    if not project:
        print("ERROR: set GOOGLE_CLOUD_PROJECT before running this live smoke", file=sys.stderr)
        return 2

    location = os.environ.get("GOOGLE_DLP_LOCATION", "global")
    endpoint = os.environ.get("GOOGLE_DLP_API_ENDPOINT") or None
    await pii.initialize(
        backend="google",
        google_project=project,
        google_location=location,
        google_api_endpoint=endpoint,
        google_min_likelihood="POSSIBLE",
        google_info_types=(
            "EMAIL_ADDRESS",
            "PHONE_NUMBER",
            "US_SOCIAL_SECURITY_NUMBER",
            "CREDIT_CARD_NUMBER",
            "IP_ADDRESS",
            "STREET_ADDRESS",
            "DATE_OF_BIRTH",
        ),
        google_timeout_seconds=10.0,
    )

    positive = await pii.run(
        "Email dlp-smoke@example.com, call 212-555-0100, or reference SSN 219-09-9999"
    )
    positive_types = {finding["type"] for finding in positive.findings}
    required_types = {"EMAIL_ADDRESS", "PHONE_NUMBER", "US_SSN"}
    if not required_types.issubset(positive_types):
        print(f"FAIL positive control: types={sorted(positive_types)}", file=sys.stderr)
        return 1
    if any(
        raw_value in positive.redacted_text
        for raw_value in ("dlp-smoke@example.com", "212-555-0100", "219-09-9999")
    ):
        print("FAIL positive control: structured PII was not redacted", file=sys.stderr)
        return 1

    technical_text = "\n".join(
        (
            "what is the latest version of Django web framework",
            "compare Flask and FastAPI",
            "upgrade PostgreSQL and Kubernetes",
            "debug React with Gemini and Claude",
        )
    )
    technical = await pii.run(technical_text)
    if technical.findings or technical.redacted_text != technical_text:
        print(
            "FAIL technical-query control: "
            f"types={sorted(finding['type'] for finding in technical.findings)}",
            file=sys.stderr,
        )
        return 1

    print(
        "PASS Google DLP live smoke: "
        f"positive_types={sorted(positive_types)} technical_query_findings=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
