"""Hybrid retrieval: BM25 (lexical) + dense embeddings (semantic).

On build, embeddings are cached to disk keyed by a hash of all chunk texts.
Subsequent restarts with identical data load instantly instead of re-embedding.
"""
from __future__ import annotations

import hashlib
import logging
import pickle
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
from rank_bm25 import BM25Okapi

from .config import SETTINGS
from .ingest import Chunk

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Retrieved:
    chunk: Chunk
    score: float


def _tokenize(text: str) -> List[str]:
    return re.findall(r"[A-Za-z0-9_]+", text.lower())


def _content_hash(chunks: List[Chunk]) -> str:
    h = hashlib.sha256()
    h.update(str(len(chunks)).encode())
    for c in chunks:
        h.update(c.source.encode("utf-8"))
        h.update(b"\x00")
        h.update(c.text.encode("utf-8"))
        h.update(b"\x01")
    return h.hexdigest()[:16]


class HybridIndex:
    """Lexical + dense retrieval. Thread-safe for concurrent queries after build()."""

    def __init__(self) -> None:
        self._chunks: List[Chunk] = []
        self._bm25: Optional[BM25Okapi] = None
        self._embeddings: Optional[np.ndarray] = None
        self._kind_boost: Optional[np.ndarray] = None   # per-chunk score multiplier
        self._embedder = None
        self._content_hash: str = ""
        self._lock = threading.Lock()
        self._ready = False

    def build(self, chunks: List[Chunk]) -> None:
        if not chunks:
            log.warning("HybridIndex.build called with zero chunks — retrieval disabled.")
            return

        with self._lock:
            self._chunks = chunks
            self._content_hash = _content_hash(chunks)

            from fastembed import TextEmbedding
            self._embedder = TextEmbedding("BAAI/bge-small-en-v1.5")

            cached = self._load_cached_bundle(chunks)

            # Embeddings: load from cache or compute
            if cached and cached.get("embeddings") is not None:
                self._embeddings = cached["embeddings"]
            else:
                self._embeddings = self._embed_in_batches([c.text for c in chunks])

            # BM25: load from cache if available, otherwise build
            if cached and cached.get("bm25") is not None:
                self._bm25 = cached["bm25"]
                log.info("BM25 restored from cache — skipped tokenisation.")
            else:
                t0 = time.monotonic()
                tokenised = [_tokenize(c.text) for c in chunks]
                self._bm25 = BM25Okapi(tokenised)
                log.info("BM25 built in %.2fs", time.monotonic() - t0)

            # Persist as bundle if anything was recomputed (saves next boot).
            if not cached or cached.get("bm25") is None:
                self._save_bundle(chunks, self._embeddings, self._bm25)

            # Kind boost is ALWAYS recomputed from env vars — cheap (~10ms at
            # 2000 chunks) and lets users retune RETRIEVAL_KIND_WEIGHTS
            # without invalidating the expensive embedding cache.
            overrides = _parse_kind_weights_env()
            self._kind_boost = np.array(
                [_weight_for_kind(c.kind, overrides) for c in chunks],
                dtype="float32",
            )
            unique_kinds = sorted({c.kind for c in chunks})
            effective = {k: _weight_for_kind(k, overrides) for k in unique_kinds}
            log.info("Retrieval kind weights: %s", effective)

            self._ready = True

        log.info("HybridIndex ready with %d chunks.", len(chunks))

    # ---------- embedding + BM25 bundle caching ----------
    # Bundle format v2 stores embeddings AND the BM25Okapi instance together
    # so warm restarts skip the ~2s BM25 build. Old single-ndarray pickles
    # are still readable (legacy path) — they just won't accelerate BM25.
    BUNDLE_VERSION = 2

    def _cache_path(self, chunks: List[Chunk]) -> Path:
        cache_dir = SETTINGS.index_cache
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir / f"embeddings_{_content_hash(chunks)}.pkl"

    def _load_cached_bundle(self, chunks: List[Chunk]) -> Optional[dict]:
        """Returns {'embeddings': ndarray, 'bm25': BM25Okapi|None} or None."""
        path = self._cache_path(chunks)
        if not path.exists():
            log.info("No bundle cache at %s — will compute fresh.", path)
            return None
        try:
            with path.open("rb") as f:
                data = pickle.load(f)
        except Exception as e:
            log.warning("Failed to load cache (%s) — recomputing.", e)
            return None

        # Legacy format: raw ndarray (v1)
        if isinstance(data, np.ndarray):
            if data.shape[0] != len(chunks):
                log.warning("Legacy cache shape mismatch — recomputing.")
                return None
            log.info("Loaded legacy (v1) embedding cache: %d vectors from %s", data.shape[0], path.name)
            return {"embeddings": data, "bm25": None}

        # New format: dict bundle (v2+)
        if isinstance(data, dict) and data.get("version") == self.BUNDLE_VERSION:
            emb = data.get("embeddings")
            if not isinstance(emb, np.ndarray) or emb.shape[0] != len(chunks):
                log.warning("Bundle shape mismatch — recomputing.")
                return None
            log.info("Loaded bundle cache (v%d): %d vectors + BM25 from %s",
                     self.BUNDLE_VERSION, emb.shape[0], path.name)
            return {"embeddings": emb, "bm25": data.get("bm25")}

        log.warning("Unknown cache format — recomputing.")
        return None

    def _save_bundle(self, chunks: List[Chunk], embeddings: np.ndarray, bm25: BM25Okapi) -> None:
        try:
            path = self._cache_path(chunks)
            for old in path.parent.glob("embeddings_*.pkl"):
                if old != path:
                    try:
                        old.unlink()
                    except OSError:
                        pass
            bundle = {
                "version": self.BUNDLE_VERSION,
                "embeddings": embeddings,
                "bm25": bm25,
            }
            with path.open("wb") as f:
                pickle.dump(bundle, f)
            log.info("Saved bundle cache (embeddings + BM25) → %s", path.name)
        except Exception as e:
            log.warning("Could not persist bundle cache: %s", e)

    def _embed_in_batches(self, texts: List[str]) -> np.ndarray:
        total = len(texts)
        batch = SETTINGS.embed_batch_size
        log.info("Embedding %d chunks in batches of %d…", total, batch)
        start = time.monotonic()
        vectors: List[np.ndarray] = []
        next_log = max(batch, total // 10)
        done = 0
        for i in range(0, total, batch):
            part = texts[i:i + batch]
            batch_vecs = list(self._embedder.embed(part))
            vectors.extend(batch_vecs)
            done += len(part)
            if done >= next_log or done == total:
                elapsed = time.monotonic() - start
                rate = done / elapsed if elapsed > 0 else 0
                remaining = (total - done) / rate if rate > 0 else 0
                log.info(
                    "  embedded %d/%d (%.0f/sec, ~%ds remaining)",
                    done, total, rate, int(remaining),
                )
                next_log = done + max(batch, total // 10)
        vecs = np.array(vectors, dtype="float32")
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return vecs / norms

    # ---------- runtime queries ----------
    @property
    def size(self) -> int:
        return len(self._chunks)

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def content_hash(self) -> str:
        """Hash of current indexed content. Used as a response-cache key
        component so cache invalidates automatically when the corpus changes."""
        return self._content_hash

    def _score_all(self, query: str) -> np.ndarray:
        q_tokens = _tokenize(query)
        bm_scores = (
            np.array(self._bm25.get_scores(q_tokens), dtype="float32")
            if q_tokens else np.zeros(len(self._chunks), dtype="float32")
        )
        bm_norm = _normalise(bm_scores)

        q_vec = np.array(list(self._embedder.embed([query])), dtype="float32")[0]
        q_norm = np.linalg.norm(q_vec) or 1.0
        q_vec = q_vec / q_norm
        dense_scores = self._embeddings @ q_vec
        dense_norm = _normalise(dense_scores)

        combined = 0.55 * dense_norm + 0.45 * bm_norm
        if self._kind_boost is not None:
            combined = combined * self._kind_boost
        return combined

    def search(self, query: str, k: int = 6, per_kind: int = 3) -> List[Retrieved]:
        """Balanced hybrid retrieval.

        Returns up to `per_kind` best chunks from each `kind` (handbook, repo),
        then tops up with global best until `k` items are reached. This prevents
        a corpus of prose (handbook) from crowding out code (repo) chunks for
        natural-language queries, or vice-versa.
        """
        if not self._ready or not query.strip():
            return []

        scores = self._score_all(query)
        order = np.argsort(scores)[::-1]

        selected_idx: List[int] = []
        seen: set[int] = set()
        kind_counts: Dict[str, int] = {}

        # Pass 1: reserve up to `per_kind` per kind, taken in global-score order
        for i in order:
            idx = int(i)
            kind = self._chunks[idx].kind
            if kind_counts.get(kind, 0) >= per_kind:
                continue
            selected_idx.append(idx)
            seen.add(idx)
            kind_counts[kind] = kind_counts.get(kind, 0) + 1
            if len(selected_idx) >= k:
                break

        # Pass 2: fill remaining slots with next-best overall, regardless of kind
        if len(selected_idx) < k:
            for i in order:
                idx = int(i)
                if idx in seen:
                    continue
                selected_idx.append(idx)
                seen.add(idx)
                if len(selected_idx) >= k:
                    break

        # Preserve descending combined score order in the final list
        selected_idx.sort(key=lambda j: -scores[j])
        return [
            Retrieved(chunk=self._chunks[j], score=float(scores[j]))
            for j in selected_idx
        ]


def _normalise(arr: np.ndarray) -> np.ndarray:
    lo, hi = float(arr.min()), float(arr.max())
    if hi - lo < 1e-9:
        return np.zeros_like(arr)
    return (arr - lo) / (hi - lo)


def _parse_kind_weights_env() -> Dict[str, float]:
    """Read RETRIEVAL_KIND_WEIGHTS='userguide:1.5,handbook:1.2,repo:1.0'.
    Empty / unset → no overrides; defaults kick in via _weight_for_kind()."""
    import os
    raw = os.getenv("RETRIEVAL_KIND_WEIGHTS", "").strip()
    out: Dict[str, float] = {}
    if not raw:
        return out
    for pair in raw.split(","):
        if ":" not in pair:
            continue
        k, v = pair.split(":", 1)
        try:
            out[k.strip().lower()] = float(v.strip())
        except ValueError:
            continue
    return out


def _weight_for_kind(kind: str, overrides: Dict[str, float]) -> float:
    """Resolve a kind → retrieval-score multiplier.

    Priority order (highest weight surfaces first on close-score ties):
      userguide / guide / tutorial / manual   → 1.5  (user perspective)
      repo (source code)                      → 1.3  (implementation truth)
      handbook / developer docs               → 1.15 (canonical schema / field defs)
      spark / error references                → 1.05 (specialised fallback)
      unknown kind                            → 1.0  (neutral)

    Overrides (from env RETRIEVAL_KIND_WEIGHTS) always win. Substring matching
    lets filenames like 'kgUserGuide', 'user_guide', 'kg_handbook' map
    automatically without extra config.
    """
    if kind in overrides:
        return overrides[kind]
    k = kind.lower()
    if any(s in k for s in ("guide", "manual", "tutorial", "walkthrough")) \
            and "handbook" not in k and "developer" not in k:
        return 1.5
    if k == "repo":
        return 1.3
    if "handbook" in k or "developer" in k or "docs" in k:
        return 1.15
    if "spark" in k or "error" in k:
        return 1.05
    # Fallback: respect legacy RETRIEVAL_CODE_BOOST if someone set it
    import os
    try:
        legacy = float(os.getenv("RETRIEVAL_CODE_BOOST", "1.0"))
    except ValueError:
        legacy = 1.0
    return legacy if k != "handbook" else 1.0
