# KnowIT-Graphs — Claude Code Context

## What this project is

AI assistant for the **KG / Solution EI platform** team. Two things in one:

1. **RAG chat assistant** — answers developer questions about the Knowledge Graph platform, grounded in the handbook, user guide, repo source, and Spark error reference.
2. **Airflow incident agent** — when a KG pipeline task fails, the `on_failure_callback` auto-POSTs the Spark log to KnowIT, which creates a diagnosis session and streams an LLM-powered root-cause analysis into the UI sidebar.

Everything runs in Docker. Single command to boot. No cloud infra needed for local dev.

---

## Repo layout

```
knowItGraphs/
├── app/                        # FastAPI backend
│   ├── main.py                 # All routes: /api/chat, /api/incidents/ingest, /api/agent/ask, /api/sessions
│   ├── rag.py                  # Hybrid BM25 + FAISS retrieval, per-kind weights, bundle cache
│   ├── ingest.py               # PDF / JSON / JSONL / Markdown / repo loader, content-hash cache
│   ├── llm.py                  # litellm wrapper (Gemini / Anthropic / OpenAI / Ollama)
│   ├── sessions.py             # In-memory session store with TTL sweep
│   ├── log_parser.py           # Extracts exception signals from pasted logs
│   ├── guardrails.py           # Redaction + source footer formatting
│   ├── response_cache.py       # LRU response cache (keyed on content hash + prompt + history)
│   ├── config.py               # Pydantic settings from .env
│   └── prompts/system.md       # System prompt for the LLM
│
├── mcp_server/
│   └── server.py               # MCP HTTP+SSE server — exposes ask_kg, search_kg, diagnose_log
│
├── ui/
│   ├── index.html              # 3-column layout (sidebar / retrieval+diagram / chat)
│   ├── app.js                  # SSE streaming, session polling, incident badge, Debug modal
│   └── styles.css              # Dark theme, incident-progress pulse, [hidden]!important fix
│
├── data/                       # Knowledge base (auto-indexed on startup)
│   ├── handbook.pdf
│   ├── kgUserGuide.pdf
│   ├── spark.jsonl
│   └── ...
│
├── demo/
│   ├── spark/Dockerfile        # Spark 4.0.2 / Scala 2.13 + Iceberg 1.10.0 + hadoop-aws 3.4.1 + AWS SDK v2 2.24.6
│   ├── airflow/Dockerfile      # Airflow 2.10.3 + same Spark client baked in
│   ├── jars/app.jar            # sds-ei-analytics_2.13-1.0.1.jar (fat assembly, user-provided)
│   ├── seed/seed_source.py     # PySpark: reads CSVs → writes Iceberg sdm.microsoft__active_directory
│   ├── spark-conf/spark-defaults.conf
│   └── dags/
│       ├── entity_loader_ad_salting_demo.py   # 4-task DAG: prep_inputs → seed_source_table → kg_extractor → verify_output
│       └── callbacks/knowit_callback.py       # on_failure_callback: reads log, strips noise, POSTs to KnowIT
│
├── docker-compose.yml          # Core: app (8080) + mcp (8765) + optional ollama
├── docker-compose.demo.yml     # Demo overlay: MinIO + Postgres + Spark + Airflow
├── .env.example                # Template — copy to .env
├── RUNBOOK.md                  # Full step-by-step setup and demo guide
└── CLAUDE.md                   # ← this file
```

---

## Key ports

| Port | Service |
|------|---------|
| 8080 | KnowIT UI + API |
| 8765 | MCP server (Cursor / Claude Desktop) |
| 8088 | Airflow UI (admin / admin) |
| 8090 | Spark master UI |
| 9000 | MinIO S3 API |
| 9001 | MinIO console (minio / minio12345) |

---

## Essential commands

### Build + start everything
```bash
docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d --build
```

### KnowIT only (no Airflow/Spark)
```bash
docker compose up -d --build
```

### Rebuild one service after code change
```bash
# App or MCP (Python change — no full rebuild needed, just restart)
docker compose restart app
docker compose restart mcp

# If Dockerfile or requirements changed
docker compose build app && docker compose up -d app
docker compose build mcp && docker compose up -d mcp

# Airflow callback changed → MUST restart scheduler to pick it up
docker compose -f docker-compose.yml -f docker-compose.demo.yml restart airflow-scheduler
```

### Logs
```bash
docker logs -f knowit-graphs
docker logs -f knowit-mcp
docker logs -f knowit-airflow-scheduler
docker logs -f knowit-spark-worker

# Incident/callback events only
docker logs -f knowit-airflow-scheduler 2>&1 | grep -i "knowit\|callback\|POST"
docker logs -f knowit-graphs 2>&1 | grep -i "incident\|ingest\|diagnosis"
```

### Stop
```bash
# Keep volumes (fast restart next time)
docker compose -f docker-compose.yml -f docker-compose.demo.yml down

# Full wipe — volumes deleted, next boot re-embeds everything
docker compose -f docker-compose.yml -f docker-compose.demo.yml down -v
```

### Reset demo between runs (wipe Iceberg data)
```bash
docker exec knowit-minio mc alias set local http://localhost:9000 minio minio12345
docker exec knowit-minio mc rm --recursive --force local/demo-kg/iceberg/
```

---

## Architecture — how the pieces connect

```
Browser UI (ui/)
    │  SSE stream (/api/chat)
    │  polling (/api/sessions/{id}/messages)
    ▼
FastAPI app (app/main.py) :8080
    ├── HybridIndex (rag.py)         BM25 + FAISS dense (fastembed BAAI/bge-small-en-v1.5)
    ├── ingest.py                    PDF/JSONL/Markdown/repo → chunks → embeddings
    ├── litellm (llm.py)             Gemini / Anthropic / OpenAI / Ollama via LLM_PROVIDER
    ├── SessionStore (sessions.py)   In-memory, TTL sweep every 60s
    └── INCIDENT_TASKS dict          asyncio.Task per active diagnosis (cancellable)

MCP server (mcp_server/server.py) :8765
    └── forwards tool calls to app via /api/agent/ask and /api/agent/search

Airflow scheduler (demo/dags/)
    └── on_failure_callback → POST /api/incidents/ingest → session created → background LLM

Spark 4.0.2 (demo/spark/)
    └── reads from /mnt/test-resources, writes Iceberg to s3a://demo-kg/iceberg/ (MinIO)
```

---

## Retrieval design

- **BM25** (rank_bm25) + **dense** (fastembed, BAAI/bge-small-en-v1.5) hybrid
- Per-kind score multipliers (higher = surfaces first on close ties):
  - `kguserguide` → **1.5** (user-facing docs, surfaces first)
  - `repo` → **1.3** (source code / implementation truth)
  - `handbook` → **1.15** (canonical schema/field definitions)
  - `spark` → **1.05** (specialised error reference)
- Balanced floor: `RETRIEVAL_PER_KIND=3` guarantees at least 3 chunks per corpus kind
- Configurable via `.env`: `RETRIEVAL_KIND_WEIGHTS=kguserguide:1.6,repo:1.3`
- Bundle cache: embeddings + BM25 saved as `.pkl` keyed by content hash → warm restart is ~10s

---

## Incident flow (Airflow → KnowIT)

```
DAG task fails
  → incident_callback() in knowit_callback.py
      → _tail_log(): reads /opt/airflow/logs/dag_id=.../run_id=.../task_id=.../attempt=N.log
          - strips Airflow line chrome ([timestamp] {file.py:N} INFO -)
          - strips Spark INFO spam (BlockManager, MemoryStore, CodeGenerator, etc.)
          - smart-extracts from "Exception in thread main" marker
          - cap: 80 000 chars
      → _best_exception_line(): extracts "Caused by:" as the real exception
      → requests.post(KNOWIT_URL/api/incidents/ingest, payload, timeout=30)

POST arrives at /api/incidents/ingest (main.py)
  → STORE.create(title="⚠ dag · task")
  → session.add("user", formatted_incident_message)   [author="airflow"]
  → session.add("assistant", INCIDENT_PLACEHOLDER)    [⏳ Analysing…]
  → asyncio.create_task(_run_incident_diagnosis())    [returns immediately to Airflow]

Background task:
  → retrieval (HybridIndex.search)
  → chat_stream (LLM)
  → tokens written to placeholder every 2s
  → final answer replaces placeholder
  → UI polling (/api/sessions/{id}/messages every 2s) picks it up
```

---

## Known gotchas

| Symptom | Cause | Fix |
|---|---|---|
| No incident session in sidebar | `log_tail` > 60K was rejected by Pydantic | Fixed: `max_length=120_000` in `IncidentIngestRequest` |
| Airflow callback fires but session not created | Check `docker logs knowit-airflow-scheduler \| grep "POST →"` — should show `200` | Restart app if needed |
| `knowit-mcp` TypeError: NoneType not callable | Starlette ≥0.38 requires endpoint to return a Response | Fixed: `_NoopResponse` in server.py |
| Spark job fails with NumberFormatException `"60s"` | hadoop-aws version mismatch | Must use `hadoop-aws 3.4.1` + `bundle-2.24.6.jar` (AWS SDK v2) |
| Airflow webserver boot-loops "Already running on PID N" | Stale PID file | Fixed in compose: `rm -f airflow-webserver.pid` before start |
| `prep_inputs` fails with `Errno 1 Operation not permitted` | WSL2 bind-mount permission issue | Fixed: using named Docker volume `demo_workdir` + `volume-init` chmod 777 |
| Log tail shows only Spark shutdown noise | Smart extract was finding shutdown instead of error | Fixed: `_strip_noise` + `_smart_extract` from "Exception in thread main" marker |
| `ERR_EMPTY_RESPONSE` on first boot | App still embedding on startup | Wait for `Uvicorn running` in logs. Embedding cache makes subsequent boots instant |
| Callback changes not picked up | Airflow scheduler caches imported modules | Always `restart airflow-scheduler` after editing `knowit_callback.py` |
| MCP not connecting from Cursor | Server URL must be `http://localhost:8765/sse` | Check MCP config; health endpoint at `http://localhost:8765/health` |

---

## LLM provider — .env settings

```dotenv
# Gemini (free tier — recommended for demo)
LLM_PROVIDER=gemini
GEMINI_API_KEY=AIza...
GEMINI_MODEL=gemini/gemini-2.5-flash

# Anthropic
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...

# Ollama (local, no key)
LLM_PROVIDER=ollama
OLLAMA_MODEL=ollama/llama3.1:8b
# Start with: docker compose --profile local up -d
# Pull model: docker exec knowit-ollama ollama pull llama3.1:8b
```

---

## Demo DAG — entity_loader_ad_salting_demo

**Tasks:**
1. `prep_inputs` — copies CSVs from test-resources to `demo_workdir`, strips `object_guid` header from one partfile (deliberate corruption)
2. `seed_source_table` — spark-submits `seed_source.py` → writes Iceberg table `sdm.microsoft__active_directory`
3. `kg_extractor` — spark-submits `demo/jars/app.jar` (class `ai.prevalent.entityinventory.loader.Loader`) with `salting=45` → **fails** with AnalysisException (missing column from corrupted partition)
4. `verify_output` — skipped on failure

**JAR:** `demo/jars/app.jar` = `sds-ei-analytics_2.13-1.0.1.jar` (fat assembly, user-provided)

**Spark stack:**
- Spark 4.0.2 / Scala 2.13
- Iceberg 1.10.0 (`iceberg-spark-runtime-4.0_2.13-1.10.0.jar`)
- hadoop-aws 3.4.1 (must match Spark 4's Hadoop 3.4 core)
- AWS SDK v2 bundle 2.24.6 (hadoop-aws 3.4 dropped SDK v1)

---

## MCP tools (for Cursor / Claude Desktop)

Connect to: `http://localhost:8765/sse`

| Tool | What it does |
|------|-------------|
| `ask_kg` | Full RAG + LLM answer with citations. Args: `question`, optional `log`, optional `session_id` |
| `search_kg` | Retrieval only, no LLM. Args: `query`, optional `top_k`. Cheaper for grounding |
| `diagnose_log` | Paste a Spark/Airflow log → root-cause analysis. Args: `log`, optional `context` |

---

## Files to touch for common changes

| Change | Files |
|---|---|
| Add knowledge-base content | Drop file in `data/`, restart `app` |
| Tune retrieval weights | `.env` → `RETRIEVAL_KIND_WEIGHTS` |
| Edit system prompt | `app/prompts/system.md`, restart `app` |
| Change LLM provider | `.env` → `LLM_PROVIDER` + key, restart `app` |
| Fix callback log reading | `demo/dags/callbacks/knowit_callback.py`, restart `airflow-scheduler` |
| Add a new MCP tool | `mcp_server/server.py` (`list_tools` + `call_tool`), rebuild `mcp` |
| Change DAG tasks | `demo/dags/entity_loader_ad_salting_demo.py`, restart `airflow-scheduler` |
| UI changes | `ui/` — bump `?v=N` cache-bust in `index.html`, restart `app` |
