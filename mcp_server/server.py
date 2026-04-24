"""MCP server for KnowIT-Graphs.

Exposes three tools to any MCP-speaking client (Cursor, Claude Desktop, etc.)
over HTTP+SSE. Each tool call forwards to the main KnowIT-Graphs app via HTTP,
so this process stays tiny (~150 LOC) and shares the app's RAG + cache + LLM
pipeline with zero duplication.

Run: uvicorn server:app --host 0.0.0.0 --port 8765   (or just `python server.py`)
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List

import httpx
import uvicorn
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import TextContent, Tool
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("knowit-mcp")

KNOWIT_API_URL = os.getenv("KNOWIT_API_URL", "http://app:8080").rstrip("/")
AGENT_API_TOKEN = os.getenv("AGENT_API_TOKEN", "").strip()
MCP_PORT = int(os.getenv("MCP_PORT", "8765"))
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "120"))


def _auth_headers() -> Dict[str, str]:
    if AGENT_API_TOKEN:
        return {"Authorization": f"Bearer {AGENT_API_TOKEN}"}
    return {}


# ---------- MCP server ----------
server = Server("knowit-graphs")


@server.list_tools()
async def list_tools() -> List[Tool]:
    return [
        Tool(
            name="ask_kg",
            description=(
                "Ask the KnowIT-Graphs assistant a question about the Knowledge Graph / "
                "Solution EI platform. Returns a grounded, concise answer with citations "
                "(handbook pages, repo files, spark reference) and, when relevant, a "
                "Mermaid diagram describing the flow. Use this for any KG concept, "
                "error code, schema, feature, or 'how do I integrate X' question."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The question to ask.",
                    },
                    "log": {
                        "type": "string",
                        "description": "Optional: an Airflow / Spark / generic pipeline log snippet to diagnose alongside the question.",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Optional: reuse a prior session to preserve conversation context.",
                    },
                },
                "required": ["question"],
            },
        ),
        Tool(
            name="search_kg",
            description=(
                "Retrieval only — returns the top relevant chunks from the KG handbook, "
                "Solution EI repo, and curated reference data (e.g. spark.json) for a "
                "query. Does NOT call an LLM. Use when the calling agent wants to reason "
                "over the raw grounding context itself, or when you want a cheaper "
                "lookup without generation cost."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query.",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "How many chunks to return (default 6, max 20).",
                        "default": 6,
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="diagnose_log",
            description=(
                "Specialised log diagnosis: paste an Airflow task log or a Spark stack "
                "trace and get a concise root-cause + fix analysis grounded in the KG "
                "handbook, Solution EI repo, and Spark reference data."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "log": {
                        "type": "string",
                        "description": "The raw log text to analyse.",
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional: what the user was trying to do (e.g. 'running disambiguation DAG').",
                    },
                },
                "required": ["log"],
            },
        ),
    ]


async def _post_upstream(client: httpx.AsyncClient, path: str, payload: dict) -> dict:
    """POST to the app with visible error handling. Raises RuntimeError with an
    actionable message that gets surfaced back to the MCP client."""
    try:
        r = await client.post(f"{KNOWIT_API_URL}{path}", json=payload, headers=_auth_headers())
    except httpx.TimeoutException:
        raise RuntimeError(
            f"Upstream `{path}` timed out after {HTTP_TIMEOUT}s. The LLM provider "
            "(likely Ollama on CPU) is slower than this MCP timeout. Options: "
            "(1) call `search_kg` instead (retrieval only, no LLM), "
            "(2) switch LLM_PROVIDER=gemini in .env for ~10x faster replies, "
            "(3) raise HTTP_TIMEOUT on the mcp container."
        )
    except httpx.ConnectError as e:
        raise RuntimeError(f"Cannot reach upstream at {KNOWIT_API_URL}{path}: {e}")
    if r.status_code == 401:
        raise RuntimeError("Upstream rejected auth — set AGENT_API_TOKEN on both containers or leave both blank.")
    if r.status_code >= 500:
        raise RuntimeError(f"Upstream error {r.status_code}: {r.text[:400]}")
    if r.status_code >= 400:
        raise RuntimeError(f"Upstream rejected request {r.status_code}: {r.text[:400]}")
    return r.json()


@server.call_tool()
async def call_tool(name: str, arguments: Dict[str, Any]) -> List[TextContent]:
    arg_preview = {k: (v[:80] + "…") if isinstance(v, str) and len(v) > 80 else v for k, v in arguments.items()}
    log.info("tool_call start: %s args=%s", name, arg_preview)
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            if name == "ask_kg":
                payload: Dict[str, Any] = {"question": arguments["question"]}
                if arguments.get("log"):
                    payload["log"] = arguments["log"]
                if arguments.get("session_id"):
                    payload["session_id"] = arguments["session_id"]
                data = await _post_upstream(client, "/api/agent/ask", payload)
                text = _format_ask_response(data)

            elif name == "diagnose_log":
                log_text = arguments["log"]
                ctx = arguments.get("context", "")
                question = (
                    f"Diagnose this log and suggest a fix. Context: {ctx}"
                    if ctx else "Diagnose this log and suggest a fix."
                )
                data = await _post_upstream(
                    client, "/api/agent/ask",
                    {"question": question, "log": log_text},
                )
                text = _format_ask_response(data)

            elif name == "search_kg":
                data = await _post_upstream(
                    client, "/api/agent/search",
                    {"query": arguments["query"], "top_k": arguments.get("top_k", 6)},
                )
                text = _format_search_response(data)

            else:
                text = f"Unknown tool: {name}"
    except RuntimeError as e:
        log.warning("tool_call FAILED: %s after %.1fs: %s", name, time.monotonic() - t0, e)
        return [TextContent(type="text", text=f"⚠ KnowIT-Graphs error: {e}")]
    except Exception as e:
        log.exception("tool_call CRASH: %s after %.1fs", name, time.monotonic() - t0)
        return [TextContent(type="text", text=f"⚠ KnowIT-Graphs unexpected error: {e}")]

    log.info("tool_call ok: %s in %.1fs", name, time.monotonic() - t0)
    return [TextContent(type="text", text=text)]


# ---------- formatters ----------
def _format_ask_response(d: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append(d.get("answer", "").strip())
    if d.get("diagram"):
        # Already included as a mermaid fence inside `answer`; surfacing again
        # would be noisy. Skip unless the answer somehow lacked the fence.
        if "```mermaid" not in d.get("answer", ""):
            lines.append("\n```mermaid\n" + d["diagram"].strip() + "\n```")
    if d.get("sources"):
        lines.append("\n---")
        lines.append("**Cited sources**")
        for s in d["sources"]:
            lines.append(f"- `{s.get('kind', '?').upper()}` {s.get('ref')} (score {s.get('score'):.2f})")
    meta_bits = []
    if d.get("cached"):
        age = d.get("cache_age_seconds", 0)
        meta_bits.append(f"cached (age {_fmt_age(age)})")
    if d.get("log_detected"):
        meta_bits.append(f"log: {d['log_detected']}")
    model = d.get("model", {})
    if model:
        meta_bits.append(f"model: {model.get('provider', '?')}/{model.get('model', '?')}")
    if meta_bits:
        lines.append(f"\n_Meta: {' · '.join(meta_bits)}_")
    return "\n".join(lines).strip()


def _format_search_response(d: Dict[str, Any]) -> str:
    lines = [f"**Top {len(d.get('chunks', []))} chunks for:** {d.get('query')}"]
    for c in d.get("chunks", []):
        lines.append(f"\n### `{c.get('kind', '?').upper()}` {c.get('ref')} (score {c.get('score'):.2f})\n```\n{c.get('text', '')}\n```")
    return "\n".join(lines)


def _fmt_age(seconds: int) -> str:
    if seconds < 60:    return f"{seconds}s"
    if seconds < 3600:  return f"{seconds // 60}m"
    if seconds < 86400: return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


# ---------- HTTP (SSE) transport ----------
sse = SseServerTransport("/messages/")


class _NoopResponse(Response):
    """Starlette >=0.38 always calls `await response(scope, receive, send)` after
    a route endpoint returns.  For SSE, the transport already sent headers and
    the full response body before we get here, so we must NOT write to `send`
    again — doing so would raise a double-send error in Uvicorn.
    This stub satisfies the Starlette contract without touching the connection."""
    async def __call__(self, scope, receive, send) -> None:  # type: ignore[override]
        pass  # SSE session already closed cleanly by SseServerTransport


async def handle_sse(request: Request) -> _NoopResponse:
    async with sse.connect_sse(request.scope, request.receive, request._send) as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())
    return _NoopResponse()


async def health(_: Request):
    # Relay upstream health so ops can see MCP <-> app connectivity in one call
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{KNOWIT_API_URL}/api/health")
            return JSONResponse({"mcp": "ok", "upstream": r.json()})
    except Exception as e:
        return JSONResponse({"mcp": "ok", "upstream_error": str(e)}, status_code=503)


app = Starlette(routes=[
    Route("/sse", endpoint=handle_sse),
    Mount("/messages/", app=sse.handle_post_message),
    Route("/health", endpoint=health),
])


if __name__ == "__main__":
    log.info("Starting KnowIT-Graphs MCP server on port %d (upstream=%s)", MCP_PORT, KNOWIT_API_URL)
    uvicorn.run(app, host="0.0.0.0", port=MCP_PORT, log_level="info")
