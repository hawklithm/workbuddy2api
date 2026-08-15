"""
DSML (DeepSeek Markup Language) 和简化工具调用解析器

CodeBuddy 后端返回两种格式的工具调用文本：
1. DSML 格式：<｜｜DSML｜｜tool_calls>...</｜｜DSML｜｜tool_calls>
2. 简化格式：<tool_call><toolName>bash</toolName><toolName>ls</toolName></tool_call>

本模块负责检测、解析并转换成 OpenAI tool_calls 格式。
"""
import re
import json
import uuid
from typing import Dict, List, Any, Optional


# DSML 标记
DSML_MARKER = "｜｜DSML｜｜"
DSML_TOOL_CALLS_START = f"<{DSML_MARKER}tool_calls>"
DSML_TOOL_CALLS_END = f"</{DSML_MARKER}tool_calls>"

# 简化的 tool_call 标记（Claude 风格）
TOOL_CALL_START = "<tool_call>"
TOOL_CALL_END = "</tool_call>"


# ============================================================================
# DSML 格式解析
# ============================================================================

def contains_dsml(text: str) -> bool:
    """检查文本中是否包含 DSML 标记"""
    return DSML_MARKER in text


def extract_dsml_blocks(text: str) -> List[str]:
    """提取所有 DSML tool_calls 块"""
    pattern = rf'<{re.escape(DSML_MARKER)}tool_calls>(.*?)</{re.escape(DSML_MARKER)}tool_calls>'
    matches = re.findall(pattern, text, re.DOTALL)
    return matches


def parse_dsml_invoke(invoke_text: str) -> Optional[Dict[str, Any]]:
    """
    解析单个 DSML invoke 块
    
    示例输入:
    <｜｜DSML｜｜invoke name="bash">
    <｜｜DSML｜｜parameter name="command" string="true">ls -la</｜｜DSML｜｜parameter>
    </｜｜DSML｜｜invoke>
    """
    # 提取函数名
    name_match = re.search(rf'<{re.escape(DSML_MARKER)}invoke\s+name="([^"]+)">', invoke_text)
    if not name_match:
        return None
    
    function_name = name_match.group(1)
    
    # 提取所有参数
    param_pattern = rf'<{re.escape(DSML_MARKER)}parameter\s+name="([^"]+)"[^>]*>(.*?)</{re.escape(DSML_MARKER)}parameter>'
    params = {}
    
    for match in re.finditer(param_pattern, invoke_text, re.DOTALL):
        param_name = match.group(1)
        param_value = match.group(2).strip()
        params[param_name] = param_value
    
    return {
        "id": f"call_{uuid.uuid4().hex[:24]}",
        "type": "function",
        "function": {
            "name": function_name,
            "arguments": json.dumps(params, ensure_ascii=False)
        }
    }


def parse_dsml_to_tool_calls(text: str) -> List[Dict[str, Any]]:
    """解析完整的 DSML 文本，返回 tool_calls 列表"""
    tool_calls = []
    
    blocks = extract_dsml_blocks(text)
    
    for block in blocks:
        invoke_pattern = rf'<{re.escape(DSML_MARKER)}invoke\s+name="[^"]+">(.*?)</{re.escape(DSML_MARKER)}invoke>'
        invokes = re.finditer(invoke_pattern, block, re.DOTALL)
        
        for invoke_match in invokes:
            full_invoke = invoke_match.group(0)
            tool_call = parse_dsml_invoke(full_invoke)
            if tool_call:
                tool_calls.append(tool_call)
    
    return tool_calls


def remove_dsml_from_content(text: str) -> str:
    """从 content 中移除所有 DSML 标记和内容"""
    pattern = rf'<{re.escape(DSML_MARKER)}tool_calls>.*?</{re.escape(DSML_MARKER)}tool_calls>'
    cleaned = re.sub(pattern, '', text, flags=re.DOTALL)
    cleaned = re.sub(r'\n\n+', '\n\n', cleaned)
    return cleaned.strip()


# ============================================================================
# 简化格式解析（Claude 风格，支持 toolName 和 tool_name）
# ============================================================================

def extract_simple_tool_call_blocks(text: str) -> List[str]:
    """提取所有简化的 <tool_call> 块"""
    pattern = r'<tool_call>(.*?)</tool_call>'
    matches = re.findall(pattern, text, re.DOTALL)
    return matches


def parse_simple_tool_call(call_text: str) -> Optional[Dict[str, Any]]:
    """
    解析简化的 tool_call 格式（支持多种变体）
    
    支持的格式:
    1. <toolName>bash</toolName><toolName>ls</toolName>
    2. <tool_name>bash</tool_name><tool_name>ls</tool_name>
    3. <toolName>bash</toolName><command>ls</command>
    4. 混合格式
    
    规则:
    - 第一个 toolName/tool_name 是工具名
    - 第二个 toolName/tool_name（如果存在）是主要参数
    - 其他标签作为参数键值对
    """
    # 提取所有标签（支持下划线和驼峰，大小写不敏感）
    tag_pattern = r'<([\w_]+)>(.*?)</\1>'
    tags = re.findall(tag_pattern, call_text, re.DOTALL | re.IGNORECASE)
    
    if not tags:
        return None
    
    # 查找工具名（toolName 或 tool_name，大小写不敏感）
    tool_names = []
    for tag_name, tag_value in tags:
        if tag_name.lower().replace('_', '') == 'toolname':
            tool_names.append(tag_value.strip())
    
    if not tool_names:
        return None
    
    function_name = tool_names[0]
    
    # 收集参数
    params = {}
    seen_tool_name = False
    
    for tag_name, tag_value in tags:
        tag_name_normalized = tag_name.lower().replace('_', '')
        
        if tag_name_normalized == 'toolname':
            if not seen_tool_name:
                # 跳过第一个（它是函数名）
                seen_tool_name = True
                continue
            else:
                # 第二个作为主要参数
                if function_name in ["bash", "sh", "zsh", "exec_command", "shell"]:
                    params["command"] = tag_value.strip()
                else:
                    params["input"] = tag_value.strip()
        else:
            # 其他标签直接作为参数
            params[tag_name] = tag_value.strip()
    
    return {
        "id": f"call_{uuid.uuid4().hex[:24]}",
        "type": "function",
        "function": {
            "name": function_name,
            "arguments": json.dumps(params, ensure_ascii=False)
        }
    }


def remove_simple_tool_calls(text: str) -> str:
    """从 content 中移除简化格式的工具调用标记"""
    pattern = r'<tool_call>.*?</tool_call>'
    cleaned = re.sub(pattern, '', text, flags=re.DOTALL)
    cleaned = re.sub(r'\n\n+', '\n\n', cleaned)
    return cleaned.strip()


# ============================================================================
# 统一接口
# ============================================================================

def parse_all_tool_calls(text: str) -> List[Dict[str, Any]]:
    """解析文本中所有的工具调用（DSML 和简化格式）"""
    tool_calls = []
    
    # 1. 解析 DSML 格式
    tool_calls.extend(parse_dsml_to_tool_calls(text))
    
    # 2. 解析简化格式
    simple_blocks = extract_simple_tool_call_blocks(text)
    for block in simple_blocks:
        tool_call = parse_simple_tool_call(block)
        if tool_call:
            tool_calls.append(tool_call)
    
    return tool_calls


def remove_all_tool_call_markers(text: str) -> str:
    """从 content 中移除所有工具调用标记（DSML 和简化格式）"""
    # 移除 DSML 格式
    cleaned = remove_dsml_from_content(text)
    
    # 移除简化格式
    cleaned = remove_simple_tool_calls(cleaned)
    
    return cleaned


# ============================================================================
# 流式响应缓冲区
# ============================================================================

class UnifiedToolCallBuffer:
    """
    统一的工具调用缓冲区，支持 DSML 和简化格式的流式累积
    """
    def __init__(self):
        self.buffer = ""
        self.in_tool_call = False
        self.tool_calls_emitted = False
        
    def add_chunk(self, content: str) -> tuple[str, Optional[List[Dict[str, Any]]]]:
        """
        添加一个 chunk 的 content
        
        返回: (应该输出的 content, tool_calls 或 None)
        """
        self.buffer += content
        
        # 检查是否进入工具调用区域
        if not self.in_tool_call:
            if DSML_TOOL_CALLS_START in self.buffer or TOOL_CALL_START in self.buffer:
                self.in_tool_call = True
        
        # 如果没有进入工具调用模式，直接输出
        if not self.in_tool_call:
            output = self.buffer
            self.buffer = ""
            return output, None
        
        # 检查是否结束工具调用
        # 1. 检查 DSML 结束
        if DSML_TOOL_CALLS_END in self.buffer:
            tool_calls = parse_dsml_to_tool_calls(self.buffer)
            cleaned = remove_dsml_from_content(self.buffer)
            
            self.buffer = ""
            self.in_tool_call = False
            self.tool_calls_emitted = True
            
            return cleaned, tool_calls
        
        # 2. 检查简化格式结束（可能有多个）
        if TOOL_CALL_END in self.buffer:
            # 统计开始和结束标记
            start_count = self.buffer.count(TOOL_CALL_START)
            end_count = self.buffer.count(TOOL_CALL_END)
            
            # 如果配对完成
            if start_count > 0 and start_count == end_count:
                tool_calls = parse_all_tool_calls(self.buffer)
                cleaned = remove_all_tool_call_markers(self.buffer)
                
                self.buffer = ""
                self.in_tool_call = False
                self.tool_calls_emitted = True
                
                return cleaned, tool_calls
        
        # 如果在工具调用中但还没结束，不输出 content
        return "", None
    
    def should_emit_tool_calls(self) -> bool:
        """是否应该发送 tool_calls"""
        return self.tool_calls_emitted


# 向后兼容的别名
DSMLStreamBuffer = UnifiedToolCallBuffer
