# KnowIT-Graphs — Complete Setup & Demo Runbook

> **Who is this for?** Anyone running the system fresh. Follows every step from bare checkout to live demo.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Repo layout](#2-repo-layout)
3. [First-time setup](#3-first-time-setup)
4. [Choose your LLM provider](#4-choose-your-llm-provider)
5. [Start KnowIT only (no demo)](#5-start-knowit-only-no-demo)
6. [Start the full demo stack](#6-start-the-full-demo-stack)
7. [Run the demo DAG](#7-run-the-demo-dag)
8. [Watch KnowIT diagnose the failure](#8-watch-knowit-diagnose-the-failure)
9. [Reset between demo runs](#9-reset-between-demo-runs)
10. [Day-to-day operations](#10-day-to-day-operations)
11. [Ports reference](#11-ports-reference)
12. [Troubleshooting](#12-troubleshooting)

---

## 1. Prerequisites

| Tool | Minimum version | Notes |
|---|---|---|
| Docker Desktop | 4.28+ | Enable WSL2 backend on Windows |
| Docker Compose | v2 (bundled with Docker Desktop) | use `docker compose`, not `docker-compose` |
| Git | any | to clone |
| LLM API key | — | Gemini free tier works; see §4 |
| `sds-solution-ei` repo | — | checked out at `../sds-solution-ei` relative to this repo |
| `app.jar` | — | `sds-ei-analytics_2.13-1.0.1.jar`, copied into `demo/jars/app.jar` |

### Windows-specific

- Docker Desktop → Settings → Resources → WSL Integration → enable for your distro.
- Use **WSL2 terminal** or PowerShell — avoid Git Bash for `docker compose` commands (TTY issues).

---

## 2. Repo layout

```
knowItGraphs/
├── app/                   # FastAPI backend (RAG, LLM, incidents, agent API)
├── mcp_server/            # MCP server (exposes KnowIT as tools to Cursor / Claude Desktop)
├── ui/                    # Browser UI (3-column: sidebar / retrieval / chat)
├── data/                  # Knowledge base — PDFs, JSON/JSONL, Markdown
│   ├── handbook.pdf
│   ├── kgUserGuide.pdf
│   ├── spark.jsonl
│   └── ...
├── demo/
│   ├── airflow/Dockerfile # Airflow image (2.10.3 + Spark 4 client baked in)
│   ├── spark/Dockerfile   # Spark 4.0.2 standalone image
│   ├── jars/app.jar       # ← you copy the fat JAR here
│   ├── seed/seed_source.py
│   ├── spark-conf/spark-defaults.conf
│   └── dags/
│       ├── entity_loader_ad_salting_demo.py
│       └── callbacks/knowit_callback.py
├── docker-compose.yml          # KnowIT core (app + mcp + optional ollama)
├── docker-compose.demo.yml     # Demo overlay (Airflow + Spark + MinIO + Postgres)
├── .env.example                # Template — copy to .env and fill in
└── RUNBOOK.md                  # ← this file
```

---

## 3. First-time setup

### 3-a. Clone

```bash
git clone <your-repo-url> knowItGraphs
cd knowItGraphs
```

### 3-b. Copy `.env`

```bash
cp .env.example .env
```

### 3-c. Set the LLM provider

Open `.env` and set `LLM_PROVIDER` and the matching key/model. See §4 for options.

### 3-d. Verify `REPO_PATH`

`.env` has:
```
REPO_PATH=../../sds-solution-ei
```
This is relative to `docker-compose.yml`. If `sds-solution-ei` is somewhere else, update it:
```
REPO_PATH=/absolute/path/to/sds-solution-ei
```

Also set `SDS_EI_REPO` in your shell (or add to `.env`) if you want the demo overlay to find the test resources:
```bash
# Windows PowerShell
$env:SDS_EI_REPO = "C:\Users\jithin.kj\Work\Rogue6\sds-solution-ei"

# Linux / WSL2
export SDS_EI_REPO=../../sds-solution-ei
```

Or put it in `.env`:
```
SDS_EI_REPO=../../sds-solution-ei
```

### 3-e. Copy the fat JAR

```bash
# From the sds-solution-ei repo after building with sbt assembly
cp ../sds-solution-ei/analytics/target/scala-2.13/sds-ei-analytics_2.13-1.0.1.jar \
   demo/jars/app.jar
```

If you already have the built JAR somewhere else, just copy it to `demo/jars/app.jar`.

### 3-f. Add knowledge-base files to `data/`

Put any of these in `data/` — they are auto-indexed on startup:

| File | Kind detected |
|---|---|
| `handbook.pdf` | `handbook` |
| `kgUserGuide.pdf` | `kguserguide` |
| `spark.jsonl` | `spark` |
| `*.pdf` | stem name |
| `*.json` / `*.jsonl` | stem name |
| `*.md` | stem name |

KnowIT reads everything in `data/` and the repo at `REPO_PATH`. Re-indexing happens automatically when file content changes (content-hash based cache invalidation).

---

## 4. Choose your LLM provider

Edit `.env` — pick **one** block:

### Option A — Gemini (recommended, free tier)

```dotenv
LLM_PROVIDER=gemini
GEMINI_API_KEY=AIza...your_key_here...
GEMINI_MODEL=gemini/gemini-2.0-flash
```

Get a free key at https://aistudio.google.com/apikey

### Option B — Anthropic

```dotenv
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-sonnet-4-5-20250929
```

### Option C — OpenAI

```dotenv
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini
```

### Option D — Ollama (local, no key, free)

```dotenv
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://ollama:11434
OLLAMA_MODEL=ollama/llama3.1:8b
```

Then start with the `local` profile (pulls Ollama into the compose stack):
```bash
docker compose --profile local up -d
```

After first boot, pull the model:
```bash
docker exec knowit-ollama ollama pull llama3.1:8b
```

---

## 5. Start KnowIT only (no demo)

Use this when you just want the chat assistant — no Airflow/Spark overhead.

```bash
docker compose up -d --build
```

**First boot is slower** — it builds images, embeds all PDFs/JSONLs/repo chunks, and saves the embedding cache. Subsequent starts are ~10 seconds.

Watch the startup:
```bash
docker logs -f knowit-graphs
```

You'll see:
```
[ingest] loaded handbook (347 chunks)
[ingest] loaded kgUserGuide (512 chunks)
[ingest] loaded spark (210 chunks)
[ingest] loaded repo (1840 chunks)
[rag] bundle cache saved → .cache/index/bundle_<hash>.pkl
Uvicorn running on http://0.0.0.0:8080
```

Open the UI: **http://localhost:8080**

---

## 6. Start the full demo stack

This adds MinIO, Postgres, Spark (master + worker), and Airflow (webserver + scheduler) on top of KnowIT.

### 6-a. Build and start everything

```bash
docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d --build
```

**First build downloads Spark 4.0.2 tarball (~300 MB) and 3 JARs — takes 5-15 min depending on network.** Subsequent builds use Docker layer cache and take seconds.

### 6-b. Watch everything come up

```bash
docker compose -f docker-compose.yml -f docker-compose.demo.yml ps
```

Expected healthy state (takes ~2-3 min):

| Container | Status |
|---|---|
| knowit-graphs | running |
| knowit-mcp | running |
| knowit-minio | running (healthy) |
| knowit-minio-init | exited (0) |
| knowit-postgres | running (healthy) |
| knowit-volume-init | exited (0) |
| knowit-spark-master | running |
| knowit-spark-worker | running |
| knowit-airflow-init | exited (0) |
| knowit-airflow-webserver | running (healthy) |
| knowit-airflow-scheduler | running |

If `minio-init` or `airflow-init` exit with code 1, check their logs:
```bash
docker logs knowit-minio-init
docker logs knowit-airflow-init
```

### 6-c. Verify the UIs

| Service | URL | Credentials |
|---|---|---|
| **KnowIT** | http://localhost:8080 | — |
| **Airflow** | http://localhost:8088 | `admin` / `admin` |
| **MinIO console** | http://localhost:9001 | `minio` / `minio12345` |
| **Spark master UI** | http://localhost:8090 | — |

---

## 7. Run the demo DAG

### 7-a. Open Airflow

Go to http://localhost:8088, log in with `admin` / `admin`.

### 7-b. Find the DAG

Search for `entity_loader_ad_salting_demo`. It appears under the **Chats** section with tags `demo`, `knowit`, `loader`, `salting`.

If it doesn't appear within 30 seconds, the scheduler hasn't picked it up yet:
```bash
docker logs knowit-airflow-scheduler | tail -20
```

### 7-c. Unpause and trigger

1. Toggle the DAG to **Unpaused** (the switch on the left).
2. Click the **▶ Trigger DAG** button (play icon, top-right of the DAG row).
3. Confirm with no extra config — click **Trigger**.

### 7-d. Watch the tasks run

Click into the DAG run → **Graph** view:

```
prep_inputs  ──►  seed_source_table  ──►  load_with_salting  ──►  verify_output
   ✅ (green)           ✅ (green)               ❌ (red)               (skipped)
```

- **prep_inputs** (~5s): copies CSV partfiles from test-resources, strips `object_guid` header from one partition.
- **seed_source_table** (~60-90s): Spark job reads the CSVs, writes Iceberg table `sdm.microsoft__active_directory`.
- **load_with_salting** (~2-4 min): Runs your fat JAR (`app.jar`) with `--conf spark.sds.salting=45`. **Fails** with `AnalysisException` because the seeded table has a partition missing `object_guid`.

The failure triggers `incident_callback` automatically.

---

## 8. Watch KnowIT diagnose the failure

### 8-a. Switch to KnowIT

Open http://localhost:8080.

Within a few seconds of the DAG task failing you will see:

- A new session appears in the **left sidebar** with a 🤖 icon and title like `"Airflow incident: entity_loader_ad_salting_demo / load_with_salting"`.
- Click it. The **right chat pane** shows:
  - **🤖 Airflow** bubble: the raw incident details (dag, task, run_id, exception, log excerpt).
  - **🧠 KnowIT** bubble: starts with `⏳ Diagnosing… (0s)` then streams the LLM diagnosis.

### 8-b. What the diagnosis contains

KnowIT automatically:
1. Retrieves relevant handbook chunks, repo code, and Spark error reference entries.
2. Identifies the real root cause (`Caused by:` line from the Spark log — the actual `AnalysisException`, not the generic "Bash command failed").
3. Suggests the fix (schema merge or partition repair).
4. Links to relevant sections from the indexed knowledge base.

### 8-c. Retrieval cards (center panel)

The center panel shows which handbook/repo chunks were retrieved for the diagnosis — click any card to expand it.

### 8-d. Debug modal

Click **⚙ Debug** (top-right of the center panel) to see the full system prompt + augmented user message + all retrieved chunks sent to the LLM.

---

## 9. Reset between demo runs

The demo is designed to fail deterministically. To run it again cleanly:

### Full reset (wipe Iceberg data in MinIO)

```bash
# Delete the Iceberg warehouse from MinIO
docker exec knowit-minio mc alias set local http://localhost:9000 minio minio12345
docker exec knowit-minio mc rm --recursive --force local/demo-kg/iceberg/
```

Or via the MinIO console (http://localhost:9001): browse to `demo-kg/iceberg/` and delete all objects.

### Clear the Airflow run history (optional)

In the Airflow UI: DAG → select all runs → **Delete** (trash icon).

### Re-trigger

Repeat §7-c. The DAG will run end-to-end again and produce a new KnowIT incident session.

---

## 10. Day-to-day operations

### Stop everything

```bash
# KnowIT only
docker compose down

# Full demo stack
docker compose -f docker-compose.yml -f docker-compose.demo.yml down
```

### Restart without rebuilding

```bash
docker compose up -d
# or
docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d
```

### Rebuild one service (after code changes)

```bash
docker compose build app && docker compose up -d app
```

### Re-index after editing `data/` files

Just restart the `app` container — ingest runs on startup and the content-hash cache means only changed files are re-embedded:
```bash
docker compose restart app
```

### Update the callback (after editing `knowit_callback.py`)

The callback runs inside the Airflow scheduler. You must restart it:
```bash
docker compose -f docker-compose.yml -f docker-compose.demo.yml \
  restart airflow-scheduler
```

### View live logs

```bash
# KnowIT app
docker logs -f knowit-graphs

# Airflow scheduler (where callback runs)
docker logs -f knowit-airflow-scheduler

# Specific Spark job output
docker logs knowit-spark-worker
```

### MCP integration (Cursor / Claude Desktop)

The MCP server runs at **http://localhost:8765/sse**.

Add to your MCP client config:
```json
{
  "knowit": {
    "url": "http://localhost:8765/sse"
  }
}
```

Available tools: `ask_kg`, `search_kg`, `diagnose_log`.

---

## 11. Ports reference

| Port | Service | Description |
|---|---|---|
| **8080** | KnowIT UI + API | Main assistant interface |
| **8765** | MCP server | Tool endpoint for Cursor / Claude Desktop |
| **8088** | Airflow webserver | DAG management UI |
| **8090** | Spark master UI | Cluster/job monitoring |
| **9000** | MinIO S3 API | `s3a://` endpoint (internal docker network: `minio:9000`) |
| **9001** | MinIO console | Browser UI for bucket management |
| **7077** | Spark master (internal) | Worker and driver registration |

---

## 12. Troubleshooting

### `docker compose up` fails immediately with image not found

The demo images are built locally — always include `--build` on the first run:
```bash
docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d --build
```

---

### Airflow webserver boot-loops: "Already running on PID N"

This is the stale PID file issue. It's handled in the compose file already (`rm -f airflow-webserver.pid` in the command). If you still see it:
```bash
docker exec knowit-airflow-webserver rm -f /opt/airflow/airflow-webserver.pid
docker compose -f docker-compose.yml -f docker-compose.demo.yml restart airflow-webserver
```

---

### KnowIT UI blank / ERR_EMPTY_RESPONSE on first boot

The app is still embedding. Watch logs:
```bash
docker logs -f knowit-graphs
```
Wait until you see `Uvicorn running on http://0.0.0.0:8080`. The healthcheck `start_period` is 600s to allow for cold embedding.

---

### DAG doesn't appear in Airflow

Scheduler hasn't synced yet, or there's a Python import error in the DAG:
```bash
docker logs knowit-airflow-scheduler | grep -i "entity_loader\|error\|import"
```

---

### `load_with_salting` fails with `NumberFormatException: For input string: "60s"`

You have a JAR version mismatch — `hadoop-aws` version in `demo/spark/Dockerfile` must be **3.4.1** (not 3.3.x). The `--conf` overrides in the DAG (`connection.establish.timeout=30000` etc.) also fix this at runtime.

---

### `NoClassDefFoundError: software/amazon/awssdk/auth/credentials/AwsCredentialsProvider`

The AWS SDK JAR is wrong version. Spark 4 + hadoop-aws 3.4 require the **AWS SDK v2 bundle** (`bundle-2.24.6.jar`), not the v1 `aws-java-sdk-bundle-1.12.x.jar`. Check `demo/spark/Dockerfile` ARGs:
```dockerfile
ARG AWS_SDK_V2_VERSION=2.24.6
```

---

### `prep_inputs` fails with `[Errno 1] Operation not permitted`

The demo workdir has wrong permissions. The `volume-init` service handles this. If it failed:
```bash
docker logs knowit-volume-init
# Then re-run it:
docker compose -f docker-compose.yml -f docker-compose.demo.yml up volume-init
```

---

### KnowIT incident session doesn't appear after DAG failure

1. Check the callback fired:
   ```bash
   docker logs knowit-airflow-scheduler | grep "knowit callback POST"
   ```
2. Check what it posted:
   ```bash
   docker logs knowit-graphs | grep "incidents/ingest"
   ```
3. If `KNOWIT_URL` is wrong — ensure the airflow containers have `KNOWIT_URL=http://app:8080` (set in `docker-compose.demo.yml` under `airflow-env`).

---

### `seed_source_table` fails with S3A / MinIO connection error

MinIO isn't ready yet, or bucket wasn't created. Check:
```bash
docker logs knowit-minio-init
```
Should end with `✔ bucket demo-kg ready`. If it failed, re-run:
```bash
docker compose -f docker-compose.yml -f docker-compose.demo.yml up minio-init
```

---

### Log tail in KnowIT shows shutdown noise instead of the real error

The callback reads the log from the direct Airflow log path and applies noise filtering (strips Spark INFO spam, finds `Exception in thread "main"` marker, extracts `Caused by:` as the exception line). If you see only noise, check:
```bash
docker logs knowit-airflow-scheduler | grep "knowit_callback"
```

The log path used is:
```
/opt/airflow/logs/dag_id=<dag>/run_id=<run>/task_id=<task>/attempt=<N>.log
```

---

### Rebuild Spark image after changing JARs or Dockerfile

```bash
docker compose -f docker-compose.yml -f docker-compose.demo.yml \
  build spark-master
docker compose -f docker-compose.yml -f docker-compose.demo.yml \
  up -d spark-master spark-worker
```

---

### Wipe everything and start fresh

```bash
docker compose -f docker-compose.yml -f docker-compose.demo.yml down -v
docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d --build
```

> ⚠️ `-v` removes all named volumes including `minio_data`, `postgres_data`, `airflow_logs`, `demo_workdir`, `index_cache`. The embedding cache and Airflow history are gone — next boot re-embeds from scratch.
