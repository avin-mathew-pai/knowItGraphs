"""Airflow on_failure_callback that posts incident payload to KnowIT.

KnowIT's /api/incidents/ingest endpoint creates a dedicated chat session for
the failure, runs the RAG diagnosis, and streams the reply. The session
appears live in the KnowIT UI sidebar.
"""
from __future__ import annotations

import glob as _glob
import logging
import os
import re
import traceback
from typing import Any, Dict

import requests

log = logging.getLogger(__name__)

KNOWIT_URL = os.environ.get("KNOWIT_URL", "http://app:8080").rstrip("/")
POST_TIMEOUT = float(os.environ.get("KNOWIT_POST_TIMEOUT", "30"))


def _tail_log(task_instance, max_chars: int = 80_000) -> str:
    """Read the Airflow task log and return as much signal as possible.

    Strategy (in order):
      1. Strip Airflow's per-line prefix chrome (`[timestamp] {subprocess.py:106} INFO - `).
      2. Drop Spark's INFO-level progress noise (BlockManager / MemoryStore /
         CodeGenerator / SparkContext / etc.) — ~80% of a typical Spark log.
      3. If filtered log fits under max_chars: return it in full. Nothing lost.
      4. If still oversized: smart-extract the exception block (error + stack
         trace) because the TAIL is usually shutdown spam, not the real error.

    Path discovery order:
      A. attempt=N.log          — Airflow 2.7+ new-style layout
      B. attempt=N-1.log        — off-by-one guard (try_number already incremented?)
      C. glob *.log in task dir — any log file for this dag/run/task
      D. execution_date layout  — pre-2.7 Airflow
      E. log_filepath attr      — legacy Airflow <2.7
    """
    base = "/opt/airflow/logs"
    candidates: list[str] = []

    try:
        dag_id   = task_instance.dag_id
        run_id   = task_instance.run_id
        task_id  = task_instance.task_id
        try_num  = task_instance.try_number
        task_dir = f"{base}/dag_id={dag_id}/run_id={run_id}/task_id={task_id}"

        # A. Primary: attempt=N.log
        candidates.append(f"{task_dir}/attempt={try_num}.log")

        # B. Off-by-one guard: Airflow increments try_number before firing the
        #    callback in some versions — also try N-1 when N > 1.
        if try_num > 1:
            candidates.append(f"{task_dir}/attempt={try_num - 1}.log")

        # C. Glob fallback: picks up whatever .log file is actually there,
        #    regardless of the exact attempt number.
        try:
            matches = sorted(_glob.glob(f"{task_dir}/*.log"))
            for m in matches:
                if m not in candidates:
                    candidates.append(m)
        except Exception as glob_err:
            log.debug("knowit_callback: glob failed — %s", glob_err)

    except Exception as e:
        log.warning("knowit_callback: could not build primary log path — %s", e)

    # D. Pre-2.7 execution_date layout
    try:
        exec_date = getattr(task_instance, "execution_date", None)
        if exec_date:
            candidates.append(
                f"{base}/{task_instance.dag_id}/{task_instance.task_id}"
                f"/{exec_date.isoformat()}/{task_instance.try_number}.log"
            )
    except Exception:
        pass

    # E. Legacy log_filepath attribute (Airflow <2.7)
    try:
        legacy = getattr(task_instance, "log_filepath", None)
        if legacy and legacy not in candidates:
            candidates.append(legacy)
    except Exception:
        pass

    log.info(
        "knowit_callback: searching %d candidate log paths for %s/%s",
        len(candidates),
        getattr(task_instance, "dag_id", "?"),
        getattr(task_instance, "task_id", "?"),
    )

    for p in candidates:
        if not p:
            continue
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                data = f.read()
            if not data.strip():
                log.debug("knowit_callback: %s exists but is empty", p)
                continue
            log.info("knowit_callback: using log %s (%d chars)", p, len(data))
            return _clean_and_trim(data, max_chars)
        except FileNotFoundError:
            log.debug("knowit_callback: not found — %s", p)
            continue
        except Exception as e:
            log.warning("knowit_callback: failed reading %s — %s", p, e)
            continue

    # All paths failed — emit a diagnostic message that shows up in the
    # KnowIT incident session so we know exactly what to fix.
    tried = "\n  - ".join(candidates or ["<none>"])
    diag = (
        f"(task log file not found — paths tried:\n  - {tried}\n\n"
        f"Debug inside the scheduler container:\n"
        f"  docker exec knowit-airflow-scheduler "
        f"find {base} -name '*.log' | head -20)"
    )
    log.warning("knowit_callback: %s", diag)
    return diag


# Markers ordered from MOST specific error start → least specific.
# First hit wins, so we prefer "Exception in thread main" (JVM crash root) over
# a generic "ERROR" line that might come from elsewhere.
_ERROR_MARKERS: tuple[str, ...] = (
    r'Exception in thread "main"',
    r"Traceback \(most recent call last\):",
    r"Caused by:",
    r"ERROR [A-Za-z_.$]+:",
)

# Regex to strip Airflow's per-line framing:
#   [2026-04-24T06:15:54.735+0000] {subprocess.py:106} INFO - <real content>
_AIRFLOW_PREFIX = re.compile(
    r"^\[[\d\-T:\.+]+(?:[+-]\d{4})?\]\s*\{[^}]+\}\s*\w+\s*-\s*",
    re.MULTILINE,
)

# Spark's INFO classes that are pure noise in a failure diagnosis. Keep
# anything NOT in this list — especially anything with ERROR/WARN or bearing
# the word Exception, Error, schema, Config, Cause, Failed, etc.
_SPARK_NOISE_RE = re.compile(
    r"^\d{2}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} INFO "
    r"(?:"
    r"SparkContext|BlockManager|BlockManagerMaster(?:Endpoint)?(?:Heartbeat)?|MemoryStore|MapOutputTracker(?:MasterEndpoint)?|"
    r"CodeGenerator|TaskSetManager|DAGScheduler|TaskSchedulerImpl|SparkUI|JettyUtils|"
    r"NettyBlockTransferService|Utils|ResourceProfile(?:Manager)?|ResourceUtils|SecurityManager|"
    r"StandaloneSchedulerBackend(?:\$\w+)?|StandaloneAppClient(?:\$\w+)?|DiskBlockManager|"
    r"BaseMetastoreCatalog|SharedState|MetricsSystemImpl|ShutdownHookManager|Executor|"
    r"TransportClientFactory|FileSourceStrategy|FileSourceScanExec|InMemoryFileIndex|"
    r"SparkEnv|NativeCodeLoader|CatalogUtil|HybridAnalyzer"
    r")"
    r"(?:\$\w+)?: ",
)

# Also drop these chatty non-Spark lines Airflow itself emits
_OTHER_NOISE_RE = re.compile(
    r"^(?:SLF4J:|WARNING: Using incubator modules|Using Spark's default log4j)",
)


def _strip_noise(log_text: str) -> str:
    """Remove Airflow line chrome and Spark progress spam.
    Preserves every line containing ERROR / WARN / Exception / Caused by /
    stack-trace frames / config dumps / data types."""
    # 1. Flatten Airflow's per-line prefix
    stripped = _AIRFLOW_PREFIX.sub("", log_text)
    # 2. Drop noise lines
    kept: list[str] = []
    for line in stripped.splitlines():
        bare = line.lstrip("\t ")
        if _SPARK_NOISE_RE.match(bare):
            continue
        if _OTHER_NOISE_RE.match(bare):
            continue
        kept.append(line)
    # 3. Collapse 3+ consecutive blank lines
    out = "\n".join(kept)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out


def _clean_and_trim(log_text: str, max_chars: int) -> str:
    """Best-effort: strip noise → if fits, return whole thing; else smart-extract."""
    raw_len = len(log_text)
    cleaned = _strip_noise(log_text)
    cleaned_len = len(cleaned)
    if cleaned_len <= max_chars:
        # All signal preserved; tell the downstream that we cleaned it
        return (
            f"…(cleaned: {raw_len:,} → {cleaned_len:,} chars — removed Spark INFO spam)…\n"
            + cleaned
        )
    # Still too big — extract the error block from the CLEANED log
    return _smart_extract(cleaned, max_chars)


def _smart_extract(log_text: str, max_chars: int) -> str:
    """Return the most useful excerpt of the log for a failure diagnosis.

    Strategy:
      1. Find the FIRST line containing a Java/Python/Scala exception marker.
      2. Return from that line forward (up to max_chars) — that's the real
         error + its stack trace, instead of the tail which is usually
         SparkContext shutdown noise.
      3. Fall back to the last max_chars if no marker found.
    """
    for pattern in _ERROR_MARKERS:
        m = re.search(pattern, log_text)
        if m:
            start = log_text.rfind("\n", 0, m.start()) + 1
            snippet = log_text[start:start + max_chars]
            prefix = f"…(found '{pattern}' — showing {len(snippet)} chars from error)…\n"
            return prefix + snippet
    # No marker — fall back to last N chars
    return (
        f"…(no exception marker found — showing last {max_chars} chars)…\n"
        + log_text[-max_chars:]
    )


def _best_exception_line(log_text: str) -> str:
    """Pull the most informative single error line out of a task log.

    Prefers 'Caused by: …' over 'Exception in thread …' because the former
    is typically the ROOT cause in a Java stack trace (most specific).
    Falls back to first line with 'Exception' or 'Error'.
    """
    # 1. Root cause in Java stack traces
    for m in re.finditer(r"Caused by:\s*([^\n]{10,500})", log_text):
        return m.group(1).strip()
    # 2. Main-thread exceptions
    m = re.search(r'Exception in thread "[^"]+"\s*([^\n]{10,500})', log_text)
    if m:
        return m.group(1).strip()
    # 3. Python tracebacks
    lines = log_text.splitlines()
    for i, line in enumerate(lines):
        if "Traceback (most recent call last)" in line and i + 1 < len(lines):
            # Last line of a traceback is usually "SomeError: message"
            for j in range(i + 1, min(i + 60, len(lines))):
                stripped = lines[j].strip()
                if re.match(r"^\w+(?:\.\w+)*(?:Error|Exception):", stripped):
                    return stripped
    # 4. ERROR <Class>: <message>
    m = re.search(r"ERROR [A-Za-z_.$]+:\s*([^\n]{10,500})", log_text)
    if m:
        return m.group(1).strip()
    return ""


def incident_callback(context: Dict[str, Any]) -> None:
    """Post the failure to KnowIT. Never raise — callback errors would mask
    the real failure in the Airflow UI."""
    try:
        ti = context["task_instance"]
        dag = context["dag"]
        exc = context.get("exception")
        airflow_exc_str = "".join(traceback.format_exception_only(type(exc), exc)).strip() if exc else ""

        log_tail = _tail_log(ti)

        # The real error is in the task log, not in `context["exception"]`
        # (which for a BashOperator is always the generic
        # "AirflowException: Bash command failed. Exit code 1").
        # Extract the actual root cause and promote it into the exception field.
        real_exc = _best_exception_line(log_tail)
        if real_exc:
            exc_str = real_exc
            if airflow_exc_str:
                exc_str = f"{real_exc}\n(Airflow wrapped this as: {airflow_exc_str})"
        else:
            exc_str = airflow_exc_str

        payload = {
            "dag_id":    dag.dag_id,
            "task_id":   ti.task_id,
            "run_id":    getattr(ti, "run_id", None) or str(ti.execution_date),
            "try_number": ti.try_number,
            "log_url":   getattr(ti, "log_url", ""),
            "exception": exc_str,
            "log_tail":  log_tail,
        }
        r = requests.post(
            f"{KNOWIT_URL}/api/incidents/ingest",
            json=payload,
            timeout=POST_TIMEOUT,
        )
        log.info("knowit callback POST → %s %s", r.status_code, r.text[:200])
    except Exception:
        log.exception("knowit incident_callback failed (swallowed)")
