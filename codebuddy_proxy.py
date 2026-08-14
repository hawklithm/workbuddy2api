#!/usr/bin/env python3
"""Local OpenAI/Responses/Anthropic compatible proxy for CodeBuddy.

Authentication is deliberately lazy unless --login is supplied.  No token
is printed or returned by the proxy.  The upstream protocol is based on the
external-link-v2 flow found in coding-copilot-latest.vsix.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import pathlib
import sys
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator

from codebuddy_client_demo import CodeBuddyClient, CodeBuddyError

# 尝试导入高级功能模块（可选）
try:
    from desensitize import desensitize_body
    HAS_DESENSITIZE = True
except ImportError:
    HAS_DESENSITIZE = False
    def desensitize_body(body, **kwargs):
        return body

try:
    from responses_projection import project_responses_chat_body
    HAS_PROJECTION = True
except ImportError:
    HAS_PROJECTION = False
    def project_responses_chat_body(body):
        return body, {}



def now_s() -> int:
    return int(time.time())


def as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(as_text(x.get("text", "")) if isinstance(x, dict) else as_text(x) for x in value)
    return "" if value is None else str(value)


def message_content(value: Any) -> Any:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        result = []
        for part in value:
            if not isinstance(part, dict):
                result.append({"type": "text", "text": as_text(part)})
                continue
            kind = part.get("type", "text")
            if kind in {"input_text", "text"}:
                result.append({"type": "text", "text": part.get("text", "")})
            elif kind in {"input_image", "image_url"}:
                url = part.get("image_url") if kind == "image_url" else part.get("image_url")
                if isinstance(url, dict):
                    url = url.get("url")
                result.append({"type": "image_url", "image_url": {"url": url}})
            else:
                result.append(part)
        return result
    return as_text(value)


def responses_to_chat(body: dict[str, Any]) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    instructions = body.get("instructions")
    if instructions:
        messages.append({"role": "system", "content": instructions})
    raw_input = body.get("input", [{"role": "user", "content": ""}])
    input_items = [raw_input] if isinstance(raw_input, str) else raw_input
    for item in input_items:
        if isinstance(item, str):
            messages.append({"role": "user", "content": item})
        elif isinstance(item, dict) and item.get("type") in {"message", None}:
            messages.append({
                "role": item.get("role", "user"),
                "content": message_content(item.get("content", "")),
                **({"name": item["name"]} if item.get("name") else {}),
            })
        elif isinstance(item, dict) and item.get("type") == "function_call_output":
            messages.append({
                "role": "tool",
                "tool_call_id": item.get("call_id", ""),
                "content": as_text(item.get("output", "")),
            })
    result = {k: body[k] for k in ("model", "temperature", "top_p", "max_output_tokens", "stream") if k in body}
    if "max_output_tokens" in result:
        result["max_tokens"] = result.pop("max_output_tokens")
    result["messages"] = messages
    if body.get("tools"):
        result["tools"] = body["tools"]
    if body.get("tool_choice") is not None:
        result["tool_choice"] = body["tool_choice"]
    return result


def anthropic_to_chat(body: dict[str, Any]) -> dict[str, Any]:
    messages = []
    system = body.get("system")
    if system:
        messages.append({"role": "system", "content": message_content(system)})
    for item in body.get("messages", []):
        item = dict(item)
        item["content"] = message_content(item.get("content", ""))
        messages.append(item)
    result: dict[str, Any] = {
        "model": body.get("model", "default"),
        "messages": messages,
        "stream": bool(body.get("stream")),
    }
    for source, target in (("max_tokens", "max_tokens"), ("temperature", "temperature"), ("top_p", "top_p")):
        if source in body:
            result[target] = body[source]
    if body.get("tools"):
        result["tools"] = []
        for tool in body["tools"]:
            if tool.get("type") == "function" and tool.get("function"):
                result["tools"].append(tool)
            elif tool.get("name"):
                result["tools"].append({
                    "type": "function",
                    "function": {"name": tool["name"], "description": tool.get("description", ""),
                                  "parameters": tool.get("input_schema", {})},
                })
            else:
                result["tools"].append(tool)
    if body.get("tool_choice") is not None:
        result["tool_choice"] = body["tool_choice"]
    return result


def collect_chat_stream(response: Any) -> dict[str, Any]:
    """Aggregate CodeBuddy's SSE-only chat response into Chat Completions."""
    text: list[str] = []
    tool_calls: dict[int, dict[str, Any]] = {}
    model = "default"
    finish_reason = None
    usage = None
    for raw in response:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        model = chunk.get("model") or model
        usage = chunk.get("usage") or usage
        for choice in chunk.get("choices") or []:
            finish_reason = choice.get("finish_reason") or finish_reason
            delta = choice.get("delta") or {}
            if delta.get("content"):
                text.append(str(delta["content"]))
            for call in delta.get("tool_calls") or []:
                index = int(call.get("index", 0))
                slot = tool_calls.setdefault(index, {"id": None, "name": "", "arguments": ""})
                slot["id"] = call.get("id") or slot["id"]
                fn = call.get("function") or {}
                slot["name"] = fn.get("name") or slot["name"]
                slot["arguments"] += fn.get("arguments") or ""
    message: dict[str, Any] = {"role": "assistant", "content": "".join(text) or None}
    if tool_calls:
        message["tool_calls"] = [
            {"id": v["id"] or "call_" + uuid.uuid4().hex, "type": "function",
             "function": {"name": v["name"], "arguments": v["arguments"]}}
            for _, v in sorted(tool_calls.items())
        ]
        finish_reason = finish_reason or "tool_calls"
    return {"id": "chatcmpl-" + uuid.uuid4().hex, "object": "chat.completion",
            "created": now_s(), "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason or "stop"}],
            "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}


def response_events_from_chunk(chunk: dict[str, Any], response_id: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    choices = chunk.get("choices") or []
    if not choices:
        return events
    delta = choices[0].get("delta") or {}
    text = delta.get("content")
    if text:
        events.append(("response.output_text.delta", {"type": "response.output_text.delta",
            "item_id": response_id, "output_index": 0, "content_index": 0, "delta": text}))
    for call in delta.get("tool_calls") or []:
        fn = call.get("function") or {}
        if fn.get("name"):
            events.append(("response.output_item.added", {"type": "response.output_item.added",
                "item": {"type": "function_call", "call_id": call.get("id", ""),
                          "name": fn["name"], "arguments": ""}}))
        if fn.get("arguments"):
            events.append(("response.function_call_arguments.delta", {
                "type": "response.function_call_arguments.delta", "item_id": call.get("id", response_id),
                "output_index": 0, "delta": fn["arguments"]}))
    return events


class AnthropicStreamState:
    def __init__(self, model: str) -> None:
        self.id = "msg_" + uuid.uuid4().hex
        self.model = model
        self.text = ""
        self.tool_calls: dict[int, dict[str, Any]] = {}
        self.finish_reason = "stop"
        self.started = False
        self.text_started = False

    def feed(self, chunk: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        out: list[tuple[str, dict[str, Any]]] = []
        if not self.started:
            self.started = True
            out.append(("message_start", {"type": "message_start", "message": {
                "id": self.id, "type": "message", "role": "assistant", "content": [],
                "model": self.model, "usage": {"input_tokens": 0, "output_tokens": 0}}}))
        for choice in chunk.get("choices") or []:
            self.finish_reason = choice.get("finish_reason") or self.finish_reason
            delta = choice.get("delta") or {}
            if delta.get("content"):
                text = str(delta["content"])
                self.text += text
                if not self.text_started:
                    self.text_started = True
                    out.append(("content_block_start", {"type": "content_block_start", "index": 0,
                        "content_block": {"type": "text", "text": ""}}))
                out.append(("content_block_delta", {"type": "content_block_delta", "index": 0,
                    "delta": {"type": "text_delta", "text": text}}))
            for call in delta.get("tool_calls") or []:
                idx = int(call.get("index", 0))
                slot = self.tool_calls.setdefault(idx, {"id": "", "name": "", "args": ""})
                fn = call.get("function") or {}
                if call.get("id"):
                    slot["id"] = call["id"]
                if fn.get("name"):
                    slot["name"] = fn["name"]
                    out.append(("content_block_start", {"type": "content_block_start", "index": idx + 1,
                        "content_block": {"type": "tool_use", "id": slot["id"], "name": slot["name"], "input": {}}}))
                if fn.get("arguments"):
                    slot["args"] += fn["arguments"]
                    out.append(("content_block_delta", {"type": "content_block_delta", "index": idx + 1,
                        "delta": {"type": "input_json_delta", "partial_json": fn["arguments"]}}))
        return out

    def finish(self) -> list[tuple[str, dict[str, Any]]]:
        reason = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens"}.get(self.finish_reason, "end_turn")
        events: list[tuple[str, dict[str, Any]]] = []
        if self.text_started:
            events.append(("content_block_stop", {"type": "content_block_stop", "index": 0}))
        for index in self.tool_calls:
            events.append(("content_block_stop", {"type": "content_block_stop", "index": index + 1}))
        events.extend([
            ("message_delta", {"type": "message_delta", "delta": {"stop_reason": reason, "stop_sequence": None},
             "usage": {"output_tokens": 0}}),
            ("message_stop", {"type": "message_stop"}),
        ])
        return events


class ProxyState:
    def __init__(self, client: CodeBuddyClient, mock_dir: pathlib.Path | None = None,
                 log_file: pathlib.Path | None = None,
                 enable_desensitize: bool = False,
                 enable_optimize_context: bool = False) -> None:
        self.client = client
        self.mock_dir = mock_dir
        self.log_file = log_file
        self.lock = threading.RLock()
        self.log_lock = threading.RLock()
        self.started_at = time.time()
        self.enable_desensitize = enable_desensitize
        self.enable_optimize_context = enable_optimize_context

    def ensure_auth(self) -> None:
        if self.mock_dir is not None:
            return
        with self.lock:
            self.client.ensure_authenticated()

    def mock_body(self, filename: str) -> bytes:
        if self.mock_dir is None:
            raise CodeBuddyError("mock mode is not enabled")
        path = self.mock_dir / filename
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            body = envelope["response"]["body_base64"]
            import base64
            return base64.b64decode(body)
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise CodeBuddyError(f"invalid mock fixture: {path}") from exc

    def mock_json(self, filename: str) -> Any:
        return json.loads(self.mock_body(filename).decode("utf-8"))

    def mock_chat_response(self) -> io.BytesIO:
        return io.BytesIO(self.mock_body("chat-hi.sse.json"))

    def write_log(self, event: str, **fields: Any) -> None:
        """Append a complete, JSONL diagnostic record without auth headers."""
        if self.log_file is None:
            return
        record = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event, **fields}
        raw = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            with self.log_lock:
                self.log_file.parent.mkdir(parents=True, exist_ok=True)
                flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
                fd = os.open(self.log_file, flags, 0o600)
                try:
                    os.write(fd, raw)
                finally:
                    os.close(fd)
        except OSError as exc:
            print(f"[proxy] unable to write log file {self.log_file}: {exc}",
                  file=sys.stderr, flush=True)

    def write_body_log(self, event: str, body: bytes, **fields: Any) -> None:
        self.write_log(event, body_bytes=len(body),
                       body_text=body.decode("utf-8", errors="replace"),
                       body_base64=base64.b64encode(body).decode("ascii"), **fields)


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def state(self) -> ProxyState:
        return self.server.proxy_state  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        # Never log Authorization, query strings, request bodies, or tokens.
        print(f"[proxy] {self.command} {self.path.split('?', 1)[0]} - {fmt % args}")

    def diagnostic(self, event: str, **fields: Any) -> None:
        """Write structured, non-sensitive request diagnostics to the service log."""
        values = " ".join(f"{key}={json.dumps(value, ensure_ascii=False)}"
                           for key, value in fields.items())
        print(f"[proxy-debug] event={event} {values}".rstrip(), file=sys.stderr, flush=True)

    @staticmethod
    def body_summary(body: dict[str, Any]) -> dict[str, Any]:
        messages = body.get("messages") or []
        message_summary = []
        for item in messages:
            if not isinstance(item, dict):
                continue
            content = item.get("content", "")
            if isinstance(content, str):
                content_length = len(content)
                content_type = "text"
            elif isinstance(content, list):
                content_length = sum(
                    len(str(part.get("text", ""))) for part in content if isinstance(part, dict)
                )
                content_type = "parts"
            else:
                content_length = 0
                content_type = type(content).__name__
            message_summary.append({
                "role": item.get("role"),
                "content_type": content_type,
                "content_length": content_length,
            })
        return {
            "model": body.get("model"),
            "stream": bool(body.get("stream")),
            "message_count": len(messages),
            "messages": message_summary,
            "tool_count": len(body.get("tools") or []),
        }

    @staticmethod
    def text_summary(value: str) -> dict[str, Any]:
        return {
            "content_length": len(value),
            "content_sha256": hashlib.sha256(value.encode("utf-8")).hexdigest()[:16],
            "safety_message_detected": any(marker in value for marker in ("敏感内容", "无法响应")),
        }

    def send_json(self, status: int, payload: Any) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 20 * 1024 * 1024:
            raise CodeBuddyError("request body too large")
        raw = self.rfile.read(length) if length else b"{}"
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise CodeBuddyError("request body must be a JSON object")
        return value

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path == "/health":
                auth = {} if self.state.mock_dir is not None else (self.state.client.session.get("auth") or {})
                expires = int(auth.get("expiresAt") or 0)
                self.send_json(200, {
                    "status": "ok",
                    "authenticated": bool(auth.get("accessToken")),
                    "token_valid": not expires or expires > int(time.time() * 1000),
                    "uptime_seconds": int(time.time() - self.state.started_at),
                })
                return
            if path == "/v1/models":
                self.state.write_log("client_request", method="GET", path=path, body=None)
                self.state.ensure_auth()
                if self.state.mock_dir is not None:
                    payload = self.state.mock_json("models.v3-config.json")
                    models = self.extract_models(payload)
                    self.send_json(200, {"object": "list", "data": [self.model_item(m) for m in models]})
                    return
                # The current VSIX CloudProductProvider defaults to /v3/config
                # (older builds used /v2/config).  Keep the old path as a
                # compatibility fallback for older CodeBuddy deployments.
                try:
                    payload = self.upstream_json("GET", "/v3/config?repos=")
                except CodeBuddyError as exc:
                    if "404" not in str(exc):
                        raise
                    payload = self.upstream_json("GET", "/v2/config")
                models = self.extract_models(payload)
                self.send_json(200, {"object": "list", "data": [self.model_item(m) for m in models]})
                return
            self.send_json(404, {"error": {"message": "not found", "type": "invalid_request_error"}})
        except Exception as exc:
            self.handle_error(exc)

    def do_POST(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        try:
            body = self.read_json()
            self.state.write_log("client_request", method="POST", path=path, body=body)
            self.diagnostic("request", request_id=self.headers.get("X-Request-ID", "-"),
                            protocol=path, **self.body_summary(body))
            if path == "/v1/chat/completions":
                self.forward_chat(body, "openai")
            elif path == "/v1/responses":
                chat_body = responses_to_chat(body)
                
                # 消息压缩优化（如果启用）
                if self.state.enable_optimize_context and HAS_PROJECTION:
                    chat_body, proj_stats = project_responses_chat_body(chat_body)
                    self.diagnostic("projection_applied", protocol="responses", **proj_stats)
                
                self.diagnostic("request", request_id=self.headers.get("X-Request-ID", "-"),
                                protocol="responses", **self.body_summary(chat_body))
                self.forward_chat(chat_body, "responses", original=body)
            elif path == "/v1/messages":
                self.forward_chat(anthropic_to_chat(body), "anthropic", original=body)
            else:
                self.send_json(404, {"error": {"message": "not found", "type": "invalid_request_error"}})
        except Exception as exc:
            self.handle_error(exc)

    @staticmethod
    def extract_models(payload: Any) -> list[dict[str, Any]]:
        current = payload
        for _ in range(4):
            if isinstance(current, dict) and isinstance(current.get("models"), list):
                return [m for m in current["models"] if isinstance(m, dict)]
            if isinstance(current, dict) and "data" in current:
                current = current["data"]
            else:
                break
        return []

    @staticmethod
    def model_item(model: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": model.get("id", "default"),
            "object": "model",
            "created": 0,
            "owned_by": model.get("vendor", "codebuddy"),
            "name": model.get("name", model.get("id", "default")),
            "context_length": model.get("maxInputTokens"),
            "max_output_tokens": model.get("maxOutputTokens"),
        }

    def upstream_json(self, method: str, path: str, body: Any = None) -> Any:
        if self.state.mock_dir is not None:
            return self.state.mock_json("models.v3-config.json")
        self.state.client.ensure_authenticated()
        headers = {
            **self.state.client._auth_headers(),
            "Accept": "application/json",
            "User-Agent": "CodeBuddyIDE/4.10.33259736",
            "X-IDE-Type": "VSCode",
            "X-IDE-Name": "VSCode",
            "X-IDE-Version": "1.70.2",
            "X-Product-Version": "4.10.33259736",
            "X-Requested-With": "XMLHttpRequest",
            "X-Product": "SaaS",
        }
        data = None if body is None else json.dumps(body).encode("utf-8")
        if data:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.state.client.endpoint + path, data=data, headers=headers, method=method
        )
        self.state.write_log("upstream_request", method=method, path=path, body=body)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw_response = response.read()
                self.state.write_body_log("upstream_response", raw_response,
                                          method=method, path=path, status=response.status)
                return json.loads(raw_response.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            self.state.write_body_log("upstream_response", detail.encode("utf-8"),
                                      method=method, path=path, status=exc.code)
            raise CodeBuddyError(f"upstream HTTP {exc.code}: {detail[:1000]}") from exc

    def forward_chat(self, body: dict[str, Any], protocol: str, original: dict[str, Any] | None = None) -> None:
        self.state.ensure_auth()
        self.diagnostic("upstream_request", protocol=protocol, **self.body_summary(body))
        stream = bool(body.get("stream"))
        upstream_body = dict(body)
        # 应用脱敏处理
        if self.state.enable_desensitize:
            upstream_body = desensitize_body(upstream_body, compact_harness=True)
        # The CodeBuddy endpoint is SSE-oriented.  Match codebuddy2api:
        # always request a stream and aggregate it for non-stream clients.
        upstream_body["stream"] = True
        upstream_body.setdefault("stream_options", {"include_usage": True})
        self.state.write_log("upstream_request", protocol=protocol,
                             method="POST", path="/v2/chat/completions", body=upstream_body)
        headers = {
            **self.state.client._auth_headers(),
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
        }
        if self.state.mock_dir is not None:
            response = self.state.mock_chat_response()
        else:
            request = urllib.request.Request(
                self.state.client.endpoint + "/v2/chat/completions",
                data=json.dumps(upstream_body).encode("utf-8"), headers=headers, method="POST",
            )
            try:
                response = urllib.request.urlopen(request, timeout=300)
                self.diagnostic("upstream_response", protocol=protocol, status=response.status,
                                content_type=response.headers.get("Content-Type", "-"))
            except urllib.error.HTTPError as exc:
                if exc.code == 401 and self.state.client.refresh():
                    return self.forward_chat(body, protocol, original)
                detail = exc.read().decode("utf-8", errors="replace")
                self.state.write_body_log("upstream_response", detail.encode("utf-8"),
                                          protocol=protocol, method="POST",
                                          path="/v2/chat/completions", status=exc.code)
                self.diagnostic("upstream_error", protocol=protocol, status=exc.code,
                                detail_length=len(detail))
                raise CodeBuddyError(f"upstream HTTP {exc.code}: {detail[:1000]}") from exc
        with response:
            if stream:
                self.stream_response(response, protocol, original)
            else:
                raw_response = response.read()
                self.state.write_body_log("upstream_response", raw_response, protocol=protocol,
                                          status=getattr(response, "status", 200),
                                          method="POST", path="/v2/chat/completions")
                data = collect_chat_stream(io.BytesIO(raw_response))
                content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                self.diagnostic("response", protocol=protocol, stream=False,
                                model=data.get("model"), finish_reason=((data.get("choices") or [{}])[0].get("finish_reason")),
                                **self.text_summary(content))
                self.send_json(200, self.convert_nonstream(data, protocol, original))

    def stream_response(self, response: Any, protocol: str, original: dict[str, Any] | None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        # The mock response is fully materialized from a fixture.  Closing the
        # connection gives simple clients an unambiguous EOF after [DONE] (or
        # the protocol-specific terminal event).
        self.send_header("Connection", "close")
        self.end_headers()
        if self.state.mock_dir is not None:
            self.close_connection = True
        response_id = "resp_" + uuid.uuid4().hex
        anthropic_state = AnthropicStreamState((original or {}).get("model", "default")) if protocol == "anthropic" else None
        emitted_response_created = False
        response_text = ""
        response_text_started = False
        chunk_count = 0
        done_seen = False
        raw_chunks: list[bytes] = []
        for raw in response:
            raw_chunks.append(raw)
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                done_seen = True
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            chunk_count += 1
            if protocol == "openai":
                response_text += str(((chunk.get("choices") or [{}])[0].get("delta") or {}).get("content") or "")
            events: list[tuple[str, dict[str, Any]]] = []
            if protocol == "responses":
                if not emitted_response_created:
                    emitted_response_created = True
                    events.append(("response.created", {"type": "response.created", "response": {
                        "id": response_id, "object": "response", "status": "in_progress",
                        "created_at": now_s(), "output": []}}))
                chunk_events = response_events_from_chunk(chunk, response_id)
                chunk_text = ((chunk.get("choices") or [{}])[0].get("delta") or {}).get("content") or ""
                if chunk_text and not response_text_started:
                    response_text_started = True
                    events.append(("response.output_item.added", {"type": "response.output_item.added",
                        "output_index": 0, "item": {"id": response_id, "type": "message", "role": "assistant"}}))
                    events.append(("response.content_part.added", {"type": "response.content_part.added",
                        "item_id": response_id, "output_index": 0, "content_index": 0,
                        "part": {"type": "output_text", "text": "", "annotations": []}}))
                response_text += str(chunk_text)
                events.extend(chunk_events)
            elif protocol == "anthropic" and anthropic_state is not None:
                events.extend(anthropic_state.feed(chunk))
            else:
                converted = self.convert_stream_chunk(chunk, protocol, response_id)
                if converted is not None:
                    events.append(("message", converted))
            for event_name, converted in events:
                prefix = (f"event: {event_name}\n" if protocol in {"responses", "anthropic"} else "")
                self.wfile.write((prefix + "data: " + json.dumps(converted, ensure_ascii=False) + "\n\n").encode())
                self.wfile.flush()
        if protocol == "responses" and not emitted_response_created:
            self.wfile.write(("event: response.created\ndata: " + json.dumps({"type": "response.created", "response": {
                "id": response_id, "object": "response", "status": "in_progress"}}, ensure_ascii=False) + "\n\n").encode())
        if protocol == "responses" and response_text_started:
            for event_name, payload in [
                ("response.output_text.done", {"type": "response.output_text.done", "item_id": response_id,
                 "output_index": 0, "content_index": 0, "text": response_text}),
                ("response.content_part.done", {"type": "response.content_part.done", "item_id": response_id,
                 "output_index": 0, "content_index": 0,
                 "part": {"type": "output_text", "text": response_text, "annotations": []}}),
                ("response.output_item.done", {"type": "response.output_item.done", "output_index": 0,
                 "item": {"id": response_id, "type": "message", "role": "assistant",
                           "content": [{"type": "output_text", "text": response_text, "annotations": []}]}}),
                ("response.completed", {"type": "response.completed", "response": {
                 "id": response_id, "object": "response", "status": "completed",
                 "output_text": response_text}}),
            ]:
                self.wfile.write((f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n").encode())
        if protocol == "anthropic" and anthropic_state is not None:
            for event_name, converted in anthropic_state.finish():
                self.wfile.write((f"event: {event_name}\ndata: {json.dumps(converted, ensure_ascii=False)}\n\n").encode())
        if protocol == "openai":
            self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        raw_response = b"".join(raw_chunks)
        self.state.write_body_log("upstream_response", raw_response, protocol=protocol,
                                  status=getattr(response, "status", 200),
                                  method="POST", path="/v2/chat/completions")
        logged_text = anthropic_state.text if anthropic_state is not None else response_text
        self.diagnostic("response", protocol=protocol, stream=True, chunk_count=chunk_count,
                        upstream_done=done_seen, **self.text_summary(logged_text))

    @staticmethod
    def convert_nonstream(data: dict[str, Any], protocol: str, original: dict[str, Any] | None) -> dict[str, Any]:
        if protocol == "openai":
            return data
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = message.get("content", "")
        if protocol == "anthropic":
            content_blocks = []
            if content:
                content_blocks.append({"type": "text", "text": content})
            for call in message.get("tool_calls") or []:
                fn = call.get("function") or {}
                try:
                    arguments = json.loads(fn.get("arguments", "{}"))
                except json.JSONDecodeError:
                    arguments = fn.get("arguments", "")
                content_blocks.append({"type": "tool_use", "id": call.get("id", ""),
                                       "name": fn.get("name", ""), "input": arguments})
            return {
                "id": "msg_" + uuid.uuid4().hex, "type": "message", "role": "assistant",
                "model": (original or {}).get("model", data.get("model", "default")),
                "content": content_blocks, "stop_reason": "tool_use" if message.get("tool_calls") else "end_turn",
                "stop_sequence": None,
                "usage": data.get("usage", {}),
            }
        output = [{"id": "msg_" + uuid.uuid4().hex, "type": "message", "role": "assistant",
                   "content": [{"type": "output_text", "text": content, "annotations": []}]}]
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            output.append({"id": call.get("id", ""), "type": "function_call", "call_id": call.get("id", ""),
                           "name": fn.get("name", ""), "arguments": fn.get("arguments", "{}")})
        return {
            "id": "resp_" + uuid.uuid4().hex, "object": "response", "created_at": now_s(),
            "status": "completed", "model": (original or {}).get("model", data.get("model", "default")),
            "output": output,
            "output_text": content, "usage": data.get("usage"),
        }

    @staticmethod
    def convert_stream_chunk(chunk: dict[str, Any], protocol: str, response_id: str) -> dict[str, Any] | None:
        if protocol == "openai":
            return chunk
        choice = (chunk.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        text = delta.get("content") or ""
        if protocol == "anthropic":
            if not text:
                return None
            return {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}
        if not text:
            return None
        return {"type": "response.output_text.delta", "sequence_number": 0, "item_id": response_id,
                "output_index": 0, "content_index": 0, "delta": text}

    def handle_error(self, exc: Exception) -> None:
        status = 401 if isinstance(exc, CodeBuddyError) and "401" in str(exc) else 500
        self.send_json(status, {"error": {"message": str(exc), "type": "server_error"}})


def main() -> int:
    parser = argparse.ArgumentParser(description="CodeBuddy local API proxy")
    parser.add_argument("--host", default=os.getenv("CODEBUDDY_PROXY_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("CODEBUDDY_PROXY_PORT", "8787")))
    parser.add_argument("--endpoint", default=os.getenv("CODEBUDDY_ENDPOINT", "https://copilot.tencent.com"))
    parser.add_argument("--session-file", type=pathlib.Path)
    parser.add_argument("--mock-dir", type=pathlib.Path,
                        help="只使用指定目录中的真实响应 fixture，不访问 CodeBuddy 后端")
    parser.add_argument("--log-file", type=pathlib.Path,
                        default=pathlib.Path(os.getenv("CODEBUDDY_PROXY_LOG_FILE", "logs/codebuddy-proxy.jsonl")),
                        help="记录完整请求/响应的 JSONL 文件（默认 logs/codebuddy-proxy.jsonl）")
    parser.add_argument("--desensitize", action="store_true",
                        help="启用脱敏处理，对 system 消息中的敏感词插入零宽空格（缓解审核误拦）")
    parser.add_argument("--optimize-context", action="store_true",
                        help="启用消息压缩优化（仅 /v1/responses），大幅减少 token 使用（适用于 Codex CLI 等长上下文场景）")
    parser.add_argument("--login", action="store_true", help="启动时执行浏览器登录/账户查询")
    parser.add_argument("--no-browser", action="store_true", help="登录时不自动打开浏览器")
    args = parser.parse_args()
    client = CodeBuddyClient(args.endpoint, session_file=args.session_file)
    if args.login:
        client.login(open_browser=not args.no_browser)
    server = ThreadingHTTPServer((args.host, args.port), ProxyHandler)
    server.proxy_state = ProxyState(
        client, args.mock_dir, args.log_file,
        enable_desensitize=args.desensitize,
        enable_optimize_context=args.optimize_context
    )  # type: ignore[attr-defined]
    print(f"CodeBuddy proxy listening on http://{args.host}:{args.port}")
    print("Endpoints: /v1/models /v1/chat/completions /v1/responses /v1/messages /health")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
