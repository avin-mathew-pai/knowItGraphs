#!/usr/bin/env bash
# Start KnowIT-Graphs. Builds on first run; reuses image after.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ ! -f .env ]]; then
  echo "[run.sh] .env not found — copying .env.example → .env"
  cp .env.example .env
  echo "[run.sh] Edit .env now and add your GEMINI_API_KEY (https://aistudio.google.com/apikey)"
  echo "[run.sh] Then re-run this script."
  exit 1
fi

docker compose up --build -d
echo
echo "KnowIT-Graphs is starting. Open http://localhost:${APP_PORT:-8080}"
echo "Follow logs: docker compose logs -f app"
