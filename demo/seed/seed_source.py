"""Seed the Iceberg source table from the mounted unit-test CSVs.

Mirrors what LoaderUtilsSpec#batchAndIncrementalRun does before calling
Loader.execute:
    - read CSV partitions (header=true, inferSchema=true)
    - filter the garbage row ('when_created_epoch != "Mac os 10"')
    - cast when_created_epoch to long
    - writeTo Iceberg table sdm.microsoft__active_directory

Not a replacement for the Scala Loader — just orchestration-level data prep.
"""
from __future__ import annotations

import sys

from pyspark.sql import SparkSession
from pyspark.sql.functions import col
from pyspark.sql.types import LongType


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: seed_source.py <csv-dir>")
    csv_dir = sys.argv[1]

    spark = (
        SparkSession.builder
        .appName("demo-seed-source")
        .getOrCreate()
    )
    try:
        df = (
            spark.read.option("header", True).option("inferSchema", True)
                 .csv(csv_dir)
                 .filter("when_created_epoch != 'Mac os 10'")
        )
        if "when_created_epoch" in df.columns:
            df = df.withColumn("when_created_epoch", col("when_created_epoch").cast(LongType()))

        spark.sql("CREATE NAMESPACE IF NOT EXISTS iceberg_catalog.sdm")
        spark.sql("CREATE NAMESPACE IF NOT EXISTS iceberg_catalog.ei_temp")
        (df.writeTo("iceberg_catalog.sdm.microsoft__active_directory")
            .using("iceberg")
            .createOrReplace())

        print(f"✔ seeded {df.count()} rows into sdm.microsoft__active_directory")
    finally:
        spark.stop()


if __name__ == "__main__":
    main()
