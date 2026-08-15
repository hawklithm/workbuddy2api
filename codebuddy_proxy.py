#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "fastapi>=0.104.0",
#     "uvicorn>=0.24.0",
#     "httpx[socks]>=0.25.0",
#     "requests>=2.31.0",
# ]
# ///
"""Local OpenAI/Responses/Anthropic compatible proxy for CodeBuddy.

High-performance async implementation with FastAPI + httpx.

Features:
- High concurrency: 1000+ concurrent requests
- Low memory footprint: ~5KB per request
- Robust timeout handling with async iterators

Usage with uv:
    uv run codebuddy_proxy.py
    uv run codebuddy_proxy.py --desensitize
    uv run codebuddy_proxy.py --host 0.0.0.0 --port 8787
"""

import argparse
import base64
import hashlib
import io
import json
import logging
import logging.handlers
import os
import pathlib
import sys
import time
import uuid
from typing import Any, Optional

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
import uvicorn
from dsml_parser import DSMLStreamBuffer, parse_all_tool_calls, remove_all_tool_call_markers

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

# 导入协议转换器
try:
    from responses_adapter import responses_request_to_chat, ResponsesStreamConverter
    HAS_RESPONSES_ADAPTER = True
except ImportError:
    HAS_RESPONSES_ADAPTER = False
    def responses_request_to_chat(body): 
        raise RuntimeError("responses_adapter not available - cannot convert /v1/responses requests")
    ResponsesStreamConverter = None

try:
    from anthropic_adapter import anthropic_to_chat, AnthropicStreamConverter
    HAS_ANTHROPIC_ADAPTER = True
except ImportError:
    HAS_ANTHROPIC_ADAPTER = False
    def anthropic_to_chat(body): 
        raise RuntimeError("anthropic_adapter not available - cannot convert /v1/messages requests")
    AnthropicStreamConverter = None


# ============================================================================
# 日志配置
# ============================================================================

def setup_logging(log_dir: pathlib.Path) -> logging.Logger:
    """配置滚动日志：按天分片，保留30天。"""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "proxy.log"
    
    logger = logging.getLogger("codebuddy_proxy")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers.clear()
    
    handler = logging.handlers.TimedRotatingFileHandler(
        log_file, when='midnight', interval=1, backupCount=30, encoding='utf-8'
    )
    handler.suffix = "%Y-%m-%d"
    formatter = logging.Formatter(
        '%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    
    return logger


def now_s() -> int:
    return int(time.time())


# ============================================================================
# 全局状态管理
# ============================================================================

class ProxyState:
    """管理 proxy 的全局状态：认证、日志、配置。"""
    
    def __init__(
        self,
        client: CodeBuddyClient,
        mock_dir: pathlib.Path | None,
        log_file: pathlib.Path | None,
        enable_desensitize: bool = False,
        enable_optimize_context: bool = False,
        verbose_llm: bool = False,
        logger: logging.Logger | None = None,
    ):
        self.client = client
        self.mock_dir = mock_dir
        self.log_file = log_file
        self.enable_desensitize = enable_desensitize
        self.enable_optimize_context = enable_optimize_context
        self.verbose_llm = verbose_llm
        self.logger = logger
        self.started_at = time.time()
    
    def ensure_auth(self) -> None:
        if self.mock_dir is None:
            self.client.ensure_authenticated()
    
    def write_log(self, event: str, **kwargs) -> None:
        if self.log_file is None:
            return
        try:
            record = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event, **kwargs}
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass
    
    def write_body_log(self, event: str, body: bytes, **kwargs) -> None:
        if self.log_file is None:
            return
        try:
            text = body.decode("utf-8", errors="replace")
            record = {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "event": event,
                "body_bytes": len(body),
                "body_text": text,
                **kwargs
            }
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass


# ============================================================================
# FastAPI 应用
# ============================================================================

app = FastAPI(title="CodeBuddy Proxy (FastAPI)", version="2.0")

# 全局状态（在 main() 中初始化）
proxy_state: ProxyState | None = None


def get_state() -> ProxyState:
    if proxy_state is None:
        raise HTTPException(status_code=503, detail={"error": {"message": "proxy not initialized", "type": "internal_error"}})
    return proxy_state


# ============================================================================
# 辅助函数
# ============================================================================

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


def text_summary(value: str) -> dict[str, Any]:
    return {
        "content_length": len(value),
        "content_sha256": hashlib.sha256(value.encode("utf-8")).hexdigest()[:16],
        "safety_message_detected": any(marker in value for marker in ("敏感内容", "无法响应")),
    }


def diagnostic(event: str, **kwargs) -> None:
    """输出诊断日志到 logger。"""
    state = get_state()
    if state.logger:
        state.logger.info(f"{event}: {json.dumps(kwargs, ensure_ascii=False)}")


def log_client_request(method: str, path: str, body: dict[str, Any] | None) -> None:
    """Log client request with verbosity control."""
    state = get_state()
    
    if state.verbose_llm:
        state.write_log("client_request", method=method, path=path, body=body)
    else:
        if body:
            summary = body_summary(body)
            state.write_log("client_request_summary", method=method, path=path, **summary)
        else:
            state.write_log("client_request_summary", method=method, path=path)


def log_upstream_request(protocol: str, body: dict[str, Any]) -> None:
    """Log upstream request with verbosity control."""
    state = get_state()
    
    if state.verbose_llm:
        state.write_log("upstream_request", protocol=protocol,
                       method="POST", path="/v2/chat/completions", body=body)
        diagnostic("upstream_request", protocol=protocol, **body_summary(body))
    else:
        messages = body.get("messages", [])
        total_chars = sum(
            len(str(m.get("content", "")))
            for m in messages
            if isinstance(m, dict)
        )
        summary = {
            "model": body.get("model"),
            "message_count": len(messages),
            "tool_count": len(body.get("tools", [])),
            "stream": bool(body.get("stream")),
            "total_chars": total_chars
        }
        state.write_log("upstream_request_summary", protocol=protocol, **summary)
        diagnostic("upstream_request_summary", protocol=protocol, **summary)


def log_upstream_response(protocol: str, text: str, **stats) -> None:
    """Log upstream response with verbosity control."""
    state = get_state()
    
    common = {
        "protocol": protocol,
        "content_length": len(text),
        "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        "safety_message_detected": any(
            marker in text.lower() for marker in 
            ("sensitive", "cannot respond", "敏感内容", "无法响应", "unable to")
        ),
        **stats
    }
    
    if state.verbose_llm:
        common["content_preview"] = text[:200] if text else ""
    
    diagnostic("response", **common)
    state.write_log("stream_completed" if stats.get("stream") else "response",
                   **{k: v for k, v in common.items() 
                      if k not in ("content_preview",)})


# ============================================================================
# 端点：/health
# ============================================================================

@app.get("/health")
async def health():
    state = get_state()
    auth = {} if state.mock_dir is not None else (state.client.session.get("auth") or {})
    expires = int(auth.get("expiresAt") or 0)
    return {
        "status": "ok",
        "authenticated": bool(auth.get("accessToken")),
        "token_valid": not expires or expires > int(time.time() * 1000),
        "uptime_seconds": int(time.time() - state.started_at),
    }


# ============================================================================
# 端点：/v1/models
# ============================================================================

@app.get("/v1/models")
async def list_models():
    state = get_state()
    log_client_request("GET", "/v1/models", None)
    state.ensure_auth()
    
    # 简化版：直接返回常用模型
    models = [
        {"id": "deepseek-v4-flash", "name": "DeepSeek V4 Flash", "vendor": "deepseek"},
        {"id": "glm-5.2", "name": "GLM-5.2", "vendor": "zhipu"},
        {"id": "deepseek-v4-pro", "name": "DeepSeek V4 Pro", "vendor": "deepseek"},
        {"id": "auto", "name": "Auto", "vendor": "codebuddy"},
    ]
    
    data = [
        {
            "id": m["id"],
            "object": "model",
            "created": 0,
            "owned_by": m.get("vendor", "codebuddy"),
            "name": m.get("name", m["id"]),
        }
        for m in models
    ]
    
    return {"object": "list", "data": data}


# ============================================================================
# 端点：/v1/chat/completions
# ============================================================================

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    state = get_state()
    body = await request.json()
    
    log_client_request("POST", "/v1/chat/completions", body)
    diagnostic("request", protocol="openai", **body_summary(body))
    
    return await forward_chat(body, "openai")


# ============================================================================
# 端点：/v1/responses
# ============================================================================

@app.post("/v1/responses")
async def create_response(request: Request):
    state = get_state()
    body = await request.json()
    
    log_client_request("POST", "/v1/responses", body)
    
    # 转换 Responses → Chat
    chat_body = responses_to_chat(body)
    
    # 消息压缩优化（如果启用）
    if state.enable_optimize_context and HAS_PROJECTION:
        chat_body, proj_stats = project_responses_chat_body(chat_body)
        diagnostic("projection_applied", protocol="responses", **proj_stats)
    
    diagnostic("request", protocol="responses", **body_summary(chat_body))
    
    return await forward_chat(chat_body, "responses", original=body)


# ============================================================================
# 端点：/v1/messages
# ============================================================================

@app.post("/v1/messages")
async def create_message(request: Request):
    state = get_state()
    body = await request.json()
    
    log_client_request("POST", "/v1/messages", body)
    
    # 转换 Anthropic → Chat
    chat_body = anthropic_to_chat(body)
    diagnostic("request", protocol="anthropic", **body_summary(chat_body))
    
    return await forward_chat(chat_body, "anthropic", original=body)


# ============================================================================
# 核心：转发请求到上游
# ============================================================================

async def forward_chat(
    body: dict[str, Any],
    protocol: str,
    original: dict[str, Any] | None = None
) -> StreamingResponse | JSONResponse:
    """转发 chat 请求到 CodeBuddy 上游，支持流式和非流式。"""
    state = get_state()
    state.ensure_auth()
    
    diagnostic("upstream_request", protocol=protocol, **body_summary(body))
    
    stream = bool(body.get("stream"))
    upstream_body = dict(body)
    
    # 限制 tools 数量防止上游拒绝 (CodeBuddy 限制约 30-50 个工具)
    original_tool_count = len(upstream_body.get("tools", []))
    MAX_TOOLS = 30
    if original_tool_count > MAX_TOOLS:
        upstream_body["tools"] = upstream_body["tools"][:MAX_TOOLS]
        diagnostic("tools_truncated", 
                  original_count=original_tool_count,
                  truncated_count=MAX_TOOLS,
                  reason="Upstream API tool limit")
    
    # 应用脱敏处理
    if state.enable_desensitize:
        upstream_body = desensitize_body(upstream_body, compact_harness=True)
    
    # 始终以流式方式请求上游（聚合或转发）
    upstream_body["stream"] = True
    upstream_body.setdefault("stream_options", {"include_usage": True})
    
    
    url = state.client.endpoint + "/v2/chat/completions"
    headers = {
        **state.client._auth_headers(),
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    
    if stream:
        # 流式：直接转发
        return StreamingResponse(
            stream_upstream(url, headers, upstream_body, protocol, original),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "close"}
        )
    else:
        # 非流式：聚合后返回
        collected = await collect_upstream(url, headers, upstream_body, protocol)
        return JSONResponse(content=convert_nonstream(collected, protocol, original))


# ============================================================================
# 异步流式转发（核心改进）
# ============================================================================

async def stream_upstream(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    protocol: str,
    original: dict[str, Any] | None
):
    """异步流式转发上游响应到客户端。
    
    关键改进：
    1. 使用 httpx.AsyncClient 异步请求
    2. aiter_lines() 自动处理行分割和超时
    3. 记录流开始/进度/完成日志
    """
    state = get_state()
    stream_start_time = time.time()
    
    # 【日志】流开始
    state.write_log("stream_started", protocol=protocol, timestamp=stream_start_time)
    diagnostic("stream_started", protocol=protocol)
    
    
    response_id = "resp_" + uuid.uuid4().hex
    anthropic_state = AnthropicStreamState(
        (original or {}).get("model", "default")
    ) if protocol == "anthropic" and AnthropicStreamState else None
    
    # DSML 缓冲区（用于处理可能的文本标记格式工具调用）
    dsml_buffer = DSMLStreamBuffer()
    
    emitted_response_created = False
    response_text = ""
    response_text_started = False
    chunk_count = 0
    done_seen = False
    raw_chunks: list[bytes] = []
    last_progress_log = stream_start_time
    detected_tool_calls = []
    try:
        # 异步HTTP客户端：timeout=None 依赖TCP超时
        # 使用合理的超时配置：连接超时30s，读取超时300s
        timeout_config = httpx.Timeout(30.0, read=300.0)
        async with httpx.AsyncClient(timeout=timeout_config) as client:
            async with client.stream("POST", url, headers=headers, json=body) as resp:
                if resp.status_code != 200:
                    error_body = await resp.aread()
                    error_text = error_body.decode("utf-8", "replace")
                    
                    diagnostic("upstream_error", protocol=protocol,
                        status=resp.status_code,
                        detail=error_text[:500])
                    
                    # 返回结构化错误（包含详细信息）
                    if protocol == "anthropic":
                        # Anthropic error format
                        error_event = {
                            "type": "error",
                            "error": {
                                "type": "api_error",
                                "message": f"Upstream API error (HTTP {resp.status_code}): {error_text[:200]}"
                            }
                        }
                        yield f"event: error\ndata: {json.dumps(error_event, ensure_ascii=False)}\n\n".encode()
                    else:
                        # OpenAI error format
                        error_chunk = {
                            "error": {
                                "message": f"Upstream API error (HTTP {resp.status_code})",
                                "type": "upstream_error",
                                "code": resp.status_code,
                                "details": error_text[:500]
                            }
                        }
                        yield f"data: {json.dumps(error_chunk, ensure_ascii=False)}\n\n".encode()
                    
                    return
                
                diagnostic("upstream_response", protocol=protocol, status=resp.status_code)
                
                # 异步迭代行（自动处理超时和分块）
                async for line in resp.aiter_lines():
                    # 【日志】进度记录
                    if chunk_count > 0 and chunk_count % 10 == 0:
                        now = time.time()
                        if now - last_progress_log >= 5:
                            diagnostic("stream_progress", protocol=protocol,
                                chunks=chunk_count,
                                elapsed=round(now - stream_start_time, 2))
                            last_progress_log = now
                    
                    line = line.strip()
                    raw_chunks.append(line.encode("utf-8"))
                    
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
                    
                    # 根据协议转换事件
                    if protocol == "openai":
                        # 提取 content 并通过 DSML 缓冲区处理
                        chunk_content = str(
                            ((chunk.get("choices") or [{}])[0].get("delta") or {}).get("content") or ""
                        )
                        
                        if chunk_content:
                            # 使用 DSML 缓冲区处理（清理标记，检测工具调用）
                            cleaned_content, chunk_tool_calls = dsml_buffer.add_chunk(chunk_content)
                            
                            # 累积清理后的文本
                            if cleaned_content:
                                response_text += cleaned_content
                            
                            # 记录检测到的工具调用
                            if chunk_tool_calls:
                                detected_tool_calls.extend(chunk_tool_calls)
                            
                            # 修改 chunk 中的 content 为清理后的内容
                            if "choices" in chunk and len(chunk["choices"]) > 0:
                                if "delta" not in chunk["choices"][0]:
                                    chunk["choices"][0]["delta"] = {}
                                chunk["choices"][0]["delta"]["content"] = cleaned_content
                        
                        # 如果检测到工具调用，添加 tool_calls 字段
                        if detected_tool_calls and dsml_buffer.should_emit_tool_calls():
                            if "choices" in chunk and len(chunk["choices"]) > 0:
                                chunk["choices"][0]["finish_reason"] = "tool_calls"
                                # 将检测到的工具调用转换为 OpenAI 格式
                                chunk["choices"][0]["delta"]["tool_calls"] = [
                                    {
                                        "index": idx,
                                        "id": f"call_{uuid.uuid4().hex[:24]}",
                                        "type": "function",
                                        "function": {
                                            "name": tc["name"],
                                            "arguments": json.dumps(tc["input"], ensure_ascii=False)
                                        }
                                    }
                                    for idx, tc in enumerate(detected_tool_calls)
                                ]
                        
                        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
                    
                    elif protocol == "responses":
                        if not emitted_response_created:
                            emitted_response_created = True
                            event = {
                                "type": "response.created",
                                "response": {
                                    "id": response_id,
                                    "object": "response",
                                    "status": "in_progress",
                                    "created_at": now_s(),
                                    "output": []
                                }
                            }
                            yield f"event: response.created\ndata: {json.dumps(event, ensure_ascii=False)}\n\n".encode()
                        
                        chunk_content = ((chunk.get("choices") or [{}])[0].get("delta") or {}).get("content") or ""
                        
                        if chunk_content:
                            # 使用 DSML 缓冲区处理
                            cleaned_content, chunk_tool_calls = dsml_buffer.add_chunk(chunk_content)
                            
                            # 累积清理后的文本
                            response_text += cleaned_content
                            
                            # 记录检测到的工具调用
                            if chunk_tool_calls:
                                detected_tool_calls.extend(chunk_tool_calls)
                            
                            # 首次有内容时发送 output_item.added 和 content_part.added
                            if cleaned_content and not response_text_started:
                                response_text_started = True
                                # 发送 output_item.added 和 content_part.added
                                yield f"event: response.output_item.added\ndata: {json.dumps({'type': 'response.output_item.added', 'output_index': 0, 'item': {'id': response_id, 'type': 'message', 'role': 'assistant'}}, ensure_ascii=False)}\n\n".encode()
                                yield f"event: response.content_part.added\ndata: {json.dumps({'type': 'response.content_part.added', 'item_id': response_id, 'output_index': 0, 'content_index': 0, 'part': {'type': 'output_text', 'text': '', 'annotations': []}}, ensure_ascii=False)}\n\n".encode()
                        
                        # 发送 delta 事件
                        for event_name, event_data in response_events_from_chunk(chunk, response_id):
                            yield f"event: {event_name}\ndata: {json.dumps(event_data, ensure_ascii=False)}\n\n".encode()
                    elif protocol == "anthropic" and anthropic_state:
                        # 提取 content 并通过 DSML 缓冲区处理
                        chunk_content = str(
                            ((chunk.get("choices") or [{}])[0].get("delta") or {}).get("content") or ""
                        )
                        
                        if chunk_content:
                            # 使用 DSML 缓冲区处理（清理标记，检测工具调用）
                            cleaned_content, chunk_tool_calls = dsml_buffer.add_chunk(chunk_content)
                            
                            # 累积清理后的文本
                            if cleaned_content:
                                response_text += cleaned_content
                            
                            # 记录检测到的工具调用
                            if chunk_tool_calls:
                                detected_tool_calls.extend(chunk_tool_calls)
                            
                            # ✅ 关键修复：在传递给 AnthropicStreamConverter 之前，先修改 chunk
                            if "choices" in chunk and len(chunk["choices"]) > 0:
                                if "delta" not in chunk["choices"][0]:
                                    chunk["choices"][0]["delta"] = {}
                                # 使用清理后的内容替换原始内容
                                chunk["choices"][0]["delta"]["content"] = cleaned_content
                            
                            # 如果检测到工具调用，添加 tool_calls 字段
                            if chunk_tool_calls and dsml_buffer.should_emit_tool_calls():
                                if "choices" in chunk and len(chunk["choices"]) > 0:
                                    chunk["choices"][0]["finish_reason"] = "tool_calls"
                                    chunk["choices"][0]["delta"]["tool_calls"] = [
                                        {
                                            "index": idx,
                                            "id": call.get("id", f"call_{uuid.uuid4().hex[:24]}"),
                                            "type": "function",
                                            "function": {
                                                "name": call["function"]["name"],
                                                "arguments": call["function"]["arguments"]
                                            }
                                        }
                                        for idx, call in enumerate(chunk_tool_calls)
                                    ]
                        
                        # 转换为 Anthropic 事件（此时 chunk 已经被清理）
                        events = anthropic_state.feed_chunk(chunk)
                        for event_name, event_data in events:
                            yield f"event: {event_name}\ndata: {json.dumps(event_data, ensure_ascii=False)}\n\n".encode()
                
                # 发送结束事件
                if protocol == "responses" and response_text_started:
                    for event_name, payload in [
                        ("response.output_text.done", {
                            "type": "response.output_text.done",
                            "item_id": response_id,
                            "output_index": 0,
                            "content_index": 0,
                            "text": response_text
                        }),
                        ("response.content_part.done", {
                            "type": "response.content_part.done",
                            "item_id": response_id,
                            "output_index": 0,
                            "content_index": 0,
                            "part": {"type": "output_text", "text": response_text, "annotations": []}
                        }),
                        ("response.output_item.done", {
                            "type": "response.output_item.done",
                            "output_index": 0,
                            "item": {
                                "id": response_id,
                                "type": "message",
                                "role": "assistant",
                                "content": [{"type": "output_text", "text": response_text, "annotations": []}]
                            }
                        }),
                        ("response.completed", {
                            "type": "response.completed",
                            "response": {
                                "id": response_id,
                                "object": "response",
                                "status": "completed",
                                "output_text": response_text
                            }
                        }),
                    ]:
                        yield f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()
                
                elif protocol == "anthropic" and anthropic_state:
                    for event_name, event_data in anthropic_state.finish():
                        yield f"event: {event_name}\ndata: {json.dumps(event_data, ensure_ascii=False)}\n\n".encode()
                
                elif protocol == "openai":
                    yield b"data: [DONE]\n\n"
    
    except httpx.TimeoutException as exc:
        # 【日志】超时
        diagnostic("stream_timeout", protocol=protocol, chunks=chunk_count,
            elapsed=round(time.time() - stream_start_time, 2), error=str(exc))
        state.write_log("stream_timeout", protocol=protocol, chunks=chunk_count, error=str(exc))
        error_chunk = {
            "error": {
                "message": f"stream timeout after {chunk_count} chunks",
                "type": "timeout_error"
            }
        }
        yield f"data: {json.dumps(error_chunk, ensure_ascii=False)}\n\n".encode()
    
    except Exception as exc:
        # 【日志】其他错误
        diagnostic("stream_error", protocol=protocol, chunks=chunk_count,
            elapsed=round(time.time() - stream_start_time, 2), error=str(exc))
        state.write_log("stream_error", protocol=protocol, chunks=chunk_count, error=str(exc))
        error_chunk = {
            "error": {
                "message": f"stream error: {exc}",
                "type": "internal_error"
            }
        }
        yield f"data: {json.dumps(error_chunk, ensure_ascii=False)}\n\n".encode()
    
    finally:
        # 【日志】流完成
        if state.verbose_llm:
            raw_response = b"\n".join(raw_chunks)
            state.write_body_log("upstream_response", raw_response, protocol=protocol,
                                status=200, method="POST", path="/v2/chat/completions")
        
        logged_text = anthropic_state.text if anthropic_state else response_text
        stream_duration = round(time.time() - stream_start_time, 2)
        
        log_upstream_response(protocol, logged_text, stream=True,
                            chunk_count=chunk_count, duration=stream_duration,
                            upstream_done=done_seen)


# ============================================================================
# 异步聚合流式响应
# ============================================================================

async def collect_upstream(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    protocol: str
) -> dict[str, Any]:
    """聚合上游流式响应为单个 JSON 对象（非流式场景）。"""
    state = get_state()
    
    usage = None
    finish_reason = None
    content = ""
    tool_calls = []
    
    # DSML 缓冲区
    dsml_buffer = DSMLStreamBuffer()
    
    try:
        # 使用合理的超时配置：连接超时30s，读取超时300s
        timeout_config = httpx.Timeout(30.0, read=300.0)
        async with httpx.AsyncClient(timeout=timeout_config) as client:
            async with client.stream("POST", url, headers=headers, json=body) as resp:
                if resp.status_code != 200:
                    error_body = await resp.aread()
                    raise HTTPException(
                        status_code=resp.status_code,
                        detail={"error": {"message": error_body.decode("utf-8", "replace")[:500], "type": "upstream_error"}}
                    )
                
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    
                    usage = chunk.get("usage") or usage
                    
                    for choice in chunk.get("choices") or []:
                        finish_reason = choice.get("finish_reason") or finish_reason
                        delta = choice.get("delta") or {}
                        
                        # 处理 content（可能包含 DSML）
                        if delta.get("content"):
                            chunk_content = delta["content"]
                            
                            # 使用 DSML 缓冲区处理
                            cleaned_content, detected_tool_calls = dsml_buffer.add_chunk(chunk_content)
                            
                            # 累积清理后的 content
                            if cleaned_content:
                                content += cleaned_content
                            
                            # 如果检测到 tool_calls
                            if detected_tool_calls:
                                tool_calls.extend(detected_tool_calls)
                        
                        # 处理原生 tool_calls（合并）
                        if delta.get("tool_calls"):
                            for tc in delta["tool_calls"]:
                                idx = tc.get("index", 0)
                                while len(tool_calls) <= idx:
                                    tool_calls.append({"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                                if tc.get("id"):
                                    tool_calls[idx]["id"] = tc["id"]
                                if tc.get("function", {}).get("name"):
                                    tool_calls[idx]["function"]["name"] = tc["function"]["name"]
                                if tc.get("function", {}).get("arguments"):
                                    tool_calls[idx]["function"]["arguments"] += tc["function"]["arguments"]
    
    except httpx.HTTPError as exc:
        diagnostic("upstream_error", protocol=protocol, error=str(exc))
        raise HTTPException(status_code=502, detail={"error": {"message": f"upstream error: {exc}", "type": "upstream_error"}})
    
    # 如果检测到 DSML tool_calls，修改 finish_reason
    if tool_calls and dsml_buffer.should_emit_tool_calls():
        finish_reason = "tool_calls"
    
    
    # 【日志】收集完成
    if state.verbose_llm:
        # collect_upstream 没有保存原始响应，只记录聚合后的内容
        pass
    
    log_upstream_response(protocol, content, stream=False)
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex,
        "object": "chat.completion",
        "created": now_s(),
        "model": body.get("model", "auto"),
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": content,
                "tool_calls": tool_calls if tool_calls else None
            },
            "finish_reason": finish_reason or "stop"
        }],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    }


# ============================================================================
# 协议转换
# ============================================================================

def convert_nonstream(data: dict[str, Any], protocol: str, original: dict[str, Any] | None) -> dict[str, Any]:
    """将聚合的 OpenAI 格式转换为目标协议格式。"""
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
            content_blocks.append({
                "type": "tool_use",
                "id": call.get("id", ""),
                "name": fn.get("name", ""),
                "input": arguments
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
                "output_tokens": (data.get("usage") or {}).get("completion_tokens", 0)
            }
        }
    
    elif protocol == "responses":
        return {
            "id": "resp_" + uuid.uuid4().hex,
            "object": "response",
            "created_at": now_s(),
            "status": "completed",
            "output": [{
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": content}]
            }]
        }
    
    return data


# ============================================================================
# 启动
# ============================================================================

def main():
    global proxy_state
    
    parser = argparse.ArgumentParser(description="CodeBuddy local API proxy")
    parser.add_argument("--host", default=os.getenv("CODEBUDDY_PROXY_HOST", "127.0.0.1"),
                        help="监听地址")
    parser.add_argument("--port", type=int, default=int(os.getenv("CODEBUDDY_PROXY_PORT", "8787")),
                        help="监听端口")
    parser.add_argument("--endpoint", default=os.getenv("CODEBUDDY_ENDPOINT", "https://copilot.tencent.com"),
                        help="CodeBuddy 后端地址")
    parser.add_argument("--session-file", type=pathlib.Path,
                        help="会话文件路径")
    parser.add_argument("--mock-dir", type=pathlib.Path,
                        help="只使用指定目录中的真实响应 fixture，不访问 CodeBuddy 后端")
    parser.add_argument("--log-file", type=pathlib.Path,
                        default=pathlib.Path(os.getenv("CODEBUDDY_PROXY_LOG_FILE", "logs/codebuddy-proxy.jsonl")),
                        help="记录完整请求/响应的 JSONL 文件（默认 logs/codebuddy-proxy.jsonl）")
    parser.add_argument("--desensitize", action="store_true",
                        help="启用脱敏处理，对 system 消息中的敏感词插入零宽空格（缓解审核误拦）")
    parser.add_argument("--optimize-context", action="store_true",
                        help="启用消息压缩优化（仅 /v1/responses），大幅减少 token 使用（适用于 Codex CLI 等长上下文场景）")
    parser.add_argument("--login", action="store_true",
                        help="启动时执行浏览器登录/账户查询")
    parser.add_argument("--no-browser", action="store_true",
                        help="登录时不自动打开浏览器")
    parser.add_argument("--verbose-llm", action="store_true",
                        help="log full LLM request/response content (default: summary only, saves 98%% space)")
    args = parser.parse_args()
    
    # 设置日志
    log_dir = args.log_file.parent if args.log_file else pathlib.Path("logs")
    logger = setup_logging(log_dir)
    
    # 初始化客户端
    client = CodeBuddyClient(args.endpoint, session_file=args.session_file)
    
    # 处理登录
    if args.login:
        client.login(open_browser=not args.no_browser)
    
    # 创建全局状态
    proxy_state = ProxyState(
        client=client,
        mock_dir=args.mock_dir,
        log_file=args.log_file,
        enable_desensitize=args.desensitize,
        enable_optimize_context=args.optimize_context,
        verbose_llm=args.verbose_llm,
        logger=logger
    )
    
    # 启动信息输出到 stdout
    print(f"CodeBuddy proxy listening on http://{args.host}:{args.port}")
    print("Endpoints: /v1/models /v1/chat/completions /v1/responses /v1/messages /health")
    
    # 同时记录到日志
    logger.info(f"CodeBuddy proxy listening on http://{args.host}:{args.port}")
    logger.info("Endpoints: /v1/models /v1/chat/completions /v1/responses /v1/messages /health")
    
    # 启动 uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")

if __name__ == "__main__":
    main()
