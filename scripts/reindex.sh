#!/usr/bin/env bash
# Rebuild the index after updating the handbook PDF or repo contents.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "[reindex.sh] Restarting container to re-ingest data…"
docker compose restart app
echo "[reindex.sh] Done. Follow logs: docker compose logs -f app"
