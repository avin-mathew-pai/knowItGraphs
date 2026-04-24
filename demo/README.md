# KnowIT-Graphs demo — Airflow pipeline → failure → grounded diagnosis

This overlay adds a **real data pipeline** that KnowIT can diagnose live:

- **Airflow** runs a DAG that spark-submits your analytics JAR
  (`sds-ei-analytics_2.13-1.0.1.jar`) against the Scala unit-test's CSVs and
  JSON config.
- One CSV partition is deliberately corrupted (the `object_guid` header is
  stripped) so the Spark job fails reproducibly with a schema mismatch.
- On failure, the DAG's `on_failure_callback` posts the incident payload to
  **KnowIT's** `/api/incidents/ingest`, which creates a new session, runs
  retrieval, and produces a grounded diagnosis citing `kgUserGuide`,
  `Handbook`, repo, and `spark.jsonl`.
- The session pops live into the **KnowIT UI sidebar** — titled
  `⚠ <dag_id> · <task_id>` — with the Airflow payload rendered as a 🤖 Airflow
  message, followed by the assistant's diagnosis.

---

## Prerequisites

- Docker Desktop (8 GB RAM committed recommended)
- The base KnowIT stack already configured (`.env` with `LLM_PROVIDER` + key)
- `demo/jars/app.jar` — your `sds-ei-analytics_2.13-1.0.1.jar` (already copied)
- Read access to `sds-solution-ei/analytics/src/test/resources/` — set
  `SDS_EI_REPO=/path/to/sds-solution-ei` if it's not at `../sds-solution-ei`

## Start the demo

```bash
bash demo/scripts/demo-start.sh
```
or on Windows:
```powershell
demo\scripts\demo-start.ps1
```

First boot takes ~5 minutes — it builds the custom Airflow + Spark images,
downloads Iceberg / Hadoop-AWS / AWS SDK jars, and initialises the Airflow
metadata DB.

## Run the demo

1. Open **Airflow UI**: <http://localhost:8088> (admin / admin).
2. In the DAGs list, find `entity_loader_ad_salting_demo`. Unpause it (toggle).
3. Click ▶ **Trigger DAG**.
4. Task timeline:
   - `prep_inputs` — ✓ (copies CSVs, strips `object_guid` from one partition)
   - `seed_source_table` — ✓ or ✗ (seeds Iceberg from the corrupted CSVs; may fail here)
   - `load_with_salting` — 🔴 **fails** with `AnalysisException: cannot resolve 'object_guid'`
5. Switch to **KnowIT UI**: <http://localhost:8080>.
   A new session `⚠ entity_loader_ad_salting_demo · load_with_salting` appears in the sidebar within a few seconds. Click it — you'll see:
   - 🤖 **Airflow** message with the failing payload
   - 🧠 **KnowIT** diagnosis with citations to `kgUserGuide`, `Handbook p.27`, repo, and `spark ref:`
   - Click `▸ Prompt details` to inspect retrieved chunks

## Reset between demos

```bash
bash demo/scripts/demo-reset.sh
```
Clears the corrupted workdir, wipes the Iceberg data in MinIO, and removes
incident sessions from KnowIT so the next run starts clean.

## Layout

```
demo/
├── airflow/
│   ├── Dockerfile        # Airflow 2.10.3 + Spark 4.0.1 client baked in
│   └── requirements.txt
├── spark/
│   ├── Dockerfile        # Spark 4.0.1 / Scala 2.13 / Iceberg 1.10.0 / Hadoop-AWS 3.3.4
│   └── entrypoint.sh
├── spark-conf/
│   └── spark-defaults.conf   # Hadoop catalog → s3a://demo-kg/iceberg/
├── dags/
│   ├── entity_loader_ad_salting_demo.py
│   └── callbacks/
│       └── knowit_callback.py    # POST /api/incidents/ingest
├── seed/
│   └── seed_source.py    # CSV → Iceberg sdm.microsoft__active_directory
├── jars/
│   └── app.jar           # your sds-ei-analytics_2.13-1.0.1.jar
├── workdir/              # scratch: corrupted CSVs + copied config (gitignored)
└── scripts/
    ├── demo-start.sh / .ps1
    └── demo-reset.sh
```

## Ports

| Service | URL | Credentials |
|---|---|---|
| KnowIT UI | <http://localhost:8080> | — |
| Airflow | <http://localhost:8088> | admin / admin |
| Spark master UI | <http://localhost:8090> | — |
| MinIO console | <http://localhost:9001> | minio / minio12345 |
| MinIO S3 API | <http://localhost:9000> | minio / minio12345 |

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `demo/jars/app.jar` missing at startup | Copy your jar there: `cp ../sds-ei-analytics_2.13-1.0.1.jar demo/jars/app.jar` |
| Airflow-init crashes with DB errors | `docker compose -f docker-compose.yml -f docker-compose.demo.yml down -v` to wipe postgres volume, then up again |
| `seed_source_table` fails with `NoSuchBucket` | MinIO init race. `docker compose restart minio-init` then re-trigger DAG |
| `No CSV partfiles found` in prep | Wrong `SDS_EI_REPO` mount. Verify: `docker exec knowit-airflow-scheduler ls /mnt/test-resources/entityinventory/loader/activedirectory/microsoft__active_directory/` |
| KnowIT session doesn't auto-appear | Sidebar polls every 15s — give it a few seconds. Check `docker logs knowit-graphs \| grep incidents`. |
| DAG greyed out in Airflow | Unpause with the toggle on the DAG row; may take 30s for scheduler to pick up a new DAG file |

## What's NOT in this demo (Phase 2 backlog)

- Multi-turn agent-to-agent conversation (KnowIT asks AirflowAgent for more context)
- Auto-fix + auto-retry (AirflowAgent applies a proposed fix and retries the task)
- Scenario menu (pick between different failure types from the Airflow UI)
