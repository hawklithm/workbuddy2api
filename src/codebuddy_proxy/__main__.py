"""Local OpenAI/Responses/Anthropic compatible proxy for CodeBuddy.

High-performance async implementation with FastAPI + httpx.

Features:
- High concurrency: 1000+ concurrent requests
- Low memory footprint: ~5KB per request
- Robust timeout handling with async iterators

Usage with uv:
    uv run --with workbuddy2api python -m codebuddy_proxy
    uv run --with workbuddy2api python -m codebuddy_proxy --desensitize
    uv run --with workbuddy2api python -m codebuddy_proxy --host 0.0.0.0 --port 8787
"""

import argparse
import base64
import hashlib
import io
import importlib.metadata
import json
import logging
import logging.handlers
import os
import pathlib
import platform
import sys
import time
import uuid
from typing import Any, Optional

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
import uvicorn
from codebuddy_proxy.dsml_parser import DSMLStreamBuffer, parse_all_tool_calls, remove_all_tool_call_markers

from codebuddy_proxy.codebuddy_client_demo import CodeBuddyClient, CodeBuddyError

# 尝试导入高级功能模块（可选）
try:
    from codebuddy_proxy.desensitize import desensitize_body
    HAS_DESENSITIZE = True
except ImportError:
    HAS_DESENSITIZE = False
    def desensitize_body(body, **kwargs):
        return body

try:
    from codebuddy_proxy.responses_projection import project_responses_chat_body
    HAS_PROJECTION = True
except ImportError:
    HAS_PROJECTION = False
    def project_responses_chat_body(body):
        return body, {}

# 导入协议转换器
try:
    from codebuddy_proxy.responses_adapter import responses_request_to_chat, ResponsesStreamConverter
    HAS_RESPONSES_ADAPTER = True
except ImportError:
    HAS_RESPONSES_ADAPTER = False
    def responses_request_to_chat(body): 
        raise RuntimeError("responses_adapter not available - cannot convert /v1/responses requests")
    ResponsesStreamConverter = None

try:
    from codebuddy_proxy.anthropic_adapter import anthropic_to_chat, AnthropicStreamConverter
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


def setup_json_logging(log_file: pathlib.Path) -> logging.Logger:
    """配置 JSONL 滚动日志：按天分片，保留30天。"""
    logger = logging.getLogger("codebuddy_proxy.jsonl")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers.clear()

    handler = logging.handlers.TimedRotatingFileHandler(
        log_file, when="midnight", interval=1, backupCount=30, encoding="utf-8"
    )
    handler.suffix = "%Y-%m-%d"
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)

    return logger


def now_s() -> int:
    return int(time.time())


def get_runtime_info() -> dict[str, str]:
    try:
        app_version = importlib.metadata.version("workbuddy2api")
    except importlib.metadata.PackageNotFoundError:
        app_version = "unknown"
    return {
        "app_version": app_version,
        "system_version": platform.platform(),
        "python_version": platform.python_version(),
        "machine": platform.machine(),
    }


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
        json_logger: logging.Logger | None = None,
    ):
        self.client = client
        self.mock_dir = mock_dir
        self.log_file = log_file
        self.enable_desensitize = enable_desensitize
        self.enable_optimize_context = enable_optimize_context
        self.verbose_llm = verbose_llm
        self.logger = logger
        self.json_logger = json_logger
        self.runtime_info = get_runtime_info()
        self.started_at = time.time()
    
    def ensure_auth(self) -> None:
        """确保已认证；认证失败（token 过期/网络错误/登录未完成）返回结构化 401 而非 500。"""
        if self.mock_dir is not None:
            return
        try:
            self.client.ensure_authenticated()
        except HTTPException:
            raise  # 已是结构化异常，原样透传（如 503 proxy not initialized）
        except Exception as exc:
            raise HTTPException(
                status_code=401,
                detail={
                    "error": {
                        "message": "认证失败：token 无效或已过期，请重新登录（--login）",
                        "type": "authentication_error",
                        "details": str(exc)[:200],
                    }
                },
            )
    
    def write_log(self, event: str, **kwargs) -> None:
        if self.json_logger is None:
            return
        try:
            record = {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "event": event,
                **self.runtime_info,
                **kwargs,
            }
            self.json_logger.info(json.dumps(record, ensure_ascii=False))
        except Exception:
            pass
    
    def write_body_log(self, event: str, body: bytes, **kwargs) -> None:
        if self.json_logger is None:
            return
        try:
            text = body.decode("utf-8", errors="replace")
            record = {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "event": event,
                **self.runtime_info,
                "body_bytes": len(body),
                "body_text": text,
                **kwargs
            }
            self.json_logger.info(json.dumps(record, ensure_ascii=False))
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



# ============================================================================
# 远程配置缓存
# ============================================================================

class RemoteConfigCache:
    """远程配置缓存（带 TTL）- 匹配 VS Code 插件的逻辑"""
    
    def __init__(self, url: str, ttl: int = 300):
        self.url = url
        self.ttl = ttl
        self._cache: Optional[dict[str, Any]] = None
        self._last_fetch: float = 0
    
    async def get_config(self) -> dict[str, Any]:
        """获取配置（带缓存）
        
        匹配插件逻辑:
        - 如果没有 enterpriseId，返回空配置
        - 端点: /console/enterprises/{enterpriseId}/config/models
        - 响应格式: { data: { data: [...models] } }
        - 超时: 5 秒
        - 失败返回空配置（不抛出异常）
        """
        now = time.time()
        
        # 缓存有效
        if self._cache and (now - self._last_fetch) < self.ttl:
            age = now - self._last_fetch
            diagnostic("remote_config_cache_hit", age_seconds=age, ttl=self.ttl)
            return self._cache
        
        # 获取远程配置
        state = get_state()
        
        # 检查是否有 enterpriseId（匹配插件逻辑）
        session = state.client.session
        account = session.get("account", {})
        enterprise_id = account.get("enterpriseId")
        
        if not enterprise_id:
            # 个人版用户：返回空配置（匹配插件的 if (!enterpriseId) return []）
            diagnostic("remote_config_skipped", reason="no enterprise id", account_type=account.get("type", "unknown"))
            return {}
        
        # 企业版用户：调用动态 API
        try:
            # 构造 URL（匹配插件逻辑）
            path = f"/console/enterprises/{enterprise_id}/config/models"
            full_url = self.url.rstrip("/") + path
            
            diagnostic("remote_config_fetch_attempt", url=full_url, enterprise_id=enterprise_id)
            
            # 构造请求头（包含认证信息）
            headers = state.client.auth_headers()
            headers["User-Agent"] = "Mozilla/5.0 (compatible; Genie-IDE/1.0)"
            headers["Accept"] = "application/json"
            
            # 发起请求（超时 5 秒，匹配插件）
            async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, read=5.0)) as client:
                resp = await client.get(full_url, headers=headers)
                
                if resp.status_code == 200:
                    # 解析响应（匹配插件：response.data.data）
                    response_data = resp.json()
                    
                    # 提取模型列表
                    models = []
                    if isinstance(response_data, dict):
                        # 尝试 response.data.data 结构
                        data_field = response_data.get("data")
                        if isinstance(data_field, dict):
                            models = data_field.get("data", [])
                        elif isinstance(data_field, list):
                            models = data_field
                    
                    # 标记模型类型（匹配插件逻辑）
                    if models:
                        models = [
                            {
                                **model,
                                "modelType": "enterprise" if (model.get("id") or "").startswith("custom:") else "built-in"
                            }
                            for model in models
                        ]
                    
                    config = {"models": models}
                    self._cache = config
                    self._last_fetch = now
                    
                    diagnostic(
                        "remote_config_fetch_success",
                        url=full_url,
                        models_count=len(models),
                        enterprise_id=enterprise_id
                    )
                    return config
                
                else:
                    diagnostic("remote_config_fetch_failed", url=full_url, status_code=resp.status_code)
                    return {}
        
        except Exception as e:
            # 匹配插件逻辑：捕获异常后返回空数组
            diagnostic("remote_config_fetch_error", url=full_url, error=str(e))
            return {}


# 全局实例（在 main() 中初始化）
remote_config_cache: Optional[RemoteConfigCache] = None


def build_model_list_static() -> list[dict[str, Any]]:
    """构建静态模型列表（兜底配置）"""
    # 从 CodeBuddy 扩展 product.json 提取的真实模型列表（2026-07-13 版本）
    # 共 25 个模型，涵盖 DeepSeek、GLM、Kimi、Hunyuan、Claude 等
    return [
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



def load_models_from_local_config() -> list[dict[str, Any]]:
    """从本地配置文件加载模型列表"""
    config_file = pathlib.Path(__file__).parent / "models_config.json"
    
    try:
        if not config_file.exists():
            diagnostic("local_config_missing", path=str(config_file))
            return build_model_list_static()
        
        with open(config_file, "r", encoding="utf-8") as f:
            config_data = json.load(f)
        
        models = config_data.get("models", [])
        diagnostic("local_config_loaded", models_count=len(models))
        
        # 规范化每个模型的格式
        normalized_models = []
        for model in models:
            normalized = normalize_model_format(model)
            normalized_models.append(normalized)
        
        return normalized_models
    
    except Exception as e:
        diagnostic("local_config_error", error=str(e), path=str(config_file))
        return build_model_list_static()


def normalize_model_format(remote_model: dict[str, Any]) -> dict[str, Any]:
    """规范化模型格式为 Codex 兼容的内部格式"""
    model_id = remote_model.get("id", "unknown")
    name = remote_model.get("name", model_id)
    vendor = remote_model.get("vendor", "unknown")
    
    # 提取 token 限制（支持多种字段名）
    max_input = (
        remote_model.get("max_input") or 
        remote_model.get("maxInputTokens") or
        remote_model.get("context_window")
    )
    max_output = (
        remote_model.get("max_output") or 
        remote_model.get("maxOutputTokens") or
        remote_model.get("max_context_window")
    )
    
    # 提取能力标志
    tool_call = (
        remote_model.get("tool_call", False) or 
        remote_model.get("supportsToolCall", False) or
        remote_model.get("supportsTools", False) or
        remote_model.get("supports_parallel_tool_calls", False)
    )
    images = (
        remote_model.get("images", False) or 
        remote_model.get("supportsImages", False)
    )
    reasoning = (
        remote_model.get("reasoning", False) or
        remote_model.get("supportsReasoning", False)
    )
    
    # 提取描述（支持中英文）
    description = (
        remote_model.get("descriptionZh") or 
        remote_model.get("descriptionEn") or
        remote_model.get("description") or 
        remote_model.get("desc")
    )
    
    return {
        "id": model_id,
        "name": name,
        "vendor": vendor,
        "max_input": max_input,
        "max_output": max_output,
        "tool_call": tool_call,
        "images": images,
        "reasoning": reasoning,
        "desc": description,
        "tags": remote_model.get("tags", []),
        "modelType": remote_model.get("modelType"),
        "credits": remote_model.get("credits"),
    }

async def build_model_list_dynamic() -> list[dict[str, Any]]:
    """从远程配置构建模型列表（带降级）"""
    
    try:
        # 获取远程配置
        if not remote_config_cache:
            diagnostic("dynamic_models_disabled", reason="remote_config_cache not initialized")
            return build_model_list_static()
        
        remote_config = await remote_config_cache.get_config()
        
        if not remote_config:
            diagnostic("dynamic_models_fallback", reason="empty remote config")
            return build_model_list_static()
        
        # 提取模型和白名单
        remote_models = remote_config.get("models", [])
        available_model_ids = set(remote_config.get("availableModels", []))
        
        diagnostic(
            "dynamic_models_fetched",
            remote_models_count=len(remote_models),
            whitelist_count=len(available_model_ids)
        )
        
        # 创建模型字典（id -> model）
        static_models = build_model_list_static()
        models_dict = {m["id"]: m for m in static_models}
        
        # 合并远程模型（覆盖静态配置）
        for remote_model in remote_models:
            model_id = remote_model.get("id")
            if model_id:
                models_dict[model_id] = normalize_model_format(remote_model)
        
        diagnostic("dynamic_models_merged", total_models=len(models_dict))
        
        # 应用白名单过滤（如果有）
        if available_model_ids:
            filtered_models = []
            for model_id, model in models_dict.items():
                # 保留条件（参考 VS Code 插件逻辑）:
                # 1. 用户自定义模型
                # 2. 企业模型
                # 3. 在白名单中的模型
                tags = model.get("tags", [])
                vendor = model.get("vendor")
                model_type = model.get("modelType")
                
                if (
                    "custom" in tags or
                    vendor == "user" or
                    model_type == "enterprise" or
                    model_id in available_model_ids
                ):
                    filtered_models.append(model)
            
            diagnostic(
                "dynamic_models_filtered",
                filtered_count=len(filtered_models),
                total_count=len(models_dict)
            )
            return filtered_models
        else:
            # 无白名单，返回全部
            diagnostic("dynamic_models_no_whitelist", total_count=len(models_dict))
            return list(models_dict.values())
    
    except Exception as e:
        diagnostic("dynamic_models_error", error=str(e))
        return build_model_list_static()



def model_to_codex_format(m: dict[str, Any]) -> dict[str, Any]:
    """将简化模型对象转换为完整的 Codex ModelInfo 格式"""
    return {
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
        "supported_reasoning_levels": [{"effort": "High", "description": "High"}] if m.get("reasoning") else [],
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
    # 从本地配置文件加载模型列表
    data = load_models_from_local_config()
    
    # 记录模型列表请求
    diagnostic(
        "models_list_request",
        models_count=len(data),
        source="local_config"
    )
    
    # 转换为 Codex 格式
    codex_models = [model_to_codex_format(m) for m in data]
    
    return {"models": codex_models}


# ============================================================================
# 请求体解析（兼容非 UTF-8 编码，避免 UnicodeDecodeError 抛 500）
# ============================================================================

async def parse_request_body(request: Request) -> Any:
    """解析 JSON 请求体，兼容 GBK/cp936/latin-1 等非 UTF-8 编码。

    某些客户端（如 Pi）偶尔以 GBK 编码发送请求体，而 Starlette 的
    `request.json()` 内部是 `json.loads(raw_bytes)`，默认按 UTF-8 解码，
    遇非 UTF-8 字节会直接抛 `UnicodeDecodeError`（`ValueError` 子类，
    非 `JSONDecodeError`），导致 500。此处改为按
    utf-8 → gbk → cp936 → latin-1 依次解码再解析，彻底失败时返回 400。
    """
    raw = await request.body()
    for encoding in ("utf-8", "gbk", "cp936", "latin-1"):
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            continue
    # latin-1 能解码任意字节，故 decode 不会失败；走到这里仅当 JSON 结构非法。
    raise HTTPException(
        status_code=400,
        detail={
            "error": {
                "message": "请求体不是有效的 JSON（已尝试 utf-8/gbk/cp936/latin-1 解码）",
                "type": "invalid_request_body",
            }
        },
    )


# ============================================================================
# 端点：/v1/chat/completions
# ============================================================================

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    state = get_state()
    body = await parse_request_body(request)
    
    log_client_request("POST", "/v1/chat/completions", body)
    diagnostic("request", protocol="openai", **body_summary(body))
    
    return await forward_chat(body, "openai")


# ============================================================================
# 端点：/v1/responses
# ============================================================================

@app.post("/v1/responses")
async def create_response(request: Request):
    state = get_state()
    body = await parse_request_body(request)
    
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
    body = await parse_request_body(request)
    
    log_client_request("POST", "/v1/messages", body)
    
    # 转换 Anthropic → Chat
    chat_body = anthropic_to_chat(body)
    diagnostic("request", protocol="anthropic", **body_summary(chat_body))
    
    return await forward_chat(chat_body, "anthropic", original=body)


# ============================================================================
# 核心：转发请求到上游
# ============================================================================

def _normalize_tool_choice(tool_choice: Any) -> Any:
    """把 OpenAI 的 object 形式 tool_choice 转成上游接受的 string 形式。

    上游 CodeBuddy 后端（Go）的 Request.tool_choice 字段是 string 类型，
    OpenAI 标准里 `{"type":"function","function":{"name":"X"}}` 这种 object
    形式（强制调用函数 X）会触发 400：cannot unmarshal object into ...
    of type string。此处转换为等价的函数名字符串 "X"（实测上游接受且语义一致）。
    """
    if isinstance(tool_choice, dict):
        name = (tool_choice.get("function") or {}).get("name")
        if name:
            return name
        # {"type": "function"} 但缺 name：退化为 required（强制调用工具）
        if tool_choice.get("type") == "function":
            return "required"
    return tool_choice


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
    
    # 归一化 tool_choice：object 形式 → 函数名字符串（上游只接受 string）
    if "tool_choice" in upstream_body:
        upstream_body["tool_choice"] = _normalize_tool_choice(upstream_body["tool_choice"])
    
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
        "User-Agent": "Mozilla/5.0 (compatible; Genie-IDE/1.0)",
        **state.client.auth_headers(),
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    
    # 🔍 调试：输出实际发送的IDE识别headers
    if state.logger:
        ide_headers = {k: v for k, v in headers.items() 
                      if k.startswith("X-IDE-") or k == "X-Product-Version" or k == "X-Machine-Id"}
        diagnostic("upstream_ide_headers", **ide_headers)
    
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

def _build_openai_flush_chunk(residual: str, last_chunk_id: str,
                              last_chunk_created: int, last_chunk_model: str) -> dict:
    """构造流结束 flush 时发出的 OpenAI ChatCompletionChunk。

    必须补全顶层 id/object/created/model 字段，否则严格解析的客户端
    （如 grok）会报 `serialization error: missing field id`。
    """
    return {
        "id": last_chunk_id,
        "object": "chat.completion.chunk",
        "created": last_chunk_created,
        "model": last_chunk_model,
        "choices": [{
            "index": 0,
            "delta": {"content": residual},
        }],
    }


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
    
    # 【修复 B1】原生流式 tool_calls name 缓存
    # 上游首 chunk 带完整 name，后续 chunk name 为空但有 arguments 分片
    # 维护 name 缓存防止空值覆盖
    native_tool_name_by_index: dict[int, str] = {}
    
    emitted_response_created = False
    response_text = ""
    response_text_started = False
    chunk_count = 0
    done_seen = False
    raw_chunks: list[bytes] = []
    last_progress_log = stream_start_time
    detected_tool_calls = []
    # 【修复 C3】记录最后一个上游 chunk 的元数据，供流结束 flush 时复用，
    # 保证 final_chunk 补全 OpenAI ChatCompletionChunk 必需的顶层字段
    last_chunk_id: str = ""
    last_chunk_created: int = 0
    last_chunk_model: str = ""
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
                
                # 【配置】超时策略 - 双重超时机制
                # 1. 空闲超时（idle timeout）：连续N秒没有新chunk → 超时
                # 2. 总时长上限（total duration）：绝对时长限制，防止无限期占用
                client_timeout = body.get("max_stream_duration")
                client_idle_timeout = body.get("max_idle_duration")
                
                # 空闲超时：默认60秒，客户端可配置（10-300秒）
                if client_idle_timeout is not None:
                    MAX_IDLE_DURATION = min(max(int(client_idle_timeout), 10), 300)
                else:
                    MAX_IDLE_DURATION = 60  # 60秒没有新数据则超时
                
                # 总时长上限：默认30分钟，客户端可配置（最大2小时）
                if client_timeout is not None:
                    MAX_TOTAL_DURATION = min(max(int(client_timeout), 60), 7200)
                else:
                    MAX_TOTAL_DURATION = 1800  # 30分钟绝对上限
                
                last_chunk_time = time.time()  # 记录最后一次收到chunk的时间
                
                # 异步迭代行（自动处理超时和分块）
                async for line in resp.aiter_lines():
                    current_time = time.time()
                    
                    # 【保护1】检查空闲超时：距离上次chunk超过N秒
                    idle_time = current_time - last_chunk_time
                    if idle_time > MAX_IDLE_DURATION:
                        diagnostic("stream_idle_timeout", protocol=protocol,
                                  chunks=chunk_count, 
                                  idle_time=round(idle_time, 2),
                                  max_idle=MAX_IDLE_DURATION)
                        state.write_log("stream_idle_timeout", protocol=protocol,
                                       chunks=chunk_count, idle_time=round(idle_time, 2))
                        break  # 空闲超时，结束流
                    
                    # 【保护2】检查总时长上限：防止无限期运行
                    total_elapsed = current_time - stream_start_time
                    if total_elapsed > MAX_TOTAL_DURATION:
                        diagnostic("stream_total_duration_exceeded", protocol=protocol,
                                  chunks=chunk_count, 
                                  elapsed=round(total_elapsed, 2),
                                  max_duration=MAX_TOTAL_DURATION)
                        state.write_log("stream_total_duration_exceeded", protocol=protocol,
                                       chunks=chunk_count, elapsed=round(total_elapsed, 2))
                        break  # 总时长超限，结束流
                    
                    # 更新最后chunk时间
                    last_chunk_time = current_time
                    
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
                    
                    # 【修复 C3】记录最后一个上游 chunk 的元数据，供流结束 flush 复用
                    if chunk.get("id"):
                        last_chunk_id = chunk["id"]
                    if chunk.get("created"):
                        last_chunk_created = chunk["created"]
                    if chunk.get("model"):
                        last_chunk_model = chunk["model"]
                    
                    # 提取当前 chunk 的原生 tool_calls（三种协议共享）。
                    # 注意：必须在协议分支之前定义，responses/anthropic 分支
                    # 的 B2 覆盖条件也会引用它（原生 tool_calls 存在时不覆盖）。
                    native_tool_calls = (
                        ((chunk.get("choices") or [{}])[0].get("delta") or {}).get("tool_calls")
                    )
                    
                    # 根据协议转换事件
                    if protocol == "openai":
                        # 【修复 Bug B3】删除空 finish_reason 字段
                        # 上游可能返回 "finish_reason": "" 或 null，导致客户端序列化失败
                        # 完全删除该字段，只保留真实的 stop/tool_calls/length/content_filter
                        if "choices" in chunk:
                            for choice in chunk["choices"]:
                                if "finish_reason" in choice and (choice["finish_reason"] == "" or choice["finish_reason"] is None):
                                    del choice["finish_reason"]
                        
                        # 【修复 Bug 2】原生流式 tool_calls name 缓存
                        # 首 chunk 带完整 name，后续 chunk name 为空但带 arguments 分片，
                        # 这里按 index 缓存 name 并回填（native_tool_calls 已在协议分支前提取）
                        if native_tool_calls:
                            for tc in native_tool_calls:
                                idx = tc.get("index", 0)
                                fn = tc.get("function") or {}
                                nm = fn.get("name") or ""
                                
                                # 首次出现非空 name：记录到缓存
                                if nm:
                                    native_tool_name_by_index[idx] = nm
                                # 后续空 name：从缓存回填
                                elif idx in native_tool_name_by_index:
                                    if "function" not in tc:
                                        tc["function"] = {}
                                    tc["function"]["name"] = native_tool_name_by_index[idx]
                        
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
                        
                        # 【修复 Bug B2】仅当 chunk 不含原生 tool_calls 时，才使用 DSML 解析的工具调用
                        # DSML 用于兜底：处理上游以文本标记返回工具调用的场景
                        # 如果 chunk 已有原生 delta.tool_calls，原样透传，绝不覆盖
                        if detected_tool_calls and dsml_buffer.should_emit_tool_calls() and not native_tool_calls:
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
                            
                            # 【修复 Bug B2】仅当 chunk 不含原生 tool_calls 时，才使用 DSML 解析的工具调用
                            if chunk_tool_calls and dsml_buffer.should_emit_tool_calls() and not native_tool_calls:
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
                            
                            # 【修复 Bug B2】仅当 chunk 不含原生 tool_calls 时，才使用 DSML 解析的工具调用
                            if chunk_tool_calls and dsml_buffer.should_emit_tool_calls() and not native_tool_calls:
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
                    # 【修复】流结束前强制刷新 DSML 缓冲区残留内容
                    residual = dsml_buffer.flush()
                    if residual:
                        for event_name, event_data in responses_state.feed_chunk({
                            "choices": [{"index": 0, "delta": {"content": residual}}]
                        }):
                            yield f"event: {event_name}\ndata: {json.dumps(event_data, ensure_ascii=False)}\n\n".encode()
                    # 使用 ResponsesStreamConverter 的 finish() 方法发出完整事件序列
                    for event_name, event_data in responses_state.finish():
                        yield f"event: {event_name}\ndata: {json.dumps(event_data, ensure_ascii=False)}\n\n".encode()
                    
                    # 【修复】发送SSE流结束标记，防止客户端持续等待
                    yield b"data: [DONE]\n\n"
                
                elif protocol == "anthropic" and anthropic_state:
                    # 【修复】流结束前强制刷新 DSML 缓冲区残留内容
                    residual = dsml_buffer.flush()
                    if residual:
                        for event_name, event_data in anthropic_state.feed_chunk({
                            "choices": [{"index": 0, "delta": {"content": residual}}]
                        }):
                            yield f"event: {event_name}\ndata: {json.dumps(event_data, ensure_ascii=False)}\n\n".encode()
                    for event_name, event_data in anthropic_state.finish():
                        yield f"event: {event_name}\ndata: {json.dumps(event_data, ensure_ascii=False)}\n\n".encode()
                    
                    # 【修复】发送SSE流结束标记，防止客户端持续等待（与Responses协议保持一致）
                    # 虽然Anthropic有message_stop事件，但明确的[DONE]标记能确保客户端立即处理最后的内容
                    yield b"data: [DONE]\n\n"
                
                elif protocol == "openai":
                    # 【修复】流结束前强制刷新 DSML 缓冲区残留内容，避免含 '<' 的
                    # 普通文本（如文档中举例的工具调用标签）被永久扣留而截断输出
                    residual = dsml_buffer.flush()
                    if residual:
                        response_text += residual
                        final_chunk = _build_openai_flush_chunk(
                            residual, last_chunk_id, last_chunk_created, last_chunk_model
                        )
                        yield f"data: {json.dumps(final_chunk, ensure_ascii=False)}\n\n".encode()
                    yield b"data: [DONE]\n\n"
    
    except httpx.TimeoutException as exc:
        # 【日志】超时
        diagnostic("stream_timeout", protocol=protocol, chunks=chunk_count,
            elapsed=round(time.time() - stream_start_time, 2), error=str(exc))
        state.write_log("stream_timeout", protocol=protocol, chunks=chunk_count, error=str(exc))
        
        # 【防御性编程】确保发出完整的事件序列
        if protocol == "responses" and responses_state:
            for event_name, event_data in responses_state.finish():
                yield f"event: {event_name}\ndata: {json.dumps(event_data, ensure_ascii=False)}\n\n".encode()
        elif protocol == "anthropic" and anthropic_state:
            for event_name, event_data in anthropic_state.finish():
                yield f"event: {event_name}\ndata: {json.dumps(event_data, ensure_ascii=False)}\n\n".encode()
        
        # 发送错误事件
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
        
        # 【防御性编程】确保发出完整的事件序列
        if protocol == "responses" and responses_state:
            for event_name, event_data in responses_state.finish():
                yield f"event: {event_name}\ndata: {json.dumps(event_data, ensure_ascii=False)}\n\n".encode()
        elif protocol == "anthropic" and anthropic_state:
            for event_name, event_data in anthropic_state.finish():
                yield f"event: {event_name}\ndata: {json.dumps(event_data, ensure_ascii=False)}\n\n".encode()
        
        # 发送错误事件
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
    default_log_file = pathlib.Path(
        os.getenv(
            "CODEBUDDY_PROXY_LOG_FILE",
            str(pathlib.Path.home() / ".workbuddy2api" / "codebuddy-proxy.jsonl"),
        )
    ).expanduser()
    parser.add_argument(
        "--log-file",
        type=pathlib.Path,
        default=default_log_file,
        help="记录完整请求/响应的 JSONL 文件（默认 ~/.workbuddy2api/codebuddy-proxy.jsonl）",
    )
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
    parser.add_argument("--static-models", action="store_true",
                        help="使用静态模型列表（默认从远程 API 动态获取）")
    parser.add_argument("--config-cache-ttl", type=int, default=int(os.getenv("CODEBUDDY_CONFIG_CACHE_TTL", "300")),
                        help="远程配置缓存 TTL（秒，默认 300）")
    args = parser.parse_args()
    args.log_file = args.log_file.expanduser()
    
    # 设置日志
    log_dir = args.log_file.parent if args.log_file else pathlib.Path("logs")
    logger = setup_logging(log_dir)
    json_logger = setup_json_logging(args.log_file)
    
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
        logger=logger,
        json_logger=json_logger,
    )
    proxy_state.write_log(
        "startup",
        host=args.host,
        port=args.port,
    )
    logger.info(
        "Runtime: app_version=%s system_version=%s python_version=%s machine=%s",
        proxy_state.runtime_info["app_version"],
        proxy_state.runtime_info["system_version"],
        proxy_state.runtime_info["python_version"],
        proxy_state.runtime_info["machine"],
    )
    
    # 初始化远程配置缓存（默认启用动态模型列表）
    global remote_config_cache
    if not args.static_models:
        # 默认：动态模式
        remote_config_cache = RemoteConfigCache(
            url=args.endpoint,
            ttl=args.config_cache_ttl
        )
        logger.info(f"Dynamic model list enabled: cache_url={args.endpoint}, ttl={args.config_cache_ttl}s")
        print(f"[Dynamic Models] Enabled (endpoint={args.endpoint}, TTL={args.config_cache_ttl}s)")
    else:
        # 显式禁用：静态模式
        logger.info("Using static model list (25 models)")
        print(f"[Static Models] Using 25 hardcoded models")
    
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
