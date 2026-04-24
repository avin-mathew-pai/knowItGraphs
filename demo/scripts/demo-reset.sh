#!/usr/bin/env bash
# demo-reset.sh — clear the demo workdir + Iceberg tables so the next DAG run
# starts from a clean state. Run between live demos.
set -euo pipefail
cd "$(dirname "$0")/../.."

echo "[reset] clearing demo_workdir volume"
# Runs inside the spark-master container so we can wipe the Docker-managed
# volume (no need for host-side permissions). If containers are down we
# fall back to removing the named volume directly.
if docker exec knowit-spark-master rm -rf /mnt/demo/workdir/microsoft__active_directory /mnt/demo/workdir/job_config.json 2>/dev/null; then
  echo "[reset]   cleared via spark-master"
else
  docker volume rm knowitgraphs_demo_workdir 2>/dev/null || true
  echo "[reset]   removed volume (will be recreated on next up)"
fi

echo "[reset] wiping MinIO bucket demo-kg/iceberg/"
docker exec knowit-minio mc alias set local http://localhost:9000 minio minio12345 >/dev/null || true
docker exec knowit-minio mc rm --recursive --force local/demo-kg/iceberg 2>/dev/null || true

echo "[reset] clearing KnowIT incident sessions (via API)"
# Best-effort: each session can be deleted individually
curl -sS http://localhost:8080/api/sessions 2>/dev/null | \
  python3 -c "import sys, json; [print(s['id']) for s in json.load(sys.stdin) if s['title'].startswith('⚠ ')]" 2>/dev/null | \
  while read -r sid; do
    curl -sS -X DELETE "http://localhost:8080/api/sessions/$sid" >/dev/null
  done || true

echo "[reset] done. Trigger DAG in Airflow to run the demo again."
