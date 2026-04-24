"""entity_loader_ad_salting_demo — the end-to-end demo DAG.

Three tasks:
  1. prep_inputs        — copy test CSVs into the shared workdir and strip the
                          `object_guid` column from ONE partfile (deterministic
                          deliberate failure).
  2. seed_source_table  — spark-submit seed_source.py → loads the (now
                          corrupted) CSVs into Iceberg sdm.microsoft__active_directory.
  3. kg_extractor       — spark-submit the user-provided analytics JAR,
                          class ai.prevalent.entityinventory.loader.Loader
                          with --conf spark.sds.salting=45 and the same CLI
                          args the unit-test uses. FAILS on the schema merge
                          because the corrupted partition drops object_guid.

On failure, incident_callback posts the payload to KnowIT
(http://app:8080/api/incidents/ingest) which creates a diagnosis session.
"""
from __future__ import annotations

import os
import shutil
import sys
from datetime import datetime

from airflow import DAG
from airflow.operators.bash import BashOperator
from airflow.operators.python import PythonOperator

# Make the callbacks/ sibling package importable without installing it
sys.path.insert(0, os.path.dirname(__file__))
from callbacks.knowit_callback import incident_callback  # noqa: E402


# ---------- constants ----------
WORKDIR        = "/mnt/demo/workdir"
TEST_RESOURCES = "/mnt/test-resources/entityinventory/loader/activedirectory"
CSV_SUBDIR     = "microsoft__active_directory"
CONFIG_NAME    = "sds_ei__host__active_directory__object_guid__job_config.json"
JAR_PATH       = "/mnt/demo/jars/app.jar"

SPARK_MASTER   = os.environ.get("SPARK_MASTER_URL", "spark://spark-master:7077")

_COMMON_CONF = " ".join([
    f"--master {SPARK_MASTER}",
    "--deploy-mode client",
    "--conf spark.sql.extensions=org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions",
    "--conf spark.sql.catalog.iceberg_catalog=org.apache.iceberg.spark.SparkCatalog",
    "--conf spark.sql.catalog.iceberg_catalog.type=hadoop",
    "--conf spark.sql.catalog.iceberg_catalog.warehouse=s3a://demo-kg/iceberg/",
    "--conf spark.sql.defaultCatalog=iceberg_catalog",
    "--conf spark.hadoop.fs.s3a.endpoint=http://minio:9000",
    "--conf spark.hadoop.fs.s3a.access.key=minio",
    "--conf spark.hadoop.fs.s3a.secret.key=minio12345",
    "--conf spark.hadoop.fs.s3a.path.style.access=true",
    "--conf spark.hadoop.fs.s3a.connection.ssl.enabled=false",
    "--conf spark.hadoop.fs.s3a.impl=org.apache.hadoop.fs.s3a.S3AFileSystem",
    # Hadoop 3.4 core parses these as integers (ms), but hadoop-aws 3.3's
    # stale defaults write "60s"/"200s" → NumberFormatException. Override
    # with numeric milliseconds.
    "--conf spark.hadoop.fs.s3a.connection.establish.timeout=30000",
    "--conf spark.hadoop.fs.s3a.connection.timeout=200000",
    "--conf spark.hadoop.fs.s3a.connection.request.timeout=0",
    "--conf spark.hadoop.fs.s3a.multipart.purge.age=86400",
    "--conf spark.sql.iceberg.merge-schema=true",
    "--conf spark.sql.iceberg.check-ordering=false",
    "--conf spark.sql.session.timeZone=UTC",
])


# ---------- task 1: prep (deterministic corruption) ----------
def prep_inputs_func(**_: object) -> None:
    src_csv = f"{TEST_RESOURCES}/{CSV_SUBDIR}"
    src_cfg = f"{TEST_RESOURCES}/{CONFIG_NAME}"
    dst_csv = f"{WORKDIR}/{CSV_SUBDIR}"
    dst_cfg = f"{WORKDIR}/{CONFIG_NAME}"

    os.makedirs(WORKDIR, exist_ok=True)
    if os.path.isdir(dst_csv):
        shutil.rmtree(dst_csv)
    # IMPORTANT: use shutil.copy (content-only), NOT the default copy2.
    # On Windows/WSL2/Docker-Desktop bind mounts, copying file metadata
    # across the mount boundary raises `[Errno 1] Operation not permitted`.
    shutil.copytree(src_csv, dst_csv, copy_function=shutil.copy)
    shutil.copy(src_cfg, dst_cfg)

    # Corrupt ONE partfile — strip 'object_guid' from its header. Spark's
    # Iceberg schema merge / Loader validation then fails because the column
    # referenced by the loader config is missing from that partition.
    partfiles = sorted(p for p in os.listdir(dst_csv) if p.endswith(".csv"))
    if not partfiles:
        raise RuntimeError(f"No CSV partfiles found under {dst_csv}")
    target = os.path.join(dst_csv, partfiles[0])
    with open(target, "r", encoding="utf-8") as f:
        lines = f.readlines()
    header = lines[0].rstrip("\r\n")
    cols = [c for c in header.split(",") if c != "object_guid"]
    if len(cols) == header.count(",") + 1:
        raise RuntimeError(
            f"Expected to strip 'object_guid' from header but it was already absent: {header!r}"
        )
    lines[0] = ",".join(cols) + "\n"
    with open(target, "w", encoding="utf-8") as f:
        f.writelines(lines)

    print(f"✔ copied CSVs + config to {WORKDIR}")
    print(f"✔ stripped 'object_guid' from header of {partfiles[0]}")
    print(f"  new header: {lines[0].strip()}")


# ---------- bash commands ----------
SEED_CMD = (
    f"spark-submit {_COMMON_CONF} "
    f"/mnt/demo/seed/seed_source.py "
    f"file://{WORKDIR}/{CSV_SUBDIR}"
)

LOADER_CMD = (
    f"spark-submit {_COMMON_CONF} "
    f"--conf spark.sds.salting=45 "
    f"--class ai.prevalent.entityinventory.loader.Loader "
    f"{JAR_PATH} "
    f"--parsed-interval-start 0000000000000 "
    f"--parsed-interval-end 1681862399999 "
    f"--current-updated-date 1681862399999 "
    f"--config-path file://{WORKDIR}/{CONFIG_NAME} "
    f"--source-path sdm.microsoft__active_directory "
    f"--inventory-path ei_temp.sds_ei__host__active_directory__object_guid "
    f"--previous-updated-date -1 "
    f"--srdm-historical-parsed-interval-start 0"
)


default_args = {
    "owner": "demo",
    "retries": 0,
    "on_failure_callback": incident_callback,
}

with DAG(
    dag_id="entity_loader_ad_salting_demo",
    description="KnowIT demo: AD host loader with salting=45, deliberate schema failure.",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    default_args=default_args,
    tags=["demo", "knowit", "loader", "salting"],
) as dag:

    prep = PythonOperator(
        task_id="prep_inputs",
        python_callable=prep_inputs_func,
    )

    seed = BashOperator(
        task_id="seed_source_table",
        bash_command=SEED_CMD,
    )

    load = BashOperator(
        task_id="kg_extractor",
        bash_command=LOADER_CMD,
    )

    verify = BashOperator(
        task_id="verify_output",
        bash_command=(
            "echo 'If we reached here the loader succeeded — the demo failure "
            "path would have aborted before this task.'"
        ),
    )

    prep >> seed >> load >> verify
