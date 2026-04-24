"""FastAPI entrypoint for KnowIT-Graphs."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import guardrails, ingest, log_parser
from .config import SETTINGS
from .feedback import FEEDBACK, FeedbackEntry
from .llm import LLMError, chat_stream, describe_provider
from .rag import HybridIndex
from .response_cache import CACHE, CacheEntry
from .sessions import STORE, Session


def _sse(event: str, data: dict) -> str:
    """Encode a Server-Sent Event frame."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("knowit-graphs")

INDEX = HybridIndex()
SYSTEM_PROMPT = Path(__file__).parent.joinpath("prompts", "system.md").read_text(encoding="utf-8")


async def _periodic_sweeper() -> None:
    """Background task to drop idle sessions."""
    while True:
        await asyncio.sleep(60)
        try:
            removed = STORE.sweep_expired()
            if removed:
                log.info("Swept %d expired session(s).", removed)
        except Exception:
            log.exception("Session sweeper error.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Provider: %s", describe_provider())
    data_dir = SETTINGS.handbook_pdf.parent
    log.info("Indexing PDFs in %s, repo (%s), references (%s/references)…",
             data_dir, SETTINGS.repo_mount, data_dir)
    chunks = ingest.load_all(data_dir, SETTINGS.repo_mount)
    # Run the heavy embedding build off the event loop so startup stays responsive to health checks
    await asyncio.to_thread(INDEX.build, chunks)
    log.info("Ready. Indexed %d chunks.", INDEX.size)

    sweeper_task = asyncio.create_task(_periodic_sweeper())
    try:
        yield
    finally:
        sweeper_task.cancel()


app = FastAPI(title="KnowIT-Graphs", version="1.0.0", lifespan=lifespan)

# Static UI
UI_DIR = Path(__file__).resolve().parent.parent / "ui"
app.mount("/static", StaticFiles(directory=str(UI_DIR)), name="static")


# ---------- API models ----------
class ChatRequest(BaseModel):
    session_id: Optional[str] = None
    message: str = Field(min_length=1, max_length=40_000)


class SessionSummary(BaseModel):
    id: str
    title: str
    last_active: float
    message_count: int




# ---------- Routes ----------
@app.get("/")
async def root():
    return FileResponse(str(UI_DIR / "index.html"))


def _extract_mermaid(text: str) -> Optional[str]:
    """Pull the first ```mermaid``` block out of an answer for agent consumers
    that want to render it separately from the prose."""
    import re
    m = re.search(r"```mermaid\s*\n([\s\S]*?)```", text)
    return m.group(1).strip() if m else None


def _require_agent_auth(authorization: Optional[str]) -> None:
    """Enforce bearer-token auth on the agent endpoints if AGENT_API_TOKEN
    is set in the environment. No-op otherwise (local-only default)."""
    import os
    token = os.getenv("AGENT_API_TOKEN", "").strip()
    if not token:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing bearer token.")
    if authorization[7:].strip() != token:
        raise HTTPException(401, "Invalid bearer token.")


@app.get("/api/health")
async def health():
    return {
        "status": "ok" if INDEX.ready else "indexing",
        "index_size": INDEX.size,
        "cache": CACHE.stats(),
        **describe_provider(),
    }


@app.get("/api/cache")
async def cache_stats():
    return CACHE.stats()


@app.delete("/api/cache")
async def cache_clear():
    n = CACHE.clear()
    return {"cleared": n}


@app.get("/api/sessions")
async def list_sessions() -> List[SessionSummary]:
    return [
        SessionSummary(
            id=s.id,
            title=s.title,
            last_active=s.last_active,
            message_count=len(s.messages),
        )
        for s in STORE.list()
    ]


@app.post("/api/sessions")
async def new_session() -> SessionSummary:
    s = STORE.create()
    return SessionSummary(id=s.id, title=s.title, last_active=s.last_active, message_count=0)


@app.delete("/api/sessions/{sid}")
async def delete_session(sid: str):
    if not STORE.delete(sid):
        raise HTTPException(404, "Session not found")
    return {"deleted": sid}


@app.post("/api/chat")
async def chat_endpoint(req: ChatRequest):
    """Streams the LLM response as Server-Sent Events.

    Event order: meta → status ("retrieving" / "generating") → token* → final → done
    Errors: any pre-flight failure is an HTTPException; mid-stream LLM failure
    is sent as an `error` SSE frame and the stream closes cleanly.
    """
    session = STORE.get(req.session_id) if req.session_id else STORE.create()
    if session is None:
        raise HTTPException(404, "Session not found or expired. Start a new chat.")

    if not session.messages and session.title == "New chat":
        session.title = (req.message[:60] + "…") if len(req.message) > 60 else req.message

    async def event_stream():
        yield _sse("meta", {"session_id": session.id})

        # Build cache key from current state — cheap, no retrieval needed yet.
        session_history = [
            {"role": m.role, "content": m.content} for m in session.messages
        ]
        cache_key = CACHE.build_key(
            index_hash=INDEX.content_hash,
            system_prompt=SYSTEM_PROMPT,
            session_history=session_history,
            user_message=req.message,
        )
        cached = CACHE.get(cache_key)

        if cached:
            # Cache hit — replay the stored answer instantly. No LLM, no retrieval.
            age_s = int(time.time() - cached.created_at)
            session.add("user", req.message)
            session.add("assistant", cached.reply)
            yield _sse("context", {
                "log_detected": cached.log_detected,
                "sources": cached.sources,
                "cached": True,
                "age_seconds": age_s,
            })
            yield _sse("prompt", {
                "system": cached.system_prompt,
                "augmented_user": cached.augmented_user,
                "retrieved": cached.retrieved_detail,
                "log_signal": None,
                "model": describe_provider(),
                "messages_count": len(session.messages) + 1,
                "cached": True,
                "age_seconds": age_s,
            })
            yield _sse("final", {"text": cached.reply, "cached": True, "age_seconds": age_s})
            yield _sse("done", {})
            return

        # Cache miss — full pipeline.
        yield _sse("status", {"phase": "retrieving", "text": "Searching handbook + repo…"})

        signal = log_parser.parse(req.message)
        log_block = signal.to_prompt_block()

        query_parts = [req.message]
        if signal.exception_classes:
            query_parts.extend(signal.exception_classes[:3])
        hits = INDEX.search(
            " ".join(query_parts),
            k=SETTINGS.retrieval_top_k,
            per_kind=SETTINGS.retrieval_per_kind,
        )
        context_block = _format_retrieved(hits)
        sources = [h.chunk.source for h in hits]
        retrieved_detail = [
            {
                "source": h.chunk.source,
                "score": round(h.score, 3),
                "kind": h.chunk.kind,
                "snippet": h.chunk.text,
            }
            for h in hits
        ]

        from collections import Counter
        kind_counts = dict(Counter(h.chunk.kind for h in hits))
        yield _sse("context", {
            "log_detected": signal.kind if signal.kind != "none" else None,
            "sources": sources,
            "kind_counts": kind_counts,
            "cached": False,
        })

        parts: List[str] = []
        if log_block:
            parts.append(f"[LOG SIGNAL]\n{log_block}")
        if context_block:
            parts.append(f"[RETRIEVED CONTEXT]\n{context_block}")
        parts.append(f"[USER QUESTION]\n{req.message}")
        augmented = "\n\n".join(parts)

        session.add("user", req.message)
        llm_messages = session.build_llm_messages(SYSTEM_PROMPT)
        llm_messages[-1] = {"role": "user", "content": augmented}

        yield _sse("prompt", {
            "system": SYSTEM_PROMPT,
            "augmented_user": augmented,
            "retrieved": retrieved_detail,
            "log_signal": log_block or None,
            "model": describe_provider(),
            "messages_count": len(llm_messages),
            "cached": False,
        })

        yield _sse("status", {"phase": "generating", "text": f"Generating via {describe_provider()['provider']}…"})

        buffer: List[str] = []
        try:
            async for token in chat_stream(llm_messages):
                buffer.append(token)
                yield _sse("token", {"text": token})
        except LLMError as e:
            if session.messages and session.messages[-1].role == "user":
                session.messages.pop()
            yield _sse("error", {"message": str(e)})
            yield _sse("done", {})
            return
        except asyncio.CancelledError:
            # Client hit Stop (or disconnected). Keep whatever we generated so
            # the session transcript reflects what the user saw, but do NOT
            # cache partial answers — they could mislead on a later cache hit.
            if buffer:
                partial = guardrails.redact("".join(buffer)).rstrip()
                if partial:
                    partial += "\n\n_[stopped by user — partial response]_"
                    session.add("assistant", partial)
            elif session.messages and session.messages[-1].role == "user":
                session.messages.pop()
            raise

        full = "".join(buffer)
        final = guardrails.redact(full)
        if "Sources:" not in final:
            final = final.rstrip() + guardrails.format_sources(sources)
        session.add("assistant", final)

        # Persist retrieval detail on the assistant message so reloading the
        # session later still shows the chunk cards in the center panel.
        if session.messages:
            session.messages[-1].retrieved = retrieved_detail

        # Store in cache for future identical queries.
        CACHE.put(cache_key, CacheEntry(
            reply=final,
            sources=sources,
            log_detected=signal.kind if signal.kind != "none" else None,
            retrieved_detail=retrieved_detail,
            augmented_user=augmented,
            system_prompt=SYSTEM_PROMPT,
            user_message=req.message,
        ))

        yield _sse("final", {"text": final, "cached": False})
        yield _sse("done", {})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class AgentAskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=40_000)
    log: Optional[str] = Field(default=None, max_length=40_000)
    session_id: Optional[str] = None
    top_k: Optional[int] = Field(default=None, ge=1, le=20)


class AgentSource(BaseModel):
    kind: str
    ref: str
    score: float
    snippet: str


class AgentAskResponse(BaseModel):
    answer: str
    diagram: Optional[str]
    sources: List[AgentSource]
    log_detected: Optional[str]
    cached: bool
    cache_age_seconds: int
    session_id: str
    model: Dict[str, str]
    retrieved_count: int


class AgentSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    top_k: Optional[int] = Field(default=None, ge=1, le=20)


class AgentSearchChunk(BaseModel):
    kind: str
    ref: str
    score: float
    text: str


class AgentSearchResponse(BaseModel):
    query: str
    chunks: List[AgentSearchChunk]


@app.post("/api/agent/ask", response_model=AgentAskResponse)
async def agent_ask(
    req: AgentAskRequest,
    authorization: Optional[str] = Header(default=None),
):
    """Non-streaming, structured JSON endpoint for agents.

    Same RAG + guardrails + cache pipeline as the streaming chat, but returns
    one clean JSON document that any agent framework can parse.
    """
    _require_agent_auth(authorization)

    # Compose user message: question with optional log prepended for analysis
    user_message = req.question
    if req.log:
        user_message = f"{req.log.strip()}\n\n{req.question}"

    session = STORE.get(req.session_id) if req.session_id else STORE.create(title="agent")
    if session is None:
        raise HTTPException(404, "Session not found or expired.")

    signal = log_parser.parse(user_message)
    log_block = signal.to_prompt_block()

    top_k = req.top_k or SETTINGS.retrieval_top_k
    query_parts = [user_message]
    if signal.exception_classes:
        query_parts.extend(signal.exception_classes[:3])
    hits = INDEX.search(
        " ".join(query_parts),
        k=top_k,
        per_kind=SETTINGS.retrieval_per_kind,
    )
    sources_detail = [h.chunk.source for h in hits]
    retrieved_detail = [
        {
            "source": h.chunk.source,
            "score": round(h.score, 3),
            "kind": h.chunk.kind,
            "snippet": h.chunk.text,
        }
        for h in hits
    ]

    parts: List[str] = []
    if log_block:
        parts.append(f"[LOG SIGNAL]\n{log_block}")
    context_block = _format_retrieved(hits)
    if context_block:
        parts.append(f"[RETRIEVED CONTEXT]\n{context_block}")
    parts.append(f"[USER QUESTION]\n{user_message}")
    augmented = "\n\n".join(parts)

    session_history = [
        {"role": m.role, "content": m.content} for m in session.messages
    ]
    cache_key = CACHE.build_key(
        index_hash=INDEX.content_hash,
        system_prompt=SYSTEM_PROMPT,
        session_history=session_history,
        user_message=user_message,
    )
    cached = CACHE.get(cache_key)
    if cached:
        session.add("user", user_message)
        session.add("assistant", cached.reply)
        return AgentAskResponse(
            answer=cached.reply,
            diagram=_extract_mermaid(cached.reply),
            sources=[
                AgentSource(kind=c.get("kind", "?"), ref=c.get("source", ""), score=c.get("score", 0.0), snippet=c.get("snippet", ""))
                for c in cached.retrieved_detail
            ],
            log_detected=cached.log_detected,
            cached=True,
            cache_age_seconds=int(time.time() - cached.created_at),
            session_id=session.id,
            model=describe_provider(),
            retrieved_count=len(cached.retrieved_detail),
        )

    session.add("user", user_message)
    llm_messages = session.build_llm_messages(SYSTEM_PROMPT)
    llm_messages[-1] = {"role": "user", "content": augmented}

    # Collect non-streaming: we still call chat_stream but exhaust it here
    buffer: List[str] = []
    try:
        async for token in chat_stream(llm_messages):
            buffer.append(token)
    except LLMError as e:
        if session.messages and session.messages[-1].role == "user":
            session.messages.pop()
        raise HTTPException(502, str(e))

    full = "".join(buffer)
    final = guardrails.redact(full)
    if "Sources:" not in final:
        final = final.rstrip() + guardrails.format_sources(sources_detail)
    session.add("assistant", final)

    CACHE.put(cache_key, CacheEntry(
        reply=final,
        sources=sources_detail,
        log_detected=signal.kind if signal.kind != "none" else None,
        retrieved_detail=retrieved_detail,
        augmented_user=augmented,
        system_prompt=SYSTEM_PROMPT,
        user_message=user_message,
    ))

    return AgentAskResponse(
        answer=final,
        diagram=_extract_mermaid(final),
        sources=[
            AgentSource(kind=h.chunk.kind, ref=h.chunk.source, score=round(h.score, 3), snippet=h.chunk.text)
            for h in hits
        ],
        log_detected=signal.kind if signal.kind != "none" else None,
        cached=False,
        cache_age_seconds=0,
        session_id=session.id,
        model=describe_provider(),
        retrieved_count=len(hits),
    )


@app.post("/api/agent/search", response_model=AgentSearchResponse)
async def agent_search(
    req: AgentSearchRequest,
    authorization: Optional[str] = Header(default=None),
):
    """Retrieval-only endpoint. Returns top chunks without calling the LLM.

    Useful for agents that have their own LLM and just need grounding context.
    Cheaper than /ask because it skips generation entirely.
    """
    _require_agent_auth(authorization)
    hits = INDEX.search(
        req.query,
        k=req.top_k or SETTINGS.retrieval_top_k,
        per_kind=SETTINGS.retrieval_per_kind,
    )
    return AgentSearchResponse(
        query=req.query,
        chunks=[
            AgentSearchChunk(
                kind=h.chunk.kind,
                ref=h.chunk.source,
                score=round(h.score, 3),
                text=h.chunk.text,
            )
            for h in hits
        ],
    )


class FeedbackRequest(BaseModel):
    session_id: str
    message_index: int
    helpful: bool


@app.post("/api/feedback")
async def post_feedback(req: FeedbackRequest):
    session = STORE.get(req.session_id)
    if not session:
        raise HTTPException(404, "Session not found or expired.")
    if req.message_index < 0 or req.message_index >= len(session.messages):
        raise HTTPException(400, "message_index out of range.")
    msg = session.messages[req.message_index]
    if msg.role != "assistant":
        raise HTTPException(400, "Feedback only valid on assistant messages.")
    user_msg = ""
    if req.message_index > 0 and session.messages[req.message_index - 1].role == "user":
        user_msg = session.messages[req.message_index - 1].content
    FEEDBACK.record(FeedbackEntry(
        session_id=req.session_id,
        message_index=req.message_index,
        user_message=user_msg[:500],
        assistant_reply=msg.content[:500],
        helpful=req.helpful,
        ts=time.time(),
    ))
    return {"ok": True}


@app.get("/api/feedback/stats")
async def feedback_stats():
    return FEEDBACK.stats()


class IncidentIngestRequest(BaseModel):
    """Payload posted by the Airflow on_failure_callback."""
    dag_id:     str
    task_id:    str
    run_id:     Optional[str] = None
    try_number: Optional[int] = None
    log_url:    Optional[str] = None
    exception:  Optional[str] = None
    log_tail:   str = Field(default="", max_length=120_000)


INCIDENT_PLACEHOLDER = "⏳ _Analysing incident — retrieving context and querying the model (30–90s on Ollama CPU)…_"

# Running incident diagnosis tasks keyed by session_id so the UI can cancel.
INCIDENT_TASKS: Dict[str, asyncio.Task] = {}


def _update_last_assistant(session, text: str) -> None:
    """Replace the most recent assistant message's content in place, so UI
    polling sees the content change without a new turn appearing."""
    for m in reversed(session.messages):
        if m.role == "assistant":
            m.content = text
            m.ts = time.time()
            return
    # No existing assistant message — append one
    session.add("assistant", text)


async def _run_incident_diagnosis(session_id: str, user_message: str) -> None:
    """Background task: retrieval + LLM call + update the placeholder assistant
    message with the final diagnosis. Runs after /api/incidents/ingest has
    already returned to Airflow's callback so the HTTP call can't time out."""
    session = STORE.get(session_id)
    if not session:
        log.warning("Incident %s: session disappeared before diagnosis ran", session_id)
        return

    try:
        signal = log_parser.parse(user_message)
        log_block = signal.to_prompt_block()
        query_parts = [user_message[:500]]
        if signal.exception_classes:
            query_parts.extend(signal.exception_classes[:3])
        hits = INDEX.search(
            " ".join(query_parts),
            k=SETTINGS.retrieval_top_k,
            per_kind=SETTINGS.retrieval_per_kind,
        )
        sources = [h.chunk.source for h in hits]
        context_block = _format_retrieved(hits)

        # Stash retrieval detail on the placeholder message so the UI's
        # center panel can render chunk cards while polling.
        retrieved_detail = [
            {
                "source": h.chunk.source,
                "score": round(h.score, 3),
                "kind": h.chunk.kind,
                "snippet": h.chunk.text,
            }
            for h in hits
        ]

        parts: List[str] = []
        if log_block:     parts.append(f"[LOG SIGNAL]\n{log_block}")
        if context_block: parts.append(f"[RETRIEVED CONTEXT]\n{context_block}")
        parts.append(f"[INCIDENT]\n{user_message}")
        augmented = "\n\n".join(parts)

        llm_messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": augmented},
        ]

        # Save retrieval + prompt data on the assistant placeholder so the
        # UI polling can populate the center panel + Debug inspector.
        for m in reversed(session.messages):
            if m.role == "assistant":
                m.retrieved = retrieved_detail
                m.prompt_data = {
                    "system": SYSTEM_PROMPT,
                    "augmented_user": augmented,
                    "retrieved": retrieved_detail,
                    "log_signal": log_block or None,
                    "model": describe_provider(),
                    "messages_count": len(llm_messages),
                }
                break

        buffer: List[str] = []
        last_ping = time.monotonic()
        async for token in chat_stream(llm_messages, max_tokens=1500):
            buffer.append(token)
            # Periodically publish the in-progress text to the placeholder so
            # UI polling shows tokens filling in rather than staying on "⏳".
            if time.monotonic() - last_ping > 2.0:
                partial = guardrails.redact("".join(buffer))
                _update_last_assistant(session, partial + " ▊")
                last_ping = time.monotonic()

        final = guardrails.redact("".join(buffer))
        if "Sources:" not in final:
            final = final.rstrip() + guardrails.format_sources(sources)
    except asyncio.CancelledError:
        _update_last_assistant(
            session,
            "⏹ _Diagnosis stopped by user. Retrieved context is preserved above._"
        )
        log.info("Incident %s cancelled by user.", session_id)
        raise
    except LLMError as e:
        final = (
            f"⚠ LLM diagnosis failed: {e}\n\n"
            "The incident was captured but the model could not be reached."
        )
    except Exception as e:
        log.exception("Incident %s diagnosis crashed", session_id)
        final = f"⚠ Internal error while producing the diagnosis: {e}"

    _update_last_assistant(session, final)
    log.info("Incident %s diagnosis complete (%d chars).", session_id, len(final))


@app.post("/api/incidents/ingest")
async def ingest_incident(req: IncidentIngestRequest, background: BackgroundTasks):
    """Airflow → KnowIT: create a diagnosis session for a pipeline failure.

    Returns IMMEDIATELY with the new session id so the Airflow callback
    doesn't block on Ollama inference. The retrieval + LLM call runs as a
    background task; the assistant's reply shows up in the session a few
    seconds later (tokens stream into the same session the UI polls).
    """
    title = f"⚠ {req.dag_id} · {req.task_id}"
    session = STORE.create(title=title)

    q_parts: List[str] = [
        "A Knowledge Graph pipeline task just failed in Airflow. Diagnose the "
        "root cause, point to the relevant handbook / userguide / repo / spark "
        "references, and propose the smallest fix. Be concise.",
        "",
        f"DAG:   {req.dag_id}",
        f"Task:  {req.task_id}",
    ]
    if req.run_id:     q_parts.append(f"Run:   {req.run_id}")
    if req.try_number: q_parts.append(f"Try:   {req.try_number}")
    if req.log_url:    q_parts.append(f"Log:   {req.log_url}")
    if req.exception:
        q_parts.append("")
        q_parts.append("Exception:")
        q_parts.append(req.exception.strip())
    if req.log_tail:
        q_parts.append("")
        q_parts.append("--- tail of task log ---")
        q_parts.append(req.log_tail.strip())

    user_message = "\n".join(q_parts)
    session.add("user", user_message)
    if session.messages:
        session.messages[-1].author = "airflow"

    # Placeholder assistant turn so the UI has something to show during the
    # background diagnosis. The background task updates this message in place
    # as tokens arrive, so polling clients see progress instead of silence.
    session.add("assistant", INCIDENT_PLACEHOLDER)

    # Kick off diagnosis as a tracked asyncio task so a /cancel endpoint
    # can interrupt it mid-Ollama. We can't use FastAPI's BackgroundTasks
    # here because those don't expose a handle to cancel.
    task = asyncio.create_task(_run_incident_diagnosis(session.id, user_message))
    INCIDENT_TASKS[session.id] = task
    task.add_done_callback(lambda _t, sid=session.id: INCIDENT_TASKS.pop(sid, None))

    return {
        "session_id": session.id,
        "session_url": f"/?session={session.id}",
        "status": "queued",
    }


@app.post("/api/incidents/{sid}/cancel")
async def cancel_incident(sid: str):
    """Cancel an in-flight incident diagnosis. Idempotent."""
    task = INCIDENT_TASKS.get(sid)
    if task and not task.done():
        task.cancel()
        return {"cancelled": True}
    return {"cancelled": False}


@app.get("/api/sessions/{sid}/messages")
async def get_messages(sid: str):
    s = STORE.get(sid)
    if not s:
        raise HTTPException(404, "Session not found")
    return {
        "id": s.id,
        "title": s.title,
        "messages": [
            {
                "role": m.role,
                "content": m.content,
                "ts": m.ts,
                "author": getattr(m, "author", None),
                # Retrieval detail attached by the incident background task
                # so the UI can render chunk cards in the center panel.
                "retrieved": getattr(m, "retrieved", None),
                # Full prompt payload (system + augmented user + model info)
                # so the UI's Debug inspector can show what went to the LLM.
                "prompt_data": getattr(m, "prompt_data", None),
            }
            for m in s.messages
        ],
    }


# ---------- Helpers ----------
def _format_retrieved(hits) -> str:
    if not hits:
        return ""
    lines: List[str] = []
    for i, h in enumerate(hits, start=1):
        snippet = h.chunk.text.strip().replace("\n", " ")
        if len(snippet) > 600:
            snippet = snippet[:600] + "…"
        lines.append(f"[{i}] ({h.chunk.source})\n{snippet}")
    return "\n\n".join(lines)


# Friendly 502/500 body
@app.exception_handler(Exception)
async def unhandled(_, exc: Exception):
    log.exception("Unhandled error")
    return JSONResponse(status_code=500, content={"error": "Internal error", "detail": str(exc)})
