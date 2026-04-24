"""Output redaction and scope enforcement."""
from __future__ import annotations

import re
from typing import Iterable

# Patterns for secrets that must never leak into chat output
SECRET_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"(?i)(api[_\- ]?key\s*[:=]\s*)([A-Za-z0-9_\-]{16,})"), r"\1<redacted>"),
    (re.compile(r"(?i)(secret\s*[:=]\s*)([A-Za-z0-9_\-/+=]{12,})"),      r"\1<redacted>"),
    (re.compile(r"(?i)(password\s*[:=]\s*)(\S+)"),                         r"\1<redacted>"),
    (re.compile(r"(?i)(token\s*[:=]\s*)([A-Za-z0-9_\-.]{16,})"),          r"\1<redacted>"),
    (re.compile(r"AKIA[0-9A-Z]{16}"),                                      "<aws-access-key-redacted>"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"),                                   "<openai-key-redacted>"),
    (re.compile(r"AIza[0-9A-Za-z\-_]{20,}"),                               "<gemini-key-redacted>"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"),                         "<slack-token-redacted>"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
     "<private-key-redacted>"),
)

LARGE_CODE_BLOCK = re.compile(r"```[\w+\-]*\n([\s\S]*?)```", re.MULTILINE)
MAX_CODE_BLOCK_LINES = 40


def redact(text: str) -> str:
    """Strip credentials, then shrink overly large code dumps."""
    for pat, repl in SECRET_PATTERNS:
        text = pat.sub(repl, text)
    text = _shrink_code_blocks(text)
    return text


def _shrink_code_blocks(text: str) -> str:
    def _replace(match: re.Match) -> str:
        body = match.group(1)
        lines = body.splitlines()
        if len(lines) <= MAX_CODE_BLOCK_LINES:
            return match.group(0)
        keep = lines[:MAX_CODE_BLOCK_LINES]
        keep.append(
            f"\n# ... {len(lines) - MAX_CODE_BLOCK_LINES} more lines omitted — "
            "see cited source file for full code."
        )
        fence = match.group(0).split("\n", 1)[0]
        return f"{fence}\n" + "\n".join(keep) + "\n```"

    return LARGE_CODE_BLOCK.sub(_replace, text)


def format_sources(sources: Iterable[str]) -> str:
    """Render a 'Sources:' footer. Deduped, preserves order."""
    seen = set()
    out = []
    for s in sources:
        if s not in seen:
            seen.add(s)
            out.append(f"- {s}")
    return "\n\nSources:\n" + "\n".join(out) if out else ""
