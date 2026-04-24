#!/usr/bin/env bash
# Quick sanity-test of the agent endpoints from the shell.
# Requires: jq (optional, for pretty-printing).

set -euo pipefail
API="${API:-http://localhost:8080}"
AUTH=()
if [[ -n "${AGENT_API_TOKEN:-}" ]]; then
  AUTH=(-H "Authorization: Bearer ${AGENT_API_TOKEN}")
fi

echo "=== ask_kg ==="
curl -sS -X POST "$API/api/agent/ask" \
  -H "Content-Type: application/json" \
  "${AUTH[@]}" \
  -d '{"question": "how do I disable a candidateKey in disambiguation?"}' \
  | (command -v jq >/dev/null && jq . || cat)

echo
echo "=== search_kg ==="
curl -sS -X POST "$API/api/agent/search" \
  -H "Content-Type: application/json" \
  "${AUTH[@]}" \
  -d '{"query": "OutOfMemoryError spark", "top_k": 3}' \
  | (command -v jq >/dev/null && jq '.chunks[] | {kind, ref, score}' || cat)
