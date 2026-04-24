"""Persistent thumbs-up / thumbs-down feedback.

Append-only JSONL on the same volume as the response cache so it survives
restarts. Lightweight — no indexing, just raw log for future analysis.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict

from .config import SETTINGS

log = logging.getLogger(__name__)


@dataclass
class FeedbackEntry:
    session_id: str
    message_index: int
    user_message: str
    assistant_reply: str
    helpful: bool
    ts: float


class FeedbackStore:
    def __init__(self) -> None:
        self._path = SETTINGS.index_cache / "feedback.jsonl"
        self._lock = threading.Lock()

    def record(self, entry: FeedbackEntry) -> None:
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
            except Exception as e:
                log.warning("Could not write feedback: %s", e)

    def stats(self) -> Dict[str, Any]:
        if not self._path.exists():
            return {"total": 0, "helpful": 0, "not_helpful": 0}
        helpful = not_helpful = 0
        try:
            with self._path.open("r", encoding="utf-8") as f:
                for line in f:
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    if d.get("helpful"):
                        helpful += 1
                    else:
                        not_helpful += 1
        except Exception as e:
            log.warning("Feedback read failed: %s", e)
        return {
            "total": helpful + not_helpful,
            "helpful": helpful,
            "not_helpful": not_helpful,
        }


FEEDBACK = FeedbackStore()
