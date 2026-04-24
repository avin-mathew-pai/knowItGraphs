"""Detect and summarise Airflow / Spark / generic logs pasted by users.

We extract compact signals that steer retrieval and the LLM prompt, without
dumping the full log (which can be huge and noisy).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from .config import SETTINGS

AIRFLOW_HINTS = (
    re.compile(r"airflow", re.I),
    re.compile(r"\bDAG\b"),
    re.compile(r"Task .* (failed|succeeded)"),
    re.compile(r"Marking task as FAILED"),
)

SPARK_HINTS = (
    re.compile(r"org\.apache\.spark"),
    re.compile(r"\bSparkException\b"),
    re.compile(r"at scala\."),
    re.compile(r"Stage \d+ \("),
)

GENERIC_ERROR = re.compile(r"(?im)^(?:.*\b(ERROR|Exception|Traceback|FATAL)\b.*)$")

# Typical exception class pattern, e.g. java.lang.NullPointerException, ValueError
EXCEPTION_CLASS = re.compile(r"\b([A-Za-z_][\w\.]*(?:Exception|Error))\b")

# Airflow-specific task identity
AIRFLOW_TASK = re.compile(r"task_id=([\w\-.]+)")
AIRFLOW_DAG = re.compile(r"dag_id=([\w\-.]+)")

# Spark stage/task
SPARK_STAGE = re.compile(r"Stage (\d+)")
SPARK_TASK = re.compile(r"Task (\d+\.\d+) in stage")


@dataclass
class LogSignal:
    kind: str                                 # "airflow" | "spark" | "generic" | "none"
    exception_classes: List[str] = field(default_factory=list)
    error_lines: List[str] = field(default_factory=list)
    airflow_dag: Optional[str] = None
    airflow_task: Optional[str] = None
    spark_stage: Optional[str] = None
    truncated: bool = False

    def to_prompt_block(self) -> str:
        """Compact summary to inject into the system prompt."""
        if self.kind == "none":
            return ""
        lines = [f"DETECTED LOG TYPE: {self.kind}"]
        if self.airflow_dag:
            lines.append(f"Airflow DAG: {self.airflow_dag}")
        if self.airflow_task:
            lines.append(f"Airflow task: {self.airflow_task}")
        if self.spark_stage:
            lines.append(f"Spark stage: {self.spark_stage}")
        if self.exception_classes:
            lines.append("Exception classes: " + ", ".join(self.exception_classes[:5]))
        if self.error_lines:
            lines.append("Representative error lines:")
            lines.extend(f"  - {ln}" for ln in self.error_lines[:6])
        if self.truncated:
            lines.append("(log was truncated before parsing)")
        return "\n".join(lines)


def looks_like_log(text: str) -> bool:
    if len(text) < 80:
        return False
    return bool(GENERIC_ERROR.search(text) or any(p.search(text) for p in AIRFLOW_HINTS + SPARK_HINTS))


def parse(text: str) -> LogSignal:
    truncated = False
    if len(text) > SETTINGS.max_log_chars:
        text = text[: SETTINGS.max_log_chars]
        truncated = True

    if not looks_like_log(text):
        return LogSignal(kind="none", truncated=truncated)

    if any(p.search(text) for p in AIRFLOW_HINTS):
        kind = "airflow"
    elif any(p.search(text) for p in SPARK_HINTS):
        kind = "spark"
    else:
        kind = "generic"

    error_lines = [m.group(0).strip() for m in GENERIC_ERROR.finditer(text)]
    # Dedupe while preserving order
    seen = set()
    dedup_errors: List[str] = []
    for ln in error_lines:
        key = ln[:200]
        if key not in seen:
            seen.add(key)
            dedup_errors.append(ln[:300])

    exc = []
    seen_exc = set()
    for m in EXCEPTION_CLASS.finditer(text):
        name = m.group(1)
        if name not in seen_exc:
            seen_exc.add(name)
            exc.append(name)

    signal = LogSignal(
        kind=kind,
        exception_classes=exc,
        error_lines=dedup_errors,
        truncated=truncated,
    )
    if kind == "airflow":
        dag = AIRFLOW_DAG.search(text)
        task = AIRFLOW_TASK.search(text)
        if dag:
            signal.airflow_dag = dag.group(1)
        if task:
            signal.airflow_task = task.group(1)
    elif kind == "spark":
        stage = SPARK_STAGE.search(text)
        if stage:
            signal.spark_stage = stage.group(1)
    return signal
