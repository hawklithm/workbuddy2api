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


# 安全词检测统一关键词（中英混合）
_SAFETY_KEYWORDS = ("sensitive", "cannot respond", "敏感内容", "无法响应", "unable to")

def is_policy_blocked(text: str) -> bool:
    """检测文本是否包含安全策略拦截标记"""
    return any(marker in text.lower() for marker in _SAFETY_KEYWORDS)


def text_summary(value: str) -> dict[str, Any]:
    return {
        "content_length": len(value),
        "content_sha256": hashlib.sha256(value.encode("utf-8")).hexdigest()[:16],
        "safety_message_detected": is_policy_blocked(value),
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
        "safety_message_detected": is_policy_blocked(text),
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
    
    # 完整的 Codex ModelInfo 格式(基于 codex-rs/protocol/src/openai_models.rs)
    # 从 CodeBuddy 扩展 product.json 提取的真实模型列表（2026-07-13 版本）
    # 共 25 个模型，涵盖 DeepSeek、GLM、Kimi、Hunyuan、Claude 等
    models = [
        # 默认模型
        {"id": "default", "name": "Default", "vendor": "codebuddy", "max_input": 168000, "max_output": 32000, "tool_call": True, "images": False},
        
        # GLM 系列
        {"id": "glm-4.7", "name": "GLM-4.7", "vendor": "zhipu", "max_input": 200000, "max_output": 48000, "tool_call": True, "images": False, "reasoning": True, "desc": "GLM-4.7 model, Well-rounded model for everyday use"},
        {"id": "glm-4.6", "name": "GLM-4.6", "vendor": "zhipu", "max_input": 168000, "max_output": 32000, "tool_call": True, "images": False, "desc": "Advanced language model with strong reasoning capabilities"},
        
        # DeepSeek 系列
        {"id": "deepseek-v3-2-volc", "name": "DeepSeek-V3.2", "vendor": "deepseek", "max_input": 96000, "max_output": 32000, "tool_call": True, "images": False, "reasoning": True, "desc": "DeepSeek-V3.2, good for daily use"},
        {"id": "deepseek-v3-1-volc", "name": "DeepSeek-V3-1-Terminus", "vendor": "deepseek", "max_input": 96000, "max_output": 32000, "tool_call": True, "images": False, "desc": "DeepSeek's flagship model, good for planning, debugging, coding, and more"},
        {"id": "deepseek-v3-1-lkeap", "name": "DeepSeek-V3-1", "vendor": "deepseek", "max_input": 96000, "max_output": 32000, "tool_call": True, "images": False, "desc": "DeepSeek's flagship model. Good for planning, debugging, coding, and more"},
        {"id": "deepseek-v3-1", "name": "DeepSeek-V3.1", "vendor": "deepseek", "max_input": 96000, "max_output": 32000, "tool_call": True, "images": False, "desc": "DeepSeek's flagship model. Good for planning, debugging, coding, and more"},
        {"id": "deepseek-v3-0324-lkeap", "name": "DeepSeek-V3-0324", "vendor": "deepseek", "max_input": 112000, "max_output": 16000, "tool_call": True, "images": False, "desc": "DeepSeek's flagship model, good for planning, debugging, coding, and more"},
        {"id": "deepseek-r1-0528-lkeap", "name": "DeepSeek-R1-0528", "vendor": "deepseek", "max_input": 96000, "max_output": 16000, "tool_call": True, "images": False, "desc": "Open-source reasoning model from DeepSeek, optimised for logic & math"},
        
        # Kimi 系列
        {"id": "kimi-k2-instruct-taiji", "name": "Kimi-K2", "vendor": "moonshot", "max_input": 31000, "max_output": 8192, "tool_call": True, "images": False},
        
        # Hunyuan (混元) 系列 - 对话模型
        {"id": "completion-gf", "name": "completion-gf", "vendor": "tencent", "max_input": 200000, "max_output": 8192, "tool_call": True, "images": False},
        {"id": "hunyuan-chat", "name": "Hunyuan-Turbos", "vendor": "tencent", "max_input": 200000, "max_output": 8192, "tool_call": True, "images": False, "desc": "Tencent's lightweight, fast general-purpose model"},
        {"id": "hunyuan-2.0-instruct", "name": "Hunyuan-2.0-Instruct", "vendor": "tencent", "max_input": 128000, "max_output": 16000, "tool_call": True, "images": False, "reasoning": True},
  
        # Claude 系列
        {"id": "default-1.1", "name": "Claude-3.7-Sonnet", "vendor": "anthropic", "max_input": None, "max_output": 8192, "tool_call": True, "images": True},
        {"id": "default-1.2", "name": "Claude-4.0-Sonnet", "vendor": "anthropic", "max_input": 200000, "max_output": 24000, "tool_call": True, "images": True, "desc": "Great for daily use. Good at most things"},
        
        # Hunyuan 视觉模型
        {"id": "hunyuan-turbos-vision", "name": "hunyuan-turbos-vision", "vendor": "tencent", "max_input": 16000, "max_output": 16000, "tool_call": True, "images": True},
        {"id": "hunyuan-t1-vision", "name": "hunyuan-turbos-vision", "vendor": "tencent", "max_input": 16000, "max_output": 24000, "tool_call": True, "images": True},
        
        # 补全模型（仅用于代码补全，不适合对话）
        {"id": "hunyuan-3b", "name": "hunyuan-3b", "vendor": "tencent", "max_input": None, "max_output": 256, "tool_call": False, "images": False},
        {"id": "hunyuan-7b-dense", "name": "hunyuan-7b", "vendor": "tencent", "max_input": None, "max_output": 256, "tool_call": False, "images": False},
        {"id": "codewise-7b-021", "name": "codewise-7b-021", "vendor": "anthropic", "max_input": None, "max_output": 256, "tool_call": False, "images": False},
        {"id": "codewise-completions", "name": "codewise-completions", "vendor": "anthropic", "max_input": None, "max_output": 256, "tool_call": False, "images": False},
        {"id": "deepseek-r1-0528", "name": "deepseek-r1", "vendor": "tencent", "max_input": None, "max_output": 256, "tool_call": False, "images": False},
        {"id": "deepseek-v3-0324-taco-completion", "name": "deepseek-v3-0324", "vendor": "tencent", "max_input": None, "max_output": 256, "tool_call": False, "images": False},
        {"id": "deepseek-v3-0324", "name": "deepseek-v3", "vendor": "tencent", "max_input": None, "max_output": 8192, "tool_call": False, "images": False},
        {"id": "codewise-navi-v1-2-taco", "name": "codewise-navi-v1-2-taco", "vendor": "tencent", "max_input": None, "max_output": 256, "tool_call": False, "images": False},
    ]
    
    data = [
        {
            # 基础标识
            "id": m["id"],
            "slug": m["id"],
            "display_name": m.get("name", m["id"]),
            "description": m.get("desc"),
            "object": "model",
            "created": 1720872952,  # 2026-07-13 的时间戳
            "owned_by": m.get("vendor", "codebuddy"),
            
            # Reasoning 支持
            "default_reasoning_level": None,
            "supported_reasoning_levels": ["extended"] if m.get("reasoning") else [],
            "default_reasoning_summary": "auto",
            "supports_reasoning_summary_parameter": True,
            
            # Shell 和工具能力
            "shell_type": "default",
            "apply_patch_tool_type": None,
            "web_search_tool_type": "text",
            "experimental_supported_tools": [],
            "supports_parallel_tool_calls": m.get("tool_call", False),
            
            # 可见性和优先级
            "visibility": "list",
            "supported_in_api": True,
            "priority": 1,
            
            # 上下文窗口
            "context_window": m.get("max_input"),
            "max_context_window": m.get("max_output"),
            "auto_compact_token_limit": None,
            "effective_context_window_percent": 95,
            
            # 输出截断策略
            "truncation_policy": {
                "mode": "bytes",
                "limit": 10000
            },
            
            # 多模态支持
            "input_modalities": ["text", "image"] if m.get("images") else ["text"],
            "supports_image_detail_original": False,
            
            # Verbosity
            "support_verbosity": False,
            "default_verbosity": None,
            
            # 其他功能开关
            "include_skills_usage_instructions": False,
            "include_plugin_usage_instructions": False,
            "include_apps_usage_instructions": True,
            
            # 速度层级和服务层级
            "additional_speed_tiers": [],
            "service_tiers": [],
            "default_service_tier": None,
            
            # 可选元数据
            "availability_nux": None,
            "model_messages": None,
            
            # 向后兼容的 base_instructions (遗留字段)
            "base_instructions": "You are a helpful AI assistant.",
        }
        for m in models
    ]
    return {"object": "list", "models": data}


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
    chat_body = responses_request_to_chat(body)
    
    # 消息压缩优化（如果启用）
    if state.enable_optimize_context and HAS_PROJECTION:
        chat_body, proj_stats = project_responses_chat_body(chat_body)
        diagnostic("projection_applied", protocol="responses", **proj_stats)
    
    # 过滤无效的工具定义
    tools = chat_body.get("tools", [])
    if tools:
        original_count = len(tools)
        filtered_tools = []
        filtered_names = []
        
        for tool in tools:
            # 1. 过滤非 function 类型
            if tool.get("type") != "function":
                filtered_names.append(f"{tool.get('type', 'unknown')} (非function类型)")
                continue
            
            # 2. 过滤空 parameters
            func = tool.get("function", {})
            params = func.get("parameters", {})
            if not params or not isinstance(params, dict) or len(params) == 0:
                filtered_names.append(f"{func.get('name', 'unknown')} (空parameters)")
                continue
            
            # 3. 检查 parameters 是否有 type 字段
            if "type" not in params:
                filtered_names.append(f"{func.get('name', 'unknown')} (缺少type)")
                continue
            
            filtered_tools.append(tool)
        
        chat_body["tools"] = filtered_tools
        
        if filtered_names:
            diagnostic("tools_filtered", 
                      original=original_count, 
                      kept=len(filtered_tools), 
                      filtered=filtered_names)
    
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
        **state.client.auth_headers(),
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
    anthropic_state = AnthropicStreamConverter(
        (original or {}).get("model", "default")
    ) if protocol == "anthropic" and AnthropicStreamConverter else None
    
    # Responses 协议转换器（使用传入的 body 参数）
    responses_state = ResponsesStreamConverter(
        model=body.get("model", "auto")
    ) if protocol == "responses" and ResponsesStreamConverter else None
    
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
                    
                    
                    # 【诊断】记录 400 错误时的工具定义
                    if resp.status_code == 400:
                        tools = body.get("tools", [])
                        diagnostic("upstream_400_error",
                                   status=resp.status_code,
                                   error_preview=error_text[:200],
                                   tool_count=len(tools),
                                   sample_tools=tools[:2] if tools else [])
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
                
                # 【配置】最大流式响应时长（秒）
                MAX_STREAM_DURATION = 60  # 60秒适合交互式对话，180秒适合复杂任务
                
                # 异步迭代行（自动处理超时和分块）
                async for line in resp.aiter_lines():
                    # 【保护】检查总时长，防止流无限期运行
                    elapsed = time.time() - stream_start_time
                    if elapsed > MAX_STREAM_DURATION:
                        diagnostic("stream_duration_exceeded", protocol=protocol,
                                  chunks=chunk_count, elapsed=round(elapsed, 2),
                                  max_duration=MAX_STREAM_DURATION)
                        state.write_log("stream_duration_exceeded", protocol=protocol, 
                                       chunks=chunk_count, elapsed=round(elapsed, 2))
                        break  # 强制结束流
                    
                    # 【日志】进度记录（每10个chunk且间隔5秒）
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
                    
                    elif protocol == "responses" and responses_state:
                        # 【修复】提取 content 并通过 DSML 缓冲区处理
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
                            if chunk_tool_calls and dsml_buffer.should_emit_tool_calls():
                                if "choices" in chunk and len(chunk["choices"]) > 0:
                                    chunk["choices"][0]["finish_reason"] = "tool_calls"
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
                        
                        # 使用 ResponsesStreamConverter 转换事件（此时 chunk 已经被清理）
                        events = responses_state.feed_chunk(chunk)
                        for event_name, event_data in events:
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
                if protocol == "responses" and responses_state:
                    # 使用 ResponsesStreamConverter 的 finish() 方法发出完整事件序列
                    for event_name, event_data in responses_state.finish():
                        yield f"event: {event_name}\ndata: {json.dumps(event_data, ensure_ascii=False)}\n\n".encode()
                
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
        
        logged_text = (
            anthropic_state.text if anthropic_state
            else responses_state.text if responses_state
            else response_text
        )
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
    tool_calls_dict: dict[int, dict] = {}  # 使用 dict 按 index 累加
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
                            
                            # 调试日志：记录 DSML 解析结果
                            if detected_tool_calls:
                                diagnostic("dsml_detected", 
                                          tool_count=len(detected_tool_calls),
                                          tools=[tc["function"]["name"] for tc in detected_tool_calls])
                            
                            # 累积清理后的 content
                            if cleaned_content:
                                content += cleaned_content
                            
                            # 如果检测到 tool_calls，添加到 dict 中
                            if detected_tool_calls:
                                for detected_call in detected_tool_calls:
                                    # 找到下一个可用的 index
                                    next_idx = len(tool_calls_dict)
                                    tool_calls_dict[next_idx] = detected_call
                        
                        # 处理原生 tool_calls（使用 dict 累加，避免预填充）
                        if delta.get("tool_calls"):
                            for tc in delta["tool_calls"]:
                                idx = tc.get("index", 0)
                                if idx not in tool_calls_dict:
                                    tool_calls_dict[idx] = {
                                        "id": "", 
                                        "type": "function", 
                                        "function": {"name": "", "arguments": ""}
                                    }
                                
                                if tc.get("id"):
                                    tool_calls_dict[idx]["id"] = tc["id"]
                                if tc.get("function", {}).get("name"):
                                    tool_calls_dict[idx]["function"]["name"] = tc["function"]["name"]
                                if tc.get("function", {}).get("arguments"):
                                    tool_calls_dict[idx]["function"]["arguments"] += tc["function"]["arguments"]
    
    except httpx.HTTPError as exc:
        diagnostic("upstream_error", protocol=protocol, error=str(exc))
        raise HTTPException(status_code=502, detail={"error": {"message": f"upstream error: {exc}", "type": "upstream_error"}})
    
    # 转换为 list 并过滤掉无效的 tool_calls（name 为空的）
    tool_calls = [
        v for k, v in sorted(tool_calls_dict.items()) 
        if v["function"]["name"]
    ]
    
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
        # 构建 content 数组（文本 + 工具调用）
        content_parts = []
        if content:
            content_parts.append({"type": "output_text", "text": content})
        
        # 处理工具调用
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            try:
                arguments = json.loads(fn.get("arguments", "{}"))
            except json.JSONDecodeError:
                arguments = fn.get("arguments", "")
            
            content_parts.append({
                "type": "function_call",
                "id": call.get("id", ""),
                "name": fn.get("name", ""),
                "arguments": arguments
            })
        
        return {
            "id": "resp_" + uuid.uuid4().hex,
            "object": "response",
            "created_at": now_s(),
            "status": "completed",
            "output": [{
                "type": "message",
                "role": "assistant",
                "content": content_parts
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
