"""
anthropic_adapter.py — Anthropic Messages API ↔ Chat Completions API 适配层。

Claude Code / CC Switch 使用 Anthropic Messages API，而 CodeBuddy 后端只支持
Chat Completions 协议。本模块做双向转换：
    请求：Anthropic system/messages/tools → Chat messages/tools
    响应：Chat SSE delta → Anthropic SSE 事件流（message_start / content_block_delta / …）
"""

from __future__ import annotations

import json
import os
import time
from typing import Any


def _rand_id(prefix: str = "msg_") -> str:
    """生成随机ID"""
    return prefix + os.urandom(12).hex()


def _now_s() -> int:
    """当前时间戳（秒）"""
    return int(time.time())


def _extract_text(value: Any) -> str:
    """从content中提取文本"""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts) if parts else ""
    return "" if value is None else str(value)


def _convert_anthropic_message(msg: dict) -> list[dict]:
    """将单条Anthropic消息转换为Chat消息（可能返回多条）
    
    Anthropic的content可能包含：
        - 字符串（纯文本）
        - [{"type": "text", "text": "..."}, {"type": "tool_result", ...}]
    
    tool_result块需要分离成独立的tool消息
    """
    role = msg.get("role", "user")
    content = msg.get("content", "")
    
    # 简单字符串content
    if isinstance(content, str):
        if not content:
            return []
        return [{"role": role, "content": content}]
    
    # 空content
    if not content:
        return []
    
    # 复杂content blocks
    if not isinstance(content, list):
        content = [content]
    
    messages = []
    
    # assistant消息处理
    if role == "assistant":
        text_parts = []
        tool_calls = []
        
        for block in content:
            if not isinstance(block, dict):
                continue
            
            block_type = block.get("type")
            
            if block_type == "text":
                text_parts.append(block.get("text", ""))
            
            elif block_type == "tool_use":
                tool_calls.append({
                    "id": block.get("id", _rand_id("call_")),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input", {})),
                    },
                })
        
        # 构造assistant消息
        assistant_msg: dict[str, Any] = {
            "role": "assistant",
            "content": "\n".join(text_parts) if text_parts else None,
        }
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        messages.append(assistant_msg)
    
    # user消息处理
    elif role == "user":
        text_parts = []
        tool_results = []
        
        for block in content:
            if not isinstance(block, dict):
                continue
            
            block_type = block.get("type")
            
            if block_type == "text":
                text_parts.append(block.get("text", ""))
            
            elif block_type == "tool_result":
                # tool_result块 → 独立的tool消息
                result_content = block.get("content", "")
                if isinstance(result_content, list):
                    # 提取text块
                    result_text = []
                    for part in result_content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            result_text.append(part.get("text", ""))
                    result_content = "\n".join(result_text)
                
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id", ""),
                    "content": str(result_content),
                })
        
        # 添加user文本消息
        if text_parts:
            messages.append({"role": "user", "content": "\n".join(text_parts)})
        
        # 添加tool结果消息
        messages.extend(tool_results)
    
    # 其他角色（system等）
    else:
        text = _extract_text(content)
        if text:
            messages.append({"role": role, "content": text})
    
    return messages


def anthropic_request_to_chat(body: dict) -> dict:
    """将Anthropic Messages API请求体转换为Chat Completions请求体
    
    关键映射：
        system → system message（置顶）
        messages → messages（展开content blocks）
        tools → Chat格式tools
        tool_choice → Chat格式
    """
    messages: list[dict] = []
    
    # system参数 → system message
    system = body.get("system")
    if system:
        system_text = _extract_text(system)
        if system_text:
            messages.append({"role": "system", "content": system_text})
    
    # messages转换
    for msg in body.get("messages", []):
        messages.extend(_convert_anthropic_message(msg))
    
    # 构造Chat body
    chat: dict[str, Any] = {
        "messages": messages,
        "stream": True,  # Anthropic默认也是流式
    }
    
    # model
    if "model" in body:
        chat["model"] = body["model"]
    
    # tools转换
    tools = body.get("tools")
    if tools:
        chat["tools"] = _convert_tools_for_chat(tools)
    
    # tool_choice转换
    tool_choice = body.get("tool_choice")
    if tool_choice is not None:
        chat["tool_choice"] = _convert_tool_choice(tool_choice)
    
    # 透传参数
    for key in ("max_tokens", "temperature", "top_p", "stop", "top_k"):
        if key in body:
            chat[key] = body[key]
    
    return chat


def _convert_tools_for_chat(tools: list[dict]) -> list[dict]:
    """将Anthropic工具格式转换为Chat格式
    
    Anthropic: {name, description, input_schema}
    Chat: {type: "function", function: {name, description, parameters}}
    """
    result = []
    for tool in tools:
        # 已经是Chat格式
        if tool.get("type") == "function" and tool.get("function"):
            result.append(tool)
        # Anthropic格式
        elif tool.get("name"):
            result.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {}),
                },
            })
        else:
            result.append(tool)
    return result


def _convert_tool_choice(tool_choice: Any) -> Any:
    """转换tool_choice格式
    
    Anthropic支持：
        - "auto" / "any" / "required" (字符串)
        - {"type": "tool", "name": "..."} (指定工具)
    
    Chat支持：
        - "auto" / "none" / "required" (字符串)
        - {"type": "function", "function": {"name": "..."}}
    """
    if isinstance(tool_choice, str):
        # Anthropic的"any" → Chat的"required"
        if tool_choice == "any":
            return "required"
        # "auto" / "none" 直接映射
        return tool_choice
    
    if isinstance(tool_choice, dict):
        # {"type": "tool", "name": "..."} → {"type": "function", "function": {"name": "..."}}
        if tool_choice.get("type") == "tool" and tool_choice.get("name"):
            return {
                "type": "function",
                "function": {"name": tool_choice["name"]},
            }
        # 已经是Chat格式
        return tool_choice
    
    return tool_choice


# ---------------------------------------------------------------------------
# 响应转换：Chat SSE → Anthropic SSE事件流
# ---------------------------------------------------------------------------

class AnthropicStreamConverter:
    """将Chat SSE流转换为Anthropic Messages API事件流
    
    事件序列：
        1. message_start
        2. content_block_start (text)
        3. content_block_delta (text_delta，多次)
        4. content_block_stop
        5. content_block_start (tool_use)
        6. content_block_delta (input_json_delta，多次)
        7. content_block_stop
        8. message_delta (stop_reason + usage)
        9. message_stop
    """
    
    def __init__(self, model: str):
        self.message_id = _rand_id("msg_")
        self.model = model
        
        # 状态跟踪
        self.started = False
        self.text_block_index: int | None = None  # 当前text块的index
        self.text = ""
        self.tool_blocks: dict[int, dict] = {}  # Chat index -> {Anthropic index, id, name, arguments}
        self.next_anthropic_index = 0  # Anthropic content_block的index计数器
        self.finish_reason: str | None = None
        self.usage: dict | None = None
        self.open_blocks: set[int] = set()  # 已打开但未关闭的块index
    
    def feed_chunk(self, chunk: dict) -> list[tuple[str, dict]]:
        """处理一个Chat SSE chunk，返回Anthropic事件列表"""
        events: list[tuple[str, dict]] = []
        
        # 首次chunk：发出message_start
        if not self.started:
            self.started = True
            events.append(("message_start", {
                "type": "message_start",
                "message": {
                    "id": self.message_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": self.model,
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
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
                
                # 首次文本：打开text块
                if self.text_block_index is None:
                    self.text_block_index = self.next_anthropic_index
                    self.next_anthropic_index += 1
                    self.open_blocks.add(self.text_block_index)
                    events.append(("content_block_start", {
                        "type": "content_block_start",
                        "index": self.text_block_index,
                        "content_block": {"type": "text", "text": ""},
                    }))
                
                # 发出text_delta
                events.append(("content_block_delta", {
                    "type": "content_block_delta",
                    "index": self.text_block_index,
                    "delta": {"type": "text_delta", "text": text_delta},
                }))
            
            # 工具调用
            for call in delta.get("tool_calls", []):
                chat_index = int(call.get("index", 0))
                
                # 初始化工具块
                if chat_index not in self.tool_blocks:
                    anthropic_index = self.next_anthropic_index
                    self.next_anthropic_index += 1
                    self.tool_blocks[chat_index] = {
                        "anthropic_index": anthropic_index,
                        "id": None,
                        "name": None,
                        "arguments": "",
                    }
                
                slot = self.tool_blocks[chat_index]
                anthropic_index = slot["anthropic_index"]
                
                call_id = call.get("id")
                if call_id and not slot["id"]:
                    slot["id"] = call_id
                
                fn = call.get("function", {})
                fn_name = fn.get("name")
                fn_args = fn.get("arguments")
                
                # 首次见到工具名：打开tool_use块
                if fn_name and not slot["name"]:
                    slot["name"] = fn_name
                    self.open_blocks.add(anthropic_index)
                    events.append(("content_block_start", {
                        "type": "content_block_start",
                        "index": anthropic_index,
                        "content_block": {
                            "type": "tool_use",
                            "id": slot["id"] or f"toolu_{self.message_id}_{chat_index}",
                            "name": fn_name,
                            "input": {},
                        },
                    }))
                
                # arguments增量
                if fn_args:
                    slot["arguments"] += fn_args
                    events.append(("content_block_delta", {
                        "type": "content_block_delta",
                        "index": anthropic_index,
                        "delta": {"type": "input_json_delta", "partial_json": fn_args},
                    }))
        
        return events
    
    def finish(self) -> list[tuple[str, dict]]:
        """流结束，发出stop事件"""
        events: list[tuple[str, dict]] = []
        
        # 关闭所有打开的块
        for index in sorted(self.open_blocks):
            events.append(("content_block_stop", {
                "type": "content_block_stop",
                "index": index,
            }))
        
        # 映射finish_reason
        stop_reason_map = {
            "stop": "end_turn",
            "tool_calls": "tool_use",
            "length": "max_tokens",
        }
        stop_reason = stop_reason_map.get(self.finish_reason or "stop", "end_turn")
        
        # 映射usage
        usage_delta = {"output_tokens": 0}
        if self.usage:
            # Chat使用completion_tokens，Anthropic使用output_tokens
            usage_delta["output_tokens"] = self.usage.get("completion_tokens", 0)
        
        # 发出message_delta
        events.append(("message_delta", {
            "type": "message_delta",
            "delta": {
                "stop_reason": stop_reason,
                "stop_sequence": None,
            },
            "usage": usage_delta,
        }))
        
        # 发出message_stop
        events.append(("message_stop", {"type": "message_stop"}))
        
        return events
    


# 向后兼容别名
anthropic_to_chat = anthropic_request_to_chat