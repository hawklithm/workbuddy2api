"""
responses_adapter.py — OpenAI Responses API ↔ Chat Completions API 适配层。

Codex CLI 使用 Responses API（POST /v1/responses），而 CodeBuddy 后端只支持
Chat Completions 协议。本模块做双向转换：
  请求：Responses input/instructions/tools → Chat messages/tools
  响应：Chat SSE delta → Responses 语义事件流（response.created / output_text.delta / …）
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any, Iterator


def _rand_id(prefix: str = "resp_") -> str:
    """生成随机ID"""
    return prefix + os.urandom(12).hex()


def _now_s() -> int:
    """当前时间戳（秒）"""
    return int(time.time())


def _extract_text(value: Any) -> str:
    """从各种content格式中提取纯文本"""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                if item.get("type") in ("text", "input_text"):
                    parts.append(item.get("text", ""))
            else:
                parts.append(str(item))
        return "".join(parts)
    return "" if value is None else str(value)


def _convert_tools_for_chat(tools: list[dict]) -> list[dict]:
    """将Responses工具格式转换为Chat格式,清理CodeBuddy后端不兼容的字段
    
    Responses: {type, name, description, parameters, strict}
    Chat: {type: "function", function: {name, description, parameters}}
    
    清理不兼容字段:
    - additionalProperties (CodeBuddy 后端不支持)
    - strict (OpenAI 扩展,其他后端不认识)
    """
    result = []
    for tool in tools:
        # 提取工具定义的核心字段
        if tool.get("type") == "function" and tool.get("function"):
            # 已经是Chat格式: {type: "function", function: {...}}
            func = tool["function"]
            name = func.get("name", "")
            description = func.get("description", "")
            parameters = func.get("parameters", {})
        elif tool.get("name"):
            # Responses格式: {type, name, description, parameters, strict}
            name = tool["name"]
            description = tool.get("description", "")
            parameters = tool.get("parameters", {})
        else:
            # 未知格式,原样保留
            result.append(tool)
            continue
        
        # 清理 parameters,移除 additionalProperties
        cleaned_params = dict(parameters) if isinstance(parameters, dict) else {}
        cleaned_params.pop("additionalProperties", None)
        
        # 构造干净的 Chat 格式工具定义(不包含 strict)
        result.append({
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": cleaned_params,
            }
        })
    
    return result


def _convert_input_items(items: list) -> list[dict]:
    """将Responses API的input数组转换为Chat messages
    
    处理：
      - {"role": "user/system/developer", "content": ...}
      - {"type": "message", ...}
      - {"type": "function_call", ...} → 合并到前面的assistant消息
      - {"type": "function_call_output", ...} → tool角色
    """
    messages: list[dict] = []
    # 临时缓存：合并相邻的assistant message和function_call
    pending_assistant_content: str | None = None
    pending_tool_calls: list[dict] = []

    def _flush_assistant():
        nonlocal pending_assistant_content, pending_tool_calls
        if pending_assistant_content is not None or pending_tool_calls:
            msg: dict[str, Any] = {
                "role": "assistant",
                "content": pending_assistant_content or "",
            }
            if pending_tool_calls:
                msg["tool_calls"] = pending_tool_calls[:]
            messages.append(msg)
            pending_assistant_content = None
            pending_tool_calls.clear()

    for item in items:
        if not isinstance(item, dict):
            continue

        item_type = item.get("type")
        role = item.get("role", "")

        # 普通消息（无type标记）
        if item_type is None and role in ("user", "system", "developer"):
            _flush_assistant()
            mapped_role = "system" if role == "developer" else role
            content = _extract_text(item.get("content", ""))
            messages.append({"role": mapped_role, "content": content})
            continue

        # typed message
        if item_type == "message":
            if role == "assistant":
                _flush_assistant()
                content = _extract_text(item.get("content", ""))
                pending_assistant_content = content
            elif role in ("user", "system", "developer"):
                _flush_assistant()
                mapped_role = "system" if role == "developer" else role
                content = _extract_text(item.get("content", ""))
                messages.append({"role": mapped_role, "content": content})
            continue

        # input_text - Responses API 的简化输入格式
        if item_type == "input_text":
            _flush_assistant()
            content = item.get("text", "")
            messages.append({"role": "user", "content": content})
            continue

        # function_call — 合并到前面的assistant消息
        if item_type == "function_call":
            if pending_assistant_content is None:
                pending_assistant_content = ""
            pending_tool_calls.append({
                "id": item.get("call_id", item.get("id", _rand_id("call_"))),
                "type": "function",
                "function": {
                    "name": item.get("name", ""),
                    "arguments": json.dumps(item.get("arguments", {})) if isinstance(item.get("arguments"), dict) else str(item.get("arguments", "")),
                },
            })
            continue

        # function_call_output → tool消息
        if item_type == "function_call_output":
            _flush_assistant()
            messages.append({
                "role": "tool",
                "tool_call_id": item.get("call_id", ""),
                "content": _extract_text(item.get("output", "")),
            })
            continue

        # 其他未知类型，如果有role就当普通消息处理
        if role:
            _flush_assistant()
            content = _extract_text(item.get("content", ""))
            messages.append({"role": role, "content": content})

    # 刷新最后一条assistant消息
    _flush_assistant()
    return messages


def responses_request_to_chat(body: dict) -> dict:
    """将Responses API请求体转换为Chat Completions请求体
    
    关键映射：
      input → messages
      instructions → system message（置顶）
      max_output_tokens → max_tokens
      tools格式微调
    """
    messages: list[dict] = []

    # instructions → system message
    instructions = body.get("instructions")
    if instructions:
        messages.append({"role": "system", "content": instructions})

    # input → messages
    inp = body.get("input", [])
    if isinstance(inp, str):
        messages.append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        messages.extend(_convert_input_items(inp))

    # 构造Chat body
    chat: dict[str, Any] = {"messages": messages, "stream": True}

    # model
    if "model" in body:
        chat["model"] = body["model"]

    # tools
    tools = body.get("tools")
    if tools:
        chat["tools"] = _convert_tools_for_chat(tools)
    if "tool_choice" in body:
        chat["tool_choice"] = body["tool_choice"]

    # 透传常见参数
    for key in ("temperature", "top_p", "stop", "seed",
                "presence_penalty", "frequency_penalty",
                "response_format", "reasoning_effort"):
        if key in body:
            chat[key] = body[key]

    # max_output_tokens → max_tokens
    if "max_output_tokens" in body:
        chat["max_tokens"] = body["max_output_tokens"]
    elif "max_tokens" in body:
        chat["max_tokens"] = body["max_tokens"]

    return chat


# ---------------------------------------------------------------------------
# 响应转换：Chat SSE → Responses事件流
# ---------------------------------------------------------------------------

class ResponsesStreamConverter:
    """将Chat SSE流转换为Responses API事件流
    
    事件序列：
      1. response.created
      2. response.in_progress
      3. response.output_item.added (message)
      4. response.content_part.added
      5. response.output_text.delta (多次)
      6. response.output_item.added (function_call，如有)
      7. response.function_call_arguments.delta (多次)
      8. response.output_text.done
      9. response.content_part.done
      10. response.output_item.done (各项)
      11. response.completed
    """

    def __init__(self, model: str):
        self.response_id = _rand_id("resp_")
        self.model = model
        self.created_at = _now_s()
        
        # 状态跟踪
        self.started = False
        self.message_item_added = False
        self.content_part_added = False
        self.text = ""
        self.function_calls: dict[int, dict] = {}  # index -> {id, name, arguments}
        self.finish_reason: str | None = None
        self.usage: dict | None = None

    def feed_chunk(self, chunk: dict) -> list[tuple[str, dict]]:
        """处理一个Chat SSE chunk，返回Responses事件列表"""
        events: list[tuple[str, dict]] = []

        # 首次chunk：发出created和in_progress
        if not self.started:
            self.started = True
            events.append(("response.created", {
                "type": "response.created",
                "response": {
                    "id": self.response_id,
                    "object": "realtime.response",
                    "status": "in_progress",
                    "created_at": self.created_at,
                },
            }))
            events.append(("response.in_progress", {
                "type": "response.in_progress",
                "response": {
                    "id": self.response_id,
                    "object": "realtime.response",
                    "status": "in_progress",
                },
            }))

        # 提取usage
        if chunk.get("usage"):
            self.usage = chunk["usage"]

        # 处理choices
        for choice in chunk.get("choices", []):
            self.finish_reason = choice.get("finish_reason") or self.finish_reason
            delta = choice.get("delta", {})

            # 文本内容
            if delta.get("content"):
                text_delta = str(delta["content"])
                self.text += text_delta

                # 首次文本：发出output_item.added + content_part.added
                if not self.message_item_added:
                    self.message_item_added = True
                    events.append(("response.output_item.added", {
                        "type": "response.output_item.added",
                        "item": {
                            "id": self.response_id + "_msg",
                            "type": "message",
                            "role": "assistant",
                            "content": [],
                        },
                    }))
                
                if not self.content_part_added:
                    self.content_part_added = True
                    events.append(("response.content_part.added", {
                        "type": "response.content_part.added",
                        "part": {"type": "text", "text": ""},
                    }))

                # 发出文本delta
                events.append(("response.output_text.delta", {
                    "type": "response.output_text.delta",
                    "item_id": self.response_id + "_msg",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": text_delta,
                }))

            # 工具调用
            for call in delta.get("tool_calls", []):
                index = int(call.get("index", 0))
                slot = self.function_calls.setdefault(index, {
                    "id": None,
                    "name": "",
                    "arguments": "",
                })

                call_id = call.get("id")
                if call_id:
                    slot["id"] = call_id

                fn = call.get("function", {})
                fn_name = fn.get("name")
                fn_args = fn.get("arguments")

                # 首次见到这个调用：发出output_item.added
                if fn_name and not slot["name"]:
                    slot["name"] = fn_name
                    item_id = slot["id"] or f"fc_{self.response_id}_{index}"
                    slot["id"] = item_id
                    events.append(("response.output_item.added", {
                        "type": "response.output_item.added",
                        "item": {
                            "id": item_id,
                            "type": "function_call",
                            "call_id": item_id,
                            "name": fn_name,
                            "arguments": "",
                        },
                    }))

                # arguments增量
                if fn_args:
                    slot["arguments"] += fn_args
                    events.append(("response.function_call_arguments.delta", {
                        "type": "response.function_call_arguments.delta",
                        "item_id": slot["id"] or f"fc_{self.response_id}_{index}",
                        "output_index": 0,
                        "delta": fn_args,
                    }))

        return events

    def finish(self) -> list[tuple[str, dict]]:
        """流结束，发出done和completed事件"""
        events: list[tuple[str, dict]] = []

        # 关闭文本块
        if self.content_part_added:
            events.append(("response.output_text.done", {
                "type": "response.output_text.done",
                "item_id": self.response_id + "_msg",
                "output_index": 0,
                "content_index": 0,
                "text": self.text,
            }))
            events.append(("response.content_part.done", {
                "type": "response.content_part.done",
                "part": {"type": "text", "text": self.text},
            }))

        # 关闭消息item
        if self.message_item_added:
            events.append(("response.output_item.done", {
                "type": "response.output_item.done",
                "item": {
                    "id": self.response_id + "_msg",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": self.text}] if self.text else [],
                },
            }))

        # 关闭各个function_call item
        for index, call in sorted(self.function_calls.items()):
            # 1. 先发出 arguments.done 事件（参数接收完成）
            events.append(("response.function_call_arguments.done", {
                "type": "response.function_call_arguments.done",
                "item_id": call["id"],
                "call_id": call["id"],
                "arguments": call["arguments"],
            }))
            
            # 2. 再发出 output_item.done 事件（工具调用项完成）
            events.append(("response.output_item.done", {
                "type": "response.output_item.done",
                "item": {
                    "id": call["id"],
                    "type": "function_call",
                    "call_id": call["id"],
                    "name": call["name"],
                    "arguments": call["arguments"],
                },
            }))

        # 构造完整response对象
        output_items = []
        if self.message_item_added:
            output_items.append({
                "id": self.response_id + "_msg",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": self.text}] if self.text else [],
            })
        for index, call in sorted(self.function_calls.items()):
            output_items.append({
                "id": call["id"],
                "type": "function_call",
                "call_id": call["id"],
                "name": call["name"],
                "arguments": call["arguments"],
            })

        # 映射usage
        usage_out = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        }
        if self.usage:
            usage_out["input_tokens"] = self.usage.get("prompt_tokens", 0)
            usage_out["output_tokens"] = self.usage.get("completion_tokens", 0)
            usage_out["total_tokens"] = self.usage.get("total_tokens", 0)

        # 发出completed
        events.append(("response.completed", {
            "type": "response.completed",
            "response": {
                "id": self.response_id,
                "object": "realtime.response",
                "status": "completed",
                "created_at": self.created_at,
                "output": output_items,
                "usage": usage_out,
                "parallel_tool_calls": True,
            },
        }))

        return events

