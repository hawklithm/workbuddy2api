"""Non-streaming response format strategies for supported client protocols.

The chat aggregation layer always returns an OpenAI-shaped object; adapters
here format it for the client's protocol. Keep the legacy payload contracts,
including their current tool-call placement and argument parsing.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Callable, Protocol


class NonStreamFormatter(Protocol):
    def format(
        self, data: dict[str, Any], original: dict[str, Any] | None,
        now_s: Callable[[], int],
    ) -> dict[str, Any]: ...


class OpenAIFormatter:
    def format(
        self, data: dict[str, Any], original: dict[str, Any] | None,
        now_s: Callable[[], int],
    ) -> dict[str, Any]:
        return data


def _first_message(data: dict[str, Any]) -> dict[str, Any]:
    choice = (data.get("choices") or [{}])[0]
    return choice.get("message") or {}


def _arguments(fn: dict[str, Any]) -> Any:
    try:
        return json.loads(fn.get("arguments", "{}"))
    except json.JSONDecodeError:
        return fn.get("arguments", "")


class AnthropicFormatter:
    def format(
        self, data: dict[str, Any], original: dict[str, Any] | None,
        now_s: Callable[[], int],
    ) -> dict[str, Any]:
        message = _first_message(data)
        content = message.get("content", "")
        content_blocks: list[dict[str, Any]] = []
        if content:
            content_blocks.append({"type": "text", "text": content})
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            content_blocks.append({
                "type": "tool_use",
                "id": call.get("id", ""),
                "name": fn.get("name", ""),
                "input": _arguments(fn),
            })
        return {
            "id": "msg_" + uuid.uuid4().hex,
            "type": "message",
            "role": "assistant",
            "model": (original or {}).get("model", data.get("model", "default")),
            "content": content_blocks,
            "stop_reason": "tool_use" if message.get("tool_calls") else "end_turn",
            "stop_sequence": None,
            "usage": {
                "input_tokens": (data.get("usage") or {}).get("prompt_tokens", 0),
                "output_tokens": (data.get("usage") or {}).get("completion_tokens", 0),
            },
        }


class ResponsesFormatter:
    def format(
        self, data: dict[str, Any], original: dict[str, Any] | None,
        now_s: Callable[[], int],
    ) -> dict[str, Any]:
        message = _first_message(data)
        content = message.get("content", "")
        content_parts: list[dict[str, Any]] = []
        if content:
            content_parts.append({"type": "output_text", "text": content})
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            content_parts.append({
                "type": "function_call",
                "id": call.get("id", ""),
                "name": fn.get("name", ""),
                "arguments": _arguments(fn),
            })
        return {
            "id": "resp_" + uuid.uuid4().hex,
            "object": "response",
            "created_at": now_s(),
            "status": "completed",
            "output": [{
                "type": "message",
                "role": "assistant",
                "content": content_parts,
            }],
        }


class NonStreamResponseRegistry:
    """Strategy registry, with the historical OpenAI identity fallback."""

    def __init__(self, formatters: dict[str, NonStreamFormatter] | None = None):
        self._fallback = OpenAIFormatter()
        self._formatters = formatters if formatters is not None else {
            "openai": self._fallback,
            "anthropic": AnthropicFormatter(),
            "responses": ResponsesFormatter(),
        }

    def convert(
        self, data: dict[str, Any], protocol: str,
        original: dict[str, Any] | None, now_s: Callable[[], int],
    ) -> dict[str, Any]:
        formatter = self._formatters.get(protocol, self._fallback)
        return formatter.format(data, original, now_s)


_default_registry = NonStreamResponseRegistry()


def format_nonstream_response(
    data: dict[str, Any], protocol: str,
    original: dict[str, Any] | None, now_s: Callable[[], int],
) -> dict[str, Any]:
    return _default_registry.convert(data, protocol, original, now_s)
