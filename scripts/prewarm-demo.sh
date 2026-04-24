#!/usr/bin/env bash
# Pre-warm the response cache with demo questions so Cursor gets instant
# (~10ms) answers during the live demo — avoiding Ollama's cold-generation
# latency that trips Cursor's MCP tool-call timeout.
#
# Edit the QUESTIONS array below to match what you'll actually ask.
# Re-run this any time you change the handbook, repo, or system prompt
# (the cache auto-invalidates on index hash change, so prewarm must be rerun).

set -euo pipefail
API="${API:-http://localhost:8080}"
AUTH=()
if [[ -n "${AGENT_API_TOKEN:-}" ]]; then
  AUTH=(-H "Authorization: Bearer ${AGENT_API_TOKEN}")
fi

QUESTIONS=(
  "How does disambiguation work at the inter level?"
  "Can I disable a candidateKey in disambiguation? If yes, show the format."
  "What is the schema for the output block in the inventory config?"
  "How do I integrate a new entity source into the KG pipeline?"
  "What does exceptionFilter do in candidateKeys?"
  "Why is my Spark job failing with OutOfMemoryError during shuffle?"
  "How do I reduce latency in a disambiguation pipeline?"
  "What is the difference between intra-level and inter-level disambiguation?"
)

echo "Pre-warming cache for ${#QUESTIONS[@]} questions against $API …"
for q in "${QUESTIONS[@]}"; do
  echo
  echo "→ $q"
  start=$(date +%s)
  resp=$(curl -sS -X POST "$API/api/agent/ask" \
    -H "Content-Type: application/json" \
    "${AUTH[@]}" \
    -d "$(printf '{"question": %s}' "$(printf '%s' "$q" | python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))')")")
  elapsed=$(($(date +%s) - start))
  cached=$(printf '%s' "$resp" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("cached"))' 2>/dev/null || echo "?")
  echo "  done in ${elapsed}s (cached=$cached)"
done

echo
echo "Cache stats:"
curl -sS "$API/api/cache" "${AUTH[@]}"
echo
echo "Pre-warm complete. Re-run these same questions from Cursor — they'll return instantly."
