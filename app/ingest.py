"""Extracts text from the handbook PDF and Solution EI repo into retrievable chunks."""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, List

import pdfplumber

from .config import SETTINGS

log = logging.getLogger(__name__)

# High-signal extensions only. YAML/JSON configs and .sh are excluded from the
# default scan because they tend to be noisy and inflate the index (see
# "max_repo_chunks"). Re-enable via CONTRIBUTING guide if needed.
REPO_INCLUDE_EXT = {".py", ".md", ".sql"}

REPO_SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__",
    "dist", "build", ".pytest_cache", ".mypy_cache", ".idea", ".vscode",
    "target", "out", "coverage", "htmlcov",
}
MAX_FILE_BYTES = 200_000
CHUNK_CHARS = 2000
CHUNK_OVERLAP = 250


@dataclass(frozen=True)
class Chunk:
    """A single retrievable passage with provenance."""
    text: str
    source: str       # human-readable citation (e.g. "Handbook p.7" or "repo: path/file.py")
    kind: str         # "handbook" | "repo"


def _window(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> Iterable[str]:
    """Sliding window that prefers paragraph / sentence / whitespace boundaries
    so chunks don't end mid-word or mid-sentence."""
    text = text.strip()
    if not text:
        return
    if len(text) <= size:
        yield text
        return
    step = max(1, size - overlap)
    i = 0
    n = len(text)
    while i < n:
        end = i + size
        if end >= n:
            piece = text[i:].strip()
            if piece:
                yield piece
            return
        # Pull the end backwards to the nearest nice boundary, searching the
        # last `size - step` chars (i.e. within the overlap region).
        search_from = i + step
        break_at = -1
        for delim in ("\n\n", ". ", "\n", " "):
            idx = text.rfind(delim, search_from, end)
            if idx > break_at:
                break_at = idx + len(delim)
                break
        if break_at <= i + step:
            break_at = end   # no good boundary; fall back to hard cut
        piece = text[i:break_at].strip()
        if piece:
            yield piece
        i = max(i + step, break_at - overlap)


def _kind_from_filename(stem: str) -> str:
    """File stem → lowercased kind tag used for retrieval weighting."""
    return stem.lower()


def _display_prefix(stem: str) -> str:
    """Human-friendly prefix for citations, e.g. 'kgUserGuide' → 'kgUserGuide p.7'.
    Preserves the original camel/snake case of the filename."""
    return stem


def load_pdf(pdf_path: Path) -> List[Chunk]:
    """Extract per-page text from one PDF as retrievable chunks.

    Also flags pages that contain images so the model can tell the user
    "see the diagram on p.X" even though we can't OCR the image itself.
    """
    if not pdf_path.exists():
        log.warning("PDF not found at %s — skipping.", pdf_path)
        return []
    kind = _kind_from_filename(pdf_path.stem)
    prefix = _display_prefix(pdf_path.stem)
    chunks: List[Chunk] = []
    n_pages = 0
    n_image_pages = 0
    try:
        pdf = pdfplumber.open(str(pdf_path))
    except Exception as e:
        log.error("Failed to open PDF %s: %s", pdf_path.name, e)
        return []
    try:
        for page_idx, page in enumerate(pdf.pages, start=1):
            n_pages += 1
            try:
                raw = page.extract_text() or ""
            except Exception as e:
                log.warning("%s page %d text extract failed: %s", pdf_path.name, page_idx, e)
                raw = ""
            raw = raw.strip()

            # Detect images / diagrams on the page
            try:
                images = page.images or []
            except Exception:
                images = []
            if images:
                n_image_pages += 1
                hint = (
                    f"\n\n[This page contains {len(images)} image(s)/diagram(s) — not available "
                    f"as text. Cite this page and tell the user to consult {prefix} p.{page_idx} "
                    "visually for the diagram.]"
                )
                raw = (raw + hint).strip() if raw else hint.strip()

            if not raw:
                continue
            for piece in _window(raw):
                chunks.append(Chunk(
                    text=piece,
                    source=f"{prefix} p.{page_idx}",
                    kind=kind,
                ))
    finally:
        pdf.close()
    log.info("%s (kind=%s): %d chunks from %d pages (%d pages with images)",
             pdf_path.name, kind, len(chunks), n_pages, n_image_pages)
    return chunks


def load_pdfs_from_dir(data_dir: Path) -> List[Chunk]:
    """Load every *.pdf in data_dir. kind = lowercased filename stem, so
    dropping in userguide.pdf / kgUserGuide.pdf / architecture.pdf etc.
    just works — no code changes, no config."""
    if not data_dir.exists():
        log.warning("Data dir %s not found — skipping PDFs.", data_dir)
        return []
    out: List[Chunk] = []
    for path in sorted(data_dir.glob("*.pdf")):
        out.extend(load_pdf(path))
    if not out:
        log.warning("No PDFs found under %s — retrieval will only have repo/references.", data_dir)
    return out


def load_repo(repo_root: Path) -> List[Chunk]:
    if not repo_root.exists():
        log.warning("Repo mount not found at %s — skipping.", repo_root)
        return []
    cap = SETTINGS.max_repo_chunks
    chunks: List[Chunk] = []
    files_seen = 0
    truncated = False
    for path in repo_root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in REPO_SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() not in REPO_INCLUDE_EXT:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size == 0 or size > MAX_FILE_BYTES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        rel = path.relative_to(repo_root).as_posix()
        files_seen += 1
        for piece in _window(text):
            chunks.append(Chunk(
                text=piece,
                source=f"repo: {rel}",
                kind="repo",
            ))
            if len(chunks) >= cap:
                truncated = True
                break
        if truncated:
            break
    suffix = " (TRUNCATED — raise MAX_REPO_CHUNKS to index more)" if truncated else ""
    log.info("Repo: %d chunks across %d files%s", len(chunks), files_seen, suffix)
    return chunks


def _stringify_entry(entry: Any, indent: int = 0) -> str:
    """Render any JSON value as readable key/value text for chunking.

    Handles nested dicts/lists; skips None/empty values to keep chunks dense.
    """
    pad = "  " * indent
    if entry is None:
        return ""
    if isinstance(entry, dict):
        lines: List[str] = []
        for k, v in entry.items():
            if v is None or v == "" or v == [] or v == {}:
                continue
            if isinstance(v, (dict, list, tuple)):
                lines.append(f"{pad}{k}:")
                nested = _stringify_entry(v, indent + 1)
                if nested:
                    lines.append(nested)
            else:
                lines.append(f"{pad}{k}: {v}")
        return "\n".join(lines)
    if isinstance(entry, (list, tuple)):
        lines = []
        for item in entry:
            if isinstance(item, (dict, list, tuple)):
                nested = _stringify_entry(item, indent + 1)
                if nested:
                    lines.append(f"{pad}-")
                    lines.append(nested)
            elif item is not None and item != "":
                lines.append(f"{pad}- {item}")
        return "\n".join(lines)
    return f"{pad}{entry}"


def _entry_title(entry: Any, fallback: str) -> str:
    if isinstance(entry, dict):
        for key in ("topic", "title", "name", "pattern", "id"):
            if key in entry and isinstance(entry[key], str) and entry[key].strip():
                return entry[key].strip()
    return fallback


def _unwrap_json_doc(doc: Any) -> List[Any]:
    """Normalise a parsed JSON document into a list of entries."""
    if isinstance(doc, list):
        return doc
    if isinstance(doc, dict):
        entries = doc.get("entries")
        if isinstance(entries, list):
            return entries
        collected: List[Any] = []
        for ek, ev in doc.items():
            if ek in ("kind", "description", "source", "version", "entries", "meta"):
                continue
            if isinstance(ev, dict):
                ev = {"title": ek, **{kk: vv for kk, vv in ev.items() if kk != "title"}}
                collected.append(ev)
        if collected:
            return collected
        return [doc]
    return []


def _load_json_file(path: Path) -> List[Any]:
    """Parse a .json or .jsonl file into a list of top-level entries.

    Uses a streaming JSON decoder (``json.JSONDecoder.raw_decode``) so it
    handles any sequential-JSON shape uniformly — whether the file is:
      - JSONL with compact one-per-line records,
      - JSONL with multi-line pretty-printed records (newlines inside fields),
      - concatenated JSON without separators,
      - a single JSON array or object.

    Web-scraped datasets frequently have embedded newlines inside string
    fields (HTML snippets, markdown content), which breaks naive per-line
    parsing. The streaming decoder skips that problem entirely.
    """
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        log.warning("Reference %s: cannot read — %s", path.name, e)
        return []
    # Strip UTF-8 BOM if present
    if raw.startswith("\ufeff"):
        raw = raw[1:]
    raw = raw.strip()
    if not raw:
        log.warning("Reference %s: file is empty.", path.name)
        return []

    preview = raw[:180].replace("\n", "\\n")
    log.info("Reference %s: %d bytes, starts with: %s%s",
             path.name, len(raw), preview, "…" if len(raw) > 180 else "")

    # Stream-parse: extract every top-level JSON value in order.
    decoder = json.JSONDecoder()
    values: List[Any] = []
    idx = 0
    n = len(raw)
    last_error: str = ""
    while idx < n:
        # Skip whitespace / separators between values
        while idx < n and raw[idx] in " \t\r\n,":
            idx += 1
        if idx >= n:
            break
        try:
            obj, end = decoder.raw_decode(raw, idx)
        except json.JSONDecodeError as e:
            last_error = f"at char {idx} ({e.msg})"
            break
        values.append(obj)
        idx = end

    if not values:
        log.warning("Reference %s: no valid JSON values found%s",
                    path.name, f" — stopped {last_error}" if last_error else "")
        return []

    # If the file was a single array or object, unwrap to a list of entries.
    if len(values) == 1:
        entries = _unwrap_json_doc(values[0])
        log.info("%s: parsed as single JSON document → %d entries.",
                 path.name, len(entries))
        return entries

    # Otherwise treat each parsed top-level value as an entry (JSONL / concatenated).
    suffix_note = f" (stopped {last_error})" if last_error else ""
    log.info("%s: stream-parsed %d entries%s.", path.name, len(values), suffix_note)
    return values


def _kind_for_reference_file(path: Path) -> str:
    """Kind comes from the JSON's top-level 'kind' if present, otherwise the filename stem."""
    if path.suffix.lower() == ".json":
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(doc, dict) and isinstance(doc.get("kind"), str):
                return doc["kind"].strip().lower()
        except Exception:
            pass
    return path.stem.lower()


def load_references(data_dir: Path) -> List[Chunk]:
    """Load curated reference files from <data>/references/*.{json,jsonl}.

    - kind = filename stem (or the top-level "kind" field in a JSON doc).
    - Each entry is rendered as readable key/value text and indexed as a chunk.
    """
    ref_dir = data_dir / "references"
    if not ref_dir.exists():
        log.info("No references dir at %s — skipping.", ref_dir)
        return []
    chunks: List[Chunk] = []
    files_seen = 0
    files_found = list(ref_dir.glob("*.json")) + list(ref_dir.glob("*.jsonl"))
    for path in sorted(files_found):
        entries = _load_json_file(path)
        if not entries:
            log.warning("Reference %s: no entries extracted.", path.name)
            continue
        files_seen += 1
        kind = _kind_for_reference_file(path)
        for i, entry in enumerate(entries):
            text = _stringify_entry(entry)
            if not text.strip():
                continue
            title = _entry_title(entry, fallback=f"entry[{i}]")
            source = f"{kind} ref: {title}"
            for piece in _window(text):
                chunks.append(Chunk(text=piece, source=source, kind=kind))
    log.info("References: %d chunks from %d file(s) under %s", len(chunks), files_seen, ref_dir)
    return chunks


def load_all(data_dir: Path, repo_root: Path) -> List[Chunk]:
    """Full ingest: all PDFs + repo code + JSON references — loaded in parallel.

    The three loaders are independent and spend most of their wall time in IO
    (rglob, read_text, pdfminer-c-extensions). Running them in a small thread
    pool cuts cold-boot ingest by ~30%. If any loader raises it's logged but
    the others still return their share — partial index is better than none.
    """
    def _safe(fn, *args, label: str):
        try:
            return fn(*args)
        except Exception as e:
            log.exception("Loader %s crashed: %s", label, e)
            return []

    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="ingest") as ex:
        f_pdfs = ex.submit(_safe, load_pdfs_from_dir, data_dir, label="pdfs")
        f_repo = ex.submit(_safe, load_repo, repo_root, label="repo")
        f_refs = ex.submit(_safe, load_references, data_dir, label="refs")
        pdfs = f_pdfs.result()
        repo = f_repo.result()
        refs = f_refs.result()
    return pdfs + repo + refs
