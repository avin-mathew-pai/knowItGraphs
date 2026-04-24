"""Central config loaded from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


def _float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    provider: str
    gemini_api_key: str
    gemini_model: str
    anthropic_api_key: str
    anthropic_model: str
    openai_api_key: str
    openai_model: str
    ollama_base_url: str
    ollama_model: str

    session_ttl_min: int
    max_history_turns: int
    retrieval_top_k: int
    retrieval_per_kind: int
    retrieval_code_boost: float
    max_log_chars: int
    max_repo_chunks: int
    embed_batch_size: int
    response_cache_enabled: bool
    response_cache_ttl_hours: float
    response_cache_max_entries: int

    handbook_pdf: Path
    repo_mount: Path
    index_cache: Path


def load() -> Settings:
    return Settings(
        provider=_env("LLM_PROVIDER", "gemini").lower(),
        gemini_api_key=_env("GEMINI_API_KEY"),
        gemini_model=_env("GEMINI_MODEL", "gemini/gemini-2.0-flash"),
        anthropic_api_key=_env("ANTHROPIC_API_KEY"),
        anthropic_model=_env("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929"),
        openai_api_key=_env("OPENAI_API_KEY"),
        openai_model=_env("OPENAI_MODEL", "gpt-4o-mini"),
        ollama_base_url=_env("OLLAMA_BASE_URL", "http://ollama:11434"),
        ollama_model=_env("OLLAMA_MODEL", "ollama/llama3.1:8b"),
        session_ttl_min=_int("SESSION_TTL_MIN", 30),
        max_history_turns=_int("MAX_HISTORY_TURNS", 10),
        retrieval_top_k=_int("RETRIEVAL_TOP_K", 6),
        retrieval_per_kind=_int("RETRIEVAL_PER_KIND", 3),
        retrieval_code_boost=_float("RETRIEVAL_CODE_BOOST", 1.35),
        response_cache_enabled=_env("RESPONSE_CACHE_ENABLED", "true").lower() in ("1", "true", "yes"),
        response_cache_ttl_hours=_float("RESPONSE_CACHE_TTL_HOURS", 24),
        response_cache_max_entries=_int("RESPONSE_CACHE_MAX_ENTRIES", 500),
        max_log_chars=_int("MAX_LOG_CHARS", 20000),
        max_repo_chunks=_int("MAX_REPO_CHUNKS", 2000),
        embed_batch_size=_int("EMBED_BATCH_SIZE", 32),
        handbook_pdf=Path(_env("HANDBOOK_PDF", "/app/data/handbook.pdf")),
        repo_mount=Path(_env("REPO_MOUNT", "/mnt/repo")),
        index_cache=Path(_env("INDEX_CACHE", "/app/.cache/index")),
    )


SETTINGS = load()
