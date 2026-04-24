# KnowIT-Graphs

A **Claude-powered (pluggable) AI assistant** for the Knowledge Graph / Solution EI platform. Clients and the KG team can self-serve answers to routine questions — error codes, schema lookups, "how do I…" queries, feature discovery, and log interpretation — instead of waiting on the core team.

Runs entirely in one Docker container. Drop in an API key, `docker compose up`, open the browser.

---

## Who this is for

- **Clients** debugging their KG pipelines.
- **KG dev/QA team** to offload routine questions.
- Anyone wanting to quickly understand a new KG feature / error / schema.

You do **NOT** need Claude Code, Cursor, Antigravity, or any IDE AI to run this. You just need:

1. Docker Desktop (Windows / Mac / Linux).
2. A free Gemini API key (2 minutes to obtain).
3. A local clone of `sds-solution-ei` (optional but recommended).

---

## One-time setup (≈ 5 minutes)

### 1. Install Docker Desktop

- Windows / Mac: https://www.docker.com/products/docker-desktop/
- Linux: `sudo apt install docker.io docker-compose-plugin`

Verify:
```bash
docker --version
docker compose version
```

### 2. Get a free Gemini API key

1. Go to **https://aistudio.google.com/apikey** and sign in with any Google account.
2. Click **Create API key** → **Create API key in new project**.
3. Copy the key (starts with `AIza...`).

This is free — Gemini 2.0 Flash gives you 1M tokens/day for free, more than enough for this tool.

### 3. Clone / locate the project

```bash
# The project lives here:
cd C:\Users\jithin.kj\Work\Rogue6\knowItGraphs
```

Make sure `sds-solution-ei` sits next to `Rogue6/`:
```
Work/
├── Rogue6/
│   └── knowItGraphs/    ← this project
└── sds-solution-ei/     ← the KG repo we index
```

If your layout differs, edit `REPO_PATH` in `.env` after the next step.

### 4. Create your `.env`

```bash
# Windows (PowerShell or Git Bash)
cp .env.example .env
```

Open `.env` in any editor and paste your key:
```
LLM_PROVIDER=gemini
GEMINI_API_KEY=AIzaXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
```

(Leave the other providers blank — they're optional alternatives.)

### 5. Run it

**Option A — the script (recommended):**
```bash
# Mac / Linux / Git Bash on Windows
bash scripts/run.sh
```
```powershell
# Windows PowerShell
.\scripts\run.ps1
```

**Option B — raw docker compose:**
```bash
docker compose up --build -d
docker compose logs -f app
```

First build takes ~5 minutes (downloads Python base + embedding model). Subsequent runs are instant.

### 6. Open it

Go to **http://localhost:8080**

You should see the KnowIT-Graphs chat UI. The sidebar shows:
- `LLM: gemini (gemini/gemini-2.0-flash)` — confirms your provider.
- `Indexed N chunks` — confirms the handbook + repo are loaded.

If you see `Backend offline`, wait 20 seconds and refresh — first startup indexes everything.

---

## Using it

### Ask a question
Type in the box, hit **Send** (or `Ctrl/Cmd + Enter`).

Examples that work well:
- `How do I implement feature X using KG?`
- `What does error code KG-4021 mean?`
- `What's the correct schema for the "entity_relation" field?`
- `What should I change to reduce pipeline latency?`

### Paste a log
Paste an entire Airflow task log or a Spark stack trace directly in the input box. The assistant auto-detects the log type (a yellow badge appears: `log detected: airflow`), extracts the error, and answers with diagnosis + fix + citation.

Example:
```
[2026-04-22 11:22:31,456] ERROR - Task failed with exception
Traceback (most recent call last):
  File ".../validator.py", line 88, in validate
    raise ValidationError(f"Field {field} missing type")
ValidationError: Field entity_id missing type
```
→ The assistant identifies the error, cites `repo: validator-api/validator.py` + relevant handbook page, and suggests the fix.

### Multiple sessions
Each session keeps its own context. Click **+ New chat** to start fresh. Sessions expire after **30 min** of inactivity (configurable in `.env`).

---

## Using KnowIT-Graphs from other AI agents

In addition to the web UI, two machine-facing paths are exposed so any other AI agent can ask KG questions and get grounded answers.

### 1. Plain JSON (works from any language / framework)

```bash
curl -X POST http://localhost:8080/api/agent/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "how do I disable a candidateKey?"}'
```

Response:
```json
{
  "answer": "...",
  "diagram": "flowchart TD ...",        // extracted mermaid, or null
  "sources": [{"kind": "handbook", "ref": "p.27", "score": 0.94, "snippet": "..."}, ...],
  "log_detected": null,
  "cached": false,
  "session_id": "abc123",
  "model": {"provider": "ollama", "model": "ollama/llama3.1:8b"},
  "retrieved_count": 6
}
```

Also available:
- `POST /api/agent/search` — retrieval only, no LLM. Returns top chunks. Cheaper; lets the calling agent reason itself.

See `examples/curl-agent-api.sh` for a ready-to-run shell script.

### 2. MCP server (Cursor, Claude Desktop, any MCP client)

The `mcp` service in `docker-compose.yml` exposes KnowIT-Graphs as MCP tools at `http://localhost:8765/sse`. Three tools are available:

- **`ask_kg`** — end-to-end Q&A with sources + diagram.
- **`search_kg`** — retrieval-only, returns top chunks.
- **`diagnose_log`** — log-focused analysis.

**Connecting Cursor** (for demo):

1. In your Cursor project root, create `.cursor/mcp.json`:
   ```json
   {
     "mcpServers": {
       "knowit-graphs": {
         "url": "http://localhost:8765/sse"
       }
     }
   }
   ```
   (Or copy `examples/.cursor/mcp.json` from this repo.)

2. Restart Cursor. In the agent/composer panel, you'll see KnowIT-Graphs listed under MCP tools.

3. Ask Cursor's Claude a KG question — it will call `ask_kg` and inline the response with citations.

**Connecting Claude Desktop**: same config in Claude Desktop's MCP settings.

**Other agents** (LangChain, LlamaIndex, n8n, custom Python): use the JSON endpoint above. The MCP path is specifically for MCP-speaking clients.

### Enabling bearer-token auth

Set `AGENT_API_TOKEN=<long-random-string>` in `.env` before exposing the ports outside your machine. Both `/api/agent/*` and the MCP server will then require:
```
Authorization: Bearer <long-random-string>
```
on every call. The MCP service passes the token through to the upstream app automatically when you set it.

For Cursor with auth, use `examples/.cursor/mcp.with-auth.json` as a template.

## Kubernetes (future)

The three services map 1:1 to Deployments:

| Docker service | Kubernetes |
|---|---|
| `app` | `Deployment` + `Service` (ClusterIP — internal only) + PVC for `/app/.cache/index` |
| `mcp` | `Deployment` + `Service` (LoadBalancer or Ingress — exposed to other agents) |
| `ollama` | `StatefulSet` + PVC — or drop entirely and point `LLM_PROVIDER=gemini` |

Notes for cluster deployment:
- Set `AGENT_API_TOKEN` (required if MCP is exposed beyond the cluster).
- Mount the Solution EI repo as a read-only volume, or bake it into a sidecar that pulls from git.
- A single replica of `app` is fine (the in-memory index is rebuilt from the PVC cache in seconds). Scale `mcp` horizontally if many agents connect.
- Run `kompose convert` on `docker-compose.yml` for a starting set of manifests; then add `AGENT_API_TOKEN` as a `Secret` and the index PVC.

## Updating the handbook / repo

If the handbook PDF or Solution EI repo changes:

```bash
# Replace the PDF
cp /path/to/new-handbook.pdf data/handbook.pdf

# Rebuild index (restart container)
bash scripts/reindex.sh
```

---

## Switching LLM providers

Edit `.env`:

| Provider | `LLM_PROVIDER` | Key env var | Notes |
|---|---|---|---|
| **Gemini** (free) | `gemini` | `GEMINI_API_KEY` | Default. 1M tokens/day free. |
| **Anthropic Claude** | `anthropic` | `ANTHROPIC_API_KEY` | Paid. Best quality. |
| **OpenAI** | `openai` | `OPENAI_API_KEY` | Paid. |
| **Ollama** (local, free) | `ollama` | none | See below. Zero cloud cost. |

Restart: `docker compose restart app`.

### Using Ollama (fully local, no key)

```bash
# Start the optional Ollama container
docker compose --profile local up -d ollama

# Pull a model (once, ~5 GB)
docker exec knowit-ollama ollama pull llama3.1:8b

# Point app at Ollama
# In .env:
LLM_PROVIDER=ollama
OLLAMA_MODEL=ollama/llama3.1:8b

docker compose restart app
```

Good for teammates who have no API key. Slower, smaller model quality, but fully offline.

---

## Troubleshooting

| Symptom | Cause / Fix |
|---|---|
| `GEMINI_API_KEY is empty` | You forgot step 4. Edit `.env`, put the key, `docker compose restart app`. |
| UI loads but chat errors | Check `docker compose logs app`. Likely bad key or rate limit. |
| `Indexed 0 chunks` | Handbook PDF or repo mount missing. Check `REPO_PATH` in `.env` points to `sds-solution-ei`. |
| Container won't start | `docker compose logs app`. 90% of the time it's a missing or malformed `.env`. |
| Slow first response | First query builds the embedding model cache. Subsequent queries are fast. |
| Port 8080 in use | Change `APP_PORT=8081` in `.env`, then `docker compose up -d`. |

Complete reset:
```bash
docker compose down -v
docker compose up --build -d
```

---

## What it does / doesn't do

**Does:**
- Retrieves from the KG handbook PDF + Solution EI repo (hybrid BM25 + embeddings).
- Cites every answer (handbook page / repo file).
- Parses Airflow + Spark + generic logs.
- Keeps multi-turn chat context per session.
- Redacts secrets (API keys, passwords, tokens) in output.
- Refuses off-topic questions.

**Does NOT:**
- Send your logs/code anywhere except to the configured LLM provider (Gemini / Claude / OpenAI / Ollama). **Nothing is logged by us.**
- Auto-fix pipelines. It diagnoses and points to the right file/line.
- Replace human judgment on complex incidents.

---

## Project layout

```
knowItGraphs/
├── README.md                  ← you are here
├── CONTRIBUTING.md            ← how to extend the system
├── docker-compose.yml
├── Dockerfile
├── .env.example
├── requirements.txt
├── mcp_server/                ← MCP service for Cursor / Claude Desktop
│   ├── Dockerfile
│   ├── requirements.txt
│   └── server.py              tool handlers, HTTP+SSE transport
├── examples/
│   ├── .cursor/mcp.json       drop into your Cursor project root
│   ├── .cursor/mcp.with-auth.json
│   └── curl-agent-api.sh      smoke-test the JSON endpoints
├── app/
│   ├── main.py                FastAPI endpoints
│   ├── config.py              env-var loader
│   ├── ingest.py              PDF + repo → chunks
│   ├── rag.py                 hybrid BM25 + embeddings
│   ├── llm.py                 pluggable LLM adapter
│   ├── sessions.py            in-mem sessions + TTL
│   ├── log_parser.py          airflow / spark detection
│   ├── guardrails.py          secret redaction + code-size cap
│   └── prompts/system.md      KG-scoped system prompt
├── ui/
│   ├── index.html
│   ├── styles.css
│   └── app.js                 vanilla JS chat UI
├── data/
│   └── handbook.pdf           embedded at build
└── scripts/
    ├── run.sh  run.ps1        one-command start
    └── reindex.sh             restart after data changes
```

---

## Adding curated reference data (e.g. Spark)

Drop JSON files into `data/references/`. Each file becomes a retrievable source with `kind` = filename stem.

Example: `data/references/spark.json` → chunks tagged `kind: spark`, cited as `spark ref: <topic>`.

Supported top-level shapes:
- `[{...}, {...}]` — list of entries
- `{"kind": "...", "entries": [...]}` — explicit kind + entries
- `{"topic-a": {...}, "topic-b": {...}}` — dict of entries (key becomes title)

After dropping or replacing a file:
```bash
bash scripts/reindex.sh
```

The spark.json shipped in the repo is a placeholder showing the format — replace with your scraped / curated dataset.

## License / Support

Internal tool. For issues, reach out to the KG Data Engineering team.
