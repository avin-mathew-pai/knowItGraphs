# Contributing to KnowIT-Graphs

This guide is written for the **4-member team** who will maintain and extend KnowIT-Graphs. You do **not** need Claude Code or any paid AI tool to do the tasks below. A normal IDE (VS Code, PyCharm, Antigravity, Cursor) is enough.

---

## Typical tasks

### 1. Add / replace the handbook

```bash
cp /path/to/NewHandbook-vX.Y.pdf data/handbook.pdf
bash scripts/reindex.sh
```

### 2. Point at a new or private repo

Edit `.env`:
```
REPO_PATH=/absolute/path/to/another-repo
```
Then `docker compose up -d --build`.

For a **private GitHub repo**, do one of:

- **Option A — clone locally**, point `REPO_PATH` at it. Simplest.
- **Option B — clone inside the container**. Add a build step in `Dockerfile`:
  ```dockerfile
  ARG GH_TOKEN
  RUN git clone https://${GH_TOKEN}@github.com/org/private-repo /mnt/repo
  ```
  Then: `docker compose build --build-arg GH_TOKEN=ghp_xxx`.

### 3. Tweak the system prompt

Edit `app/prompts/system.md`. This is plain Markdown — no code, no AI needed. Restart:
```bash
docker compose restart app
```

Test your change by asking the assistant the same question before and after.

### 4. Change retrieval behaviour

`app/rag.py` — the blend weight at the bottom of `search()`:
```python
combined = 0.55 * dense_norm + 0.45 * bm_norm
```
- **Increase dense** → conceptual / paraphrased questions work better.
- **Increase BM25** → exact error codes, field names, CLI flags work better.
- Change `k` (retrieval_top_k in `.env`) if you want more or fewer cited chunks.

### 5. Add a new LLM provider

`app/llm.py → _resolve_model_and_key()`. Add a branch:
```python
if provider == "mistral":
    if not os.getenv("MISTRAL_API_KEY"):
        raise LLMError("MISTRAL_API_KEY missing.")
    return os.getenv("MISTRAL_MODEL", "mistral/mistral-small"), {}
```
Add the vars to `.env.example`. `litellm` supports most major providers out of the box.

### 6. Extend log parsing

`app/log_parser.py`. Each log type has:
- A list of regex "hint" patterns (to detect it).
- A `LogSignal` field for the extracted identity (DAG name, stage, etc.).

To add e.g. Kafka log support:
1. Add `KAFKA_HINTS = (re.compile(r"org\.apache\.kafka"), ...)`.
2. Extend the `if any(p.search(text) ...` chain in `parse()`.
3. Add a `kafka_topic` field to `LogSignal` if useful.
4. Update `to_prompt_block()`.

### 7. Tighten or loosen scope

`app/prompts/system.md` — the **"Scope — strict"** section. Edit the allowed/disallowed list. Lower temperature in `app/llm.py` (`chat(...)`) if hallucinations reappear.

### 8. Add more redaction rules

`app/guardrails.py → SECRET_PATTERNS`. One line per rule:
```python
(re.compile(r"my-corp-token-[A-Z0-9]{20}"), "<corp-token-redacted>"),
```
Order doesn't matter; all patterns run.

---

## Local dev (without Docker)

```bash
python -m venv .venv
source .venv/bin/activate          # on Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Prewarm embedding model
python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5')"

# Set env & paths (adjust to your machine)
export LLM_PROVIDER=gemini
export GEMINI_API_KEY=AIza...
export HANDBOOK_PDF=./data/handbook.pdf
export REPO_MOUNT=../../sds-solution-ei

uvicorn app.main:app --reload --port 8080
```

Open http://localhost:8080.

---

## Testing a change

There's intentionally no heavy test harness — the product is an assistant, so manual evals are more valuable.

**Smoke-test checklist after any change:**
1. `docker compose up --build -d` succeeds.
2. `GET http://localhost:8080/api/health` returns `status: ok` and `index_size > 0`.
3. UI loads at `http://localhost:8080`.
4. Ask: *"What is the KG platform?"* → answer cites the handbook.
5. Ask: *"How do I reduce latency?"* → answer cites handbook and/or repo.
6. Paste a real Airflow error log → yellow `log detected: airflow` badge appears, answer diagnoses it.
7. Ask: *"Who won the 2022 World Cup?"* → assistant politely refuses (scope check).
8. Answer ends with a `Sources:` section.

---

## Design principles

- **Grounding > cleverness.** If retrieval misses, the assistant must say so — never invent a schema, field name, or file path.
- **Citations always.** The `Sources:` footer is non-negotiable.
- **Minimum tokens.** Hybrid retrieval = few chunks per query. Rolling summary on old turns. No streaming full docs into the model.
- **Pluggable.** No provider-specific code outside `llm.py`. Any team member can switch providers via `.env`.
- **Zero secrets in output.** Redaction runs on every LLM response.

---

## Common "I tried X and it broke" gotchas

| You did | It broke because | Fix |
|---|---|---|
| Changed system prompt, nothing changed | Container caches `SYSTEM_PROMPT` at startup | `docker compose restart app` |
| Added a provider, got `Unknown LLM_PROVIDER` | Typo in `.env` (case-sensitive) | Use lowercase: `gemini`, `anthropic`, etc. |
| Mounted new repo, saw `Indexed 0 chunks` | Path wrong or no supported file types | Check container log; confirm `REPO_PATH` resolves on host |
| Upgraded `litellm` major version | Breaking changes between versions | Pin the version in `requirements.txt` |

---

## Release checklist (for the team lead)

Before announcing a new version to clients:

1. Update `data/handbook.pdf` to latest.
2. Bump version in `app/main.py` (`FastAPI(title=..., version="X.Y.Z")`).
3. Build a fresh image: `docker compose build --no-cache`.
4. Run smoke-test checklist above.
5. Commit `.env.example`, `requirements.txt`, code — never commit your real `.env`.
