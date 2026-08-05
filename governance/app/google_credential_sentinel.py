from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .google_credentials import GoogleCredentialPreflightError, preflight

_READY = "ready"
_UNAVAILABLE = "unavailable"


def _now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def probe(expected_service_account: str) -> dict[str, Any]:
    checked_at = _now()
    try:
        result = preflight(expected_service_account=expected_service_account)
    except GoogleCredentialPreflightError:
        return {
            "status": _UNAVAILABLE,
            "checked_at": _timestamp(checked_at),
            "reason": "credential_refresh_failed",
        }

    expiry = result.expiry
    status: dict[str, Any] = {
        "status": _READY,
        "checked_at": _timestamp(checked_at),
        "service_account": result.service_account_email,
    }
    if isinstance(expiry, datetime):
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=UTC)
        status["token_expiry"] = _timestamp(expiry.astimezone(UTC))
    return status


def write_status(path: Path, status: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(status, handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def read_status(path: Path, *, max_age_seconds: float) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        checked_at = datetime.fromisoformat(payload["checked_at"].replace("Z", "+00:00"))
        age_seconds = (_now() - checked_at.astimezone(UTC)).total_seconds()
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None
    if age_seconds < 0 or age_seconds > max_age_seconds:
        return None
    if payload.get("status") not in {_READY, _UNAVAILABLE}:
        return None
    return payload


def credentials_ready(path: str | None, *, max_age_seconds: float) -> bool:
    if not path:
        return True
    status = read_status(Path(path), max_age_seconds=max_age_seconds)
    return status is not None and status["status"] == _READY


def run_forever(
    *,
    expected_service_account: str,
    status_path: Path,
    interval_seconds: float,
) -> None:
    while True:
        write_status(status_path, probe(expected_service_account))
        time.sleep(interval_seconds)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Refresh-probe Google ADC and publish status")
    parser.add_argument("--check", action="store_true", help="exit nonzero unless status is fresh and ready")
    return parser


def main() -> int:
    args = _parser().parse_args()
    status_path = Path(os.environ.get("GOOGLE_ADC_STATUS_PATH", "/var/run/google-auth/status.json"))
    max_age_seconds = float(os.environ.get("GOOGLE_ADC_STATUS_MAX_AGE_SECONDS", "90"))
    if args.check:
        return 0 if credentials_ready(str(status_path), max_age_seconds=max_age_seconds) else 1

    expected_service_account = os.environ.get("GOOGLE_DLP_EXPECTED_SERVICE_ACCOUNT")
    if not expected_service_account:
        print("GOOGLE_DLP_EXPECTED_SERVICE_ACCOUNT is required", file=sys.stderr)
        return 2
    interval_seconds = float(os.environ.get("GOOGLE_ADC_PROBE_INTERVAL_SECONDS", "30"))
    if interval_seconds <= 0:
        print("GOOGLE_ADC_PROBE_INTERVAL_SECONDS must be positive", file=sys.stderr)
        return 2
    run_forever(
        expected_service_account=expected_service_account,
        status_path=status_path,
        interval_seconds=interval_seconds,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
