#!/usr/bin/env bash
# Usage: entrypoint.sh [master|worker|submit] [args...]
set -euo pipefail

ROLE="${1:-master}"
shift || true

case "$ROLE" in
  master)
    exec /opt/spark/bin/spark-class org.apache.spark.deploy.master.Master \
      --host 0.0.0.0 --port 7077 --webui-port 8080
    ;;
  worker)
    MASTER_URL="${1:-spark://spark-master:7077}"
    exec /opt/spark/bin/spark-class org.apache.spark.deploy.worker.Worker \
      "${MASTER_URL}" --webui-port 8080
    ;;
  submit)
    exec /opt/spark/bin/spark-submit "$@"
    ;;
  shell)
    exec /bin/bash -c "$*"
    ;;
  *)
    echo "Unknown role: $ROLE (expected master|worker|submit|shell)" >&2
    exit 1
    ;;
esac
