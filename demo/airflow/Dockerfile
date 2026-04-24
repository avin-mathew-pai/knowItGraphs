# Airflow 2.10.3 + Spark 4.0.1 client baked in (matches the spark-cluster).
# Tarball from dlcdn.apache.org (fast CDN) with archive.apache.org fallback.
# Iceberg 1.10.0 + hadoop-aws 3.4.1 for MinIO/Iceberg writes.
FROM apache/airflow:2.10.3-python3.11

USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
        openjdk-17-jre-headless curl ca-certificates procps \
    && rm -rf /var/lib/apt/lists/*

ENV SPARK_VERSION=4.0.2 \
    SPARK_HOME=/opt/spark \
    PATH=/opt/spark/bin:$PATH

RUN set -eux; \
    curl -fsSL -o /tmp/spark.tgz \
      "https://dlcdn.apache.org/spark/spark-${SPARK_VERSION}/spark-${SPARK_VERSION}-bin-hadoop3.tgz" \
      || curl -fsSL -o /tmp/spark.tgz \
      "https://archive.apache.org/dist/spark/spark-${SPARK_VERSION}/spark-${SPARK_VERSION}-bin-hadoop3.tgz" ; \
    tar -xzf /tmp/spark.tgz -C /opt ; \
    mv /opt/spark-${SPARK_VERSION}-bin-hadoop3 /opt/spark ; \
    rm /tmp/spark.tgz ; \
    chown -R airflow:root /opt/spark

ARG ICEBERG_VERSION=1.10.0
ARG HADOOP_AWS_VERSION=3.4.1
# hadoop-aws 3.4.x uses AWS SDK v2, not v1.
ARG AWS_SDK_V2_VERSION=2.24.6

RUN set -eux; \
    cd /opt/spark/jars; \
    curl -fsSL -O https://repo1.maven.org/maven2/org/apache/iceberg/iceberg-spark-runtime-4.0_2.13/${ICEBERG_VERSION}/iceberg-spark-runtime-4.0_2.13-${ICEBERG_VERSION}.jar; \
    curl -fsSL -O https://repo1.maven.org/maven2/org/apache/hadoop/hadoop-aws/${HADOOP_AWS_VERSION}/hadoop-aws-${HADOOP_AWS_VERSION}.jar; \
    curl -fsSL -O https://repo1.maven.org/maven2/software/amazon/awssdk/bundle/${AWS_SDK_V2_VERSION}/bundle-${AWS_SDK_V2_VERSION}.jar

USER airflow
COPY requirements.txt /tmp/requirements.txt
RUN if grep -qE '^[[:space:]]*[^[:space:]#]' /tmp/requirements.txt; then \
      pip install --no-cache-dir --user \
        --constraint "https://raw.githubusercontent.com/apache/airflow/constraints-2.10.3/constraints-3.11.txt" \
        -r /tmp/requirements.txt; \
    else \
      echo "[airflow build] requirements.txt has no active pins — skipping pip install."; \
    fi
