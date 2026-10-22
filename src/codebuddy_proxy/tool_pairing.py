"""Repair tool-call/result grouping after protocol adapters create Chat messages.

The proxy may reorder interleaved *text* to satisfy Chat tool-call adjacency.
It must never silently erase an unanswered call: execution may already have
had side effects. For ambiguous histories fail closed before contacting upstream.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any

_LOG = logging.getLogger("codebuddy_proxy.tool_pairing")


class ToolPairingError(ValueError):
    """An ambiguous tool history cannot be forwarded without losing meaning."""

    def __init__(self, reason: str, call_ids: tuple[str, ...] = ()) -> None:
        self.reason = reason
        self.call_ids = call_ids
        super().__init__(f"Invalid tool history ({reason}); calls={len(call_ids)}")

    def as_detail(self) -> dict[str, Any]:
        return {
            "code": "invalid_tool_history",
            "reason": self.reason,
            "tool_call_ids": list(self.call_ids),
            "message": "Tool execution history is ambiguous; no upstream request was sent.",
        }


def _result_digest(content: Any) -> bytes:
    """Store a small comparison token rather than retaining tool output twice."""
    return hashlib.sha256(repr(content).encode("utf-8", "backslashreplace")).digest()


def sanitize_tool_pairing(
    messages: list[dict], *, protocol: str = "unknown",
) -> list[dict]:
    """Return a valid Chat tool history or raise ToolPairingError.

    Non-tool messages interleaved with an *open* group are deferred in their
    original relative order. Orphan results and identical duplicates cannot
    be paired and are dropped with a warning. Missing calls, ambiguous IDs or
    contradictory duplicate results are errors rather than silent data loss.
    """
    out: list[dict] = []
    deferred: list[dict] = []
    pending: set[str] = set()
    seen_call_ids: set[str] = set()
    completed_results: dict[str, bytes] = {}
    active = False

    def warn(action: str, count: int = 1) -> None:
        # Do not log tool outputs, arguments or conversation contents.
        _LOG.warning(
            "tool_pairing protocol=%s action=%s count=%d", protocol, action, count
        )

    def fail(reason: str, ids: set[str] | tuple[str, ...] = ()) -> None:
        ordered_ids = tuple(sorted(ids))
        warn(reason, len(ordered_ids))
        raise ToolPairingError(reason, ordered_ids)

    def close_group() -> None:
        nonlocal active
        if pending:
            fail("unanswered_tool_calls", pending)
        active = False
        if deferred:
            _LOG.info(
                "tool_pairing protocol=%s action=deferred_messages count=%d",
                protocol,
                len(deferred),
            )
            out.extend(deferred)
            deferred.clear()

    for message in messages:
        if not isinstance(message, dict):
            fail("invalid_message")

        role = message.get("role")
        calls = message.get("tool_calls") if role == "assistant" else None
        if calls:
            close_group()
            if not isinstance(calls, list):
                fail("invalid_tool_calls")
            call_ids: set[str] = set()
            for call in calls:
                if not isinstance(call, dict):
                    fail("invalid_tool_calls")
                call_id = call.get("id")
                if not isinstance(call_id, str) or not call_id:
                    fail("invalid_tool_call_id")
                if call_id in call_ids or call_id in seen_call_ids:
                    fail("duplicate_tool_call_id", (call_id,))
                call_ids.add(call_id)
            seen_call_ids.update(call_ids)
            pending.update(call_ids)
            active = True
            out.append(message)
            continue

        if role == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or not call_id:
                warn("invalid_result_id_dropped")
                continue
            digest = _result_digest(message.get("content"))
            if active and call_id in pending:
                out.append(message)
                completed_results[call_id] = digest
                pending.remove(call_id)
                if not pending:
                    close_group()
            elif call_id in completed_results:
                if completed_results[call_id] != digest:
                    fail("conflicting_duplicate_result", (call_id,))
                warn("duplicate_result_dropped")
            else:
                warn("orphan_result_dropped")
            continue

        # Empty assistant messages have no meaning in Chat histories.
        if role == "assistant" and not str(message.get("content") or "").strip():
            continue

        if active:
            deferred.append(message)
        else:
            out.append(message)

    close_group()
    return out
