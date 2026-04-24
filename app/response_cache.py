"""Persistent response cache with TTL.

Avoids re-running the LLM for questions we've already answered. Cache key
includes the index content hash, so the cache invalidates automatically when
the handbook or repo is re-indexed.

Design choices:
- Cache keyed on (index_hash, system_prompt_hash, session_history, user_message).
  First-message cross-session hits are possible; mid-conversation replays only
  hit when the full turn sequence is identical.
- Storage: a single pickle file on the same `index_cache` volume as embeddings,
  so the cache survives container restarts.
- Eviction: lazy — expired entries dropped on access and on every write.
"""
from __future__ import annotations

import hashlib
import logging
import pickle
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import SETTINGS

log = logging.getLogger(__name__)


@dataclass
class CacheEntry:
    reply: str
    sources: List[str]
    log_detected: Optional[str]
    retrieved_detail: List[Dict[str, Any]]
    augmented_user: str
    system_prompt: str
    created_at: float = field(default_factory=time.time)
    user_message: str = ""              # kept for /api/cache listing / debugging
    last_accessed: float = field(default_factory=time.time)   # for LRU eviction


class ResponseCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._store: Dict[str, CacheEntry] = {}
        self._path: Path = SETTINGS.index_cache / "response_cache.pkl"
        self._ttl: float = SETTINGS.response_cache_ttl_hours * 3600
        self._enabled: bool = SETTINGS.response_cache_enabled
        self._max_entries: int = max(0, SETTINGS.response_cache_max_entries)
        if self._enabled:
            self._load()

    # ---------- persistence ----------
    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            with self._path.open("rb") as f:
                data = pickle.load(f)
        except Exception as e:
            log.warning("Could not load response cache (%s) — starting empty.", e)
            return
        if not isinstance(data, dict):
            log.warning("Corrupted response cache — starting empty.")
            return
        # Tolerate old-shape entries — keep only those that unpickle correctly
        valid: Dict[str, CacheEntry] = {}
        for k, v in data.items():
            if isinstance(v, CacheEntry):
                valid[k] = v
        self._store = valid
        log.info("Response cache loaded: %d entries", len(self._store))

    def _save(self) -> None:
        if not self._enabled:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".pkl.tmp")
            with tmp.open("wb") as f:
                pickle.dump(self._store, f)
            tmp.replace(self._path)
        except Exception as e:
            log.warning("Could not save response cache: %s", e)

    # ---------- keying ----------
    @staticmethod
    def build_key(
        index_hash: str,
        system_prompt: str,
        session_history: List[Dict[str, str]],
        user_message: str,
    ) -> str:
        h = hashlib.sha256()
        h.update(index_hash.encode())
        h.update(b"\x00")
        h.update(hashlib.sha256(system_prompt.encode("utf-8")).hexdigest().encode())
        h.update(b"\x00")
        for turn in session_history:
            h.update(turn.get("role", "").encode())
            h.update(b"\x01")
            h.update(turn.get("content", "").encode("utf-8"))
            h.update(b"\x02")
        h.update(b"\x03")
        h.update(user_message.encode("utf-8"))
        return h.hexdigest()[:24]

    # ---------- get / put ----------
    def get(self, key: str) -> Optional[CacheEntry]:
        if not self._enabled:
            return None
        with self._lock:
            entry = self._store.get(key)
            if not entry:
                return None
            age = time.time() - entry.created_at
            if age > self._ttl:
                self._store.pop(key, None)
                return None
            entry.last_accessed = time.time()   # touch for LRU tracking
        return entry

    def put(self, key: str, entry: CacheEntry) -> None:
        if not self._enabled:
            return
        with self._lock:
            self._store[key] = entry
            now = time.time()
            # Evict expired first
            expired = [k for k, v in self._store.items() if now - v.created_at > self._ttl]
            for k in expired:
                self._store.pop(k, None)
            # Then cap at max_entries via LRU (oldest last_accessed first)
            if self._max_entries > 0 and len(self._store) > self._max_entries:
                ordered = sorted(self._store.items(), key=lambda kv: kv[1].last_accessed)
                to_drop = len(self._store) - self._max_entries
                for k, _ in ordered[:to_drop]:
                    self._store.pop(k, None)
                log.info("Cache LRU-evicted %d entries (over max=%d)", to_drop, self._max_entries)
            self._save()

    def clear(self) -> int:
        with self._lock:
            n = len(self._store)
            self._store.clear()
            self._save()
        return n

    # ---------- introspection ----------
    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def ttl_seconds(self) -> float:
        return self._ttl

    def stats(self) -> Dict[str, Any]:
        now = time.time()
        with self._lock:
            entries = list(self._store.values())
        total = len(entries)
        ages = [now - e.created_at for e in entries]
        return {
            "enabled": self._enabled,
            "ttl_hours": self._ttl / 3600,
            "max_entries": self._max_entries,
            "entries": total,
            "oldest_age_seconds": max(ages) if ages else 0,
            "newest_age_seconds": min(ages) if ages else 0,
        }


CACHE = ResponseCache()
