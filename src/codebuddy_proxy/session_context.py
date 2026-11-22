"""Request-local conversation correlation; never an authorization boundary.

Wire rules follow Buddy Switch ebcaead, session_headers.rs. No history is stored.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import uuid
from typing import Any, Mapping

SESSION_HEADERS = ("x-claude-code-session-id", "x-session-id", "x-conversation-id")
CORRELATION_HEADERS = SESSION_HEADERS + ("x-conversation-request-id", "x-trace-id")
SESSION_ID_HEADERS = frozenset(CORRELATION_HEADERS)


def text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def derive(salt: str, key: str) -> str:
    return hashlib.sha256(f"{salt}|{key}".encode()).hexdigest()[:32]


def content_identity(body: dict) -> str:
    digest = hashlib.sha256(b"chat-content-session-v2|")
    found = False
    for message in body.get("messages") or []:
        if not isinstance(message, dict) or message.get("content") is None:
            continue
        role = message.get("role")
        if role not in ("system", "developer", "user"):
            continue
        encoded = json.dumps(message["content"], ensure_ascii=False,
                             sort_keys=True, separators=(",", ":")).encode()
        digest.update(role.encode())
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        found = True
        if role == "user":
            break
    return digest.hexdigest() if found else ""


@dataclass(frozen=True)
class SessionContext:
    conversation_id: str
    request_id: str
    trace_id: str = ""

    @classmethod
    def from_request(cls, original: dict, headers: Mapping[str, str] | None = None,
                     chat_body: dict | None = None) -> SessionContext:
        headers = {key.lower(): text(value) for key, value in (headers or {}).items()}
        metadata = original.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        session = (text(metadata.get("conversation_id"))
                   or text(metadata.get("conversationId"))
                   or text(original.get("session_id"))
                   or text(metadata.get("session_id")) or text(metadata.get("sessionId")))
        session = session or next((headers[key] for key in SESSION_HEADERS if headers.get(key)), "")
        session = (session or text(original.get("conversation_id"))
                   or text(original.get("prompt_cache_key"))
                   or content_identity(chat_body if chat_body is not None else original))
        conversation = session or uuid.uuid4().hex
        request = headers.get("x-conversation-request-id") or (derive("session", session) if session else uuid.uuid4().hex)
        return cls(conversation, request, headers.get("x-trace-id", ""))

    def headers(self) -> dict[str, str]:
        message = uuid.uuid4().hex
        valid_b3 = len(self.request_id) in (16, 32) and all(c in "0123456789abcdefABCDEF" for c in self.request_id)
        return {
            "X-Conversation-ID": self.conversation_id,
            "X-Conversation-Request-ID": self.request_id,
            "X-Conversation-Message-ID": message,
            "X-Request-ID": message,
            "X-Root-Request-ID": self.request_id,
            "X-Trace-ID": self.trace_id or self.request_id,
            "X-B3-TraceId": self.request_id if valid_b3 else message,
            "X-B3-SpanId": message[:16],
            "X-B3-Sampled": "1",
        }


def prompt_cache_key(uid: str, conversation: str) -> str:
    suffix = hashlib.sha256(f"{uid}|{conversation}".encode()).hexdigest()[:16]
    return f"wb2a-{uid[:8] or '-'}-{suffix}"
