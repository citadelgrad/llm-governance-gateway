#!/usr/bin/env bash
set -euo pipefail
echo "[cron] Starting rotate_partitions at $(date -u)"
cd /app
uv run python scripts/rotate_partitions.py
echo "[cron] Completed at $(date -u)"
