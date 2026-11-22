"""Non-streaming response format strategies for supported client protocols.

The chat aggregation layer always returns an OpenAI-shaped object; adapters
here format it for the client's protocol. Keep the legacy payload contracts,
including their current tool-call placement and argument parsing.
"""

from __future__ import annotations

import json
import uuid
from codebuddy_proxy.reasoning import reasoning_tokens
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
        if message.get("reasoning_content"):
            content_blocks.append({"type": "thinking", "thinking": message["reasoning_content"]})
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
        from codebuddy_proxy.responses_tools import ResponsesTools, ToolTranslationError
        message = _first_message(data)
        codec = ResponsesTools(original or {})
        output = []
        if message.get("reasoning_content"):
            output.append({"id": "rs_" + uuid.uuid4().hex, "type": "reasoning", "summary": [
                {"type": "summary_text", "text": message["reasoning_content"]}]})
        if message.get("content"):
            output.append({"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": message["content"]}
            ]})
        response = {"id": "resp_" + uuid.uuid4().hex, "object": "response",
                    "created_at": now_s(), "status": "completed", "output": output}
        if data.get("usage"):
            usage = data["usage"]
            response["usage"] = {"input_tokens": usage.get("prompt_tokens", 0),
                                 "output_tokens": usage.get("completion_tokens", 0),
                                 "total_tokens": usage.get("total_tokens", 0),
                                 "output_tokens_details": {"reasoning_tokens": reasoning_tokens(usage)}}
        try:
            for call in message.get("tool_calls") or []:
                fn = call.get("function") or {}
                arguments = fn.get("arguments", "")
                output.append(codec.restore_item({
                    "type": "function_call", "id": call.get("id", ""),
                    "call_id": call.get("id", ""), "name": fn.get("name", ""),
                    "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments),
                }))
        except ToolTranslationError:
            response.update(status="failed", output=[], error={
                "code": "invalid_tool_arguments", "message": "Invalid custom tool input envelope"})
        return response


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
