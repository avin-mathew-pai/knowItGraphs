"""In-memory chat sessions with inactivity TTL.

A session holds the full message history. To keep tokens low we only replay
the most recent N turns verbatim; older turns are replaced by a short summary
line the LLM can rely on for context.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .config import SETTINGS


@dataclass
class Message:
    role: str   # "user" | "assistant"
    content: str
    ts: float = field(default_factory=time.time)


@dataclass
class Session:
    id: str
    title: str
    messages: List[Message] = field(default_factory=list)
    summary: str = ""
    last_active: float = field(default_factory=time.time)

    def add(self, role: str, content: str) -> None:
        self.messages.append(Message(role=role, content=content))
        self.last_active = time.time()

    def build_llm_messages(self, system_prompt: str) -> List[Dict[str, str]]:
        """Return OpenAI-style messages, trimmed for token efficiency."""
        out: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]
        if self.summary:
            out.append({"role": "system", "content": f"Earlier-conversation summary:\n{self.summary}"})
        # Keep last 2*N messages (N user+assistant pairs)
        keep = SETTINGS.max_history_turns * 2
        recent = self.messages[-keep:]
        for m in recent:
            out.append({"role": m.role, "content": m.content})
        return out

    def should_summarise(self) -> bool:
        return len(self.messages) > SETTINGS.max_history_turns * 2


class SessionStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: Dict[str, Session] = {}

    def create(self, title: str = "New chat") -> Session:
        sid = uuid.uuid4().hex[:12]
        sess = Session(id=sid, title=title or "New chat")
        with self._lock:
            self._sessions[sid] = sess
        return sess

    def get(self, sid: str) -> Optional[Session]:
        with self._lock:
            sess = self._sessions.get(sid)
        if sess:
            sess.last_active = time.time()
        return sess

    def delete(self, sid: str) -> bool:
        with self._lock:
            return self._sessions.pop(sid, None) is not None

    def list(self) -> List[Session]:
        with self._lock:
            return sorted(self._sessions.values(), key=lambda s: s.last_active, reverse=True)

    def sweep_expired(self) -> int:
        """Remove sessions idle longer than TTL. Returns count removed."""
        cutoff = time.time() - SETTINGS.session_ttl_min * 60
        with self._lock:
            expired = [sid for sid, s in self._sessions.items() if s.last_active < cutoff]
            for sid in expired:
                self._sessions.pop(sid, None)
        return len(expired)


STORE = SessionStore()
