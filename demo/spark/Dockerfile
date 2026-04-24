# Spark 4.0.1 / Scala 2.13 / Java 17 — matching user's production stack.
# Tarball pulled from Apache's active CDN (dlcdn.apache.org), with the slow
# archive.apache.org as fallback. Iceberg 1.10.0 + hadoop-aws 3.4.1 (matches
# Spark 4.0's bundled Hadoop 3.4.x core — avoids the "60s" NumberFormatException
# seen with the older hadoop-aws 3.3.x).
FROM eclipse-temurin:17-jre-noble

ENV SPARK_VERSION=4.0.2 \
    SPARK_HOME=/opt/spark \
    PATH=/opt/spark/bin:/opt/spark/sbin:$PATH \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates procps tini python3 python3-pip bash \
    && rm -rf /var/lib/apt/lists/*

# Primary: dlcdn.apache.org (fast CDN). Fallback: archive.apache.org.
RUN set -eux; \
    curl -fsSL -o /tmp/spark.tgz \
      "https://dlcdn.apache.org/spark/spark-${SPARK_VERSION}/spark-${SPARK_VERSION}-bin-hadoop3.tgz" \
      || curl -fsSL -o /tmp/spark.tgz \
      "https://archive.apache.org/dist/spark/spark-${SPARK_VERSION}/spark-${SPARK_VERSION}-bin-hadoop3.tgz" ; \
    tar -xzf /tmp/spark.tgz -C /opt ; \
    mv /opt/spark-${SPARK_VERSION}-bin-hadoop3 /opt/spark ; \
    rm /tmp/spark.tgz

# Iceberg + hadoop-aws + AWS SDK. ARGs so bumping versions only re-runs this
# layer — the heavy Spark tarball download stays cached.
ARG ICEBERG_VERSION=1.10.0
ARG HADOOP_AWS_VERSION=3.4.1
# hadoop-aws 3.4.x uses AWS SDK v2 (software.amazon.awssdk), not v1
# (com.amazonaws). The `bundle` artifact contains all needed modules.
ARG AWS_SDK_V2_VERSION=2.24.6

RUN set -eux; \
    cd /opt/spark/jars; \
    curl -fsSL -O https://repo1.maven.org/maven2/org/apache/iceberg/iceberg-spark-runtime-4.0_2.13/${ICEBERG_VERSION}/iceberg-spark-runtime-4.0_2.13-${ICEBERG_VERSION}.jar; \
    curl -fsSL -O https://repo1.maven.org/maven2/org/apache/hadoop/hadoop-aws/${HADOOP_AWS_VERSION}/hadoop-aws-${HADOOP_AWS_VERSION}.jar; \
    curl -fsSL -O https://repo1.maven.org/maven2/software/amazon/awssdk/bundle/${AWS_SDK_V2_VERSION}/bundle-${AWS_SDK_V2_VERSION}.jar

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 7077 8080 4040

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
