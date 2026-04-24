#!/usr/bin/env bash
# demo-start.sh — bring up the full KnowIT + Airflow + Spark + MinIO stack.
set -euo pipefail
cd "$(dirname "$0")/../.."

if [[ ! -f .env ]]; then
  echo "[demo] .env missing — run scripts/run.sh first to configure KnowIT."
  exit 1
fi

if [[ ! -f demo/jars/app.jar ]]; then
  echo "[demo] demo/jars/app.jar is missing. Place your sds-ei-analytics jar there."
  exit 1
fi

# Default SDS_EI_REPO to the sibling layout used by the base compose
if [[ -z "${SDS_EI_REPO:-}" ]]; then
  if [[ -d "../../sds-solution-ei" ]]; then
    export SDS_EI_REPO="../../sds-solution-ei"
  elif [[ -d "../sds-solution-ei" ]]; then
    export SDS_EI_REPO="../sds-solution-ei"
  else
    echo "[demo] SDS_EI_REPO not set and sds-solution-ei not found."
    echo "       Set SDS_EI_REPO=/path/to/sds-solution-ei and rerun."
    exit 1
  fi
fi

echo "[demo] Using SDS_EI_REPO=$SDS_EI_REPO"
docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d --build

echo
echo "  KnowIT UI:     http://localhost:8080"
echo "  Airflow UI:    http://localhost:8088    (admin / admin)"
echo "  Spark master:  http://localhost:8090"
echo "  MinIO console: http://localhost:9001    (minio / minio12345)"
echo
echo "First boot takes ~5 min (image builds + indexing). Tail with:"
echo "  docker compose -f docker-compose.yml -f docker-compose.demo.yml logs -f airflow-scheduler spark-master"
