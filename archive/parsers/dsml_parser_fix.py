"""
DSML 解析器 - 修复版本
支持所有 3 种格式：
1. DSML 标准格式
2. 简化格式（Claude 风格）
3. 混合格式（Claude + invoke 属性）
"""
import re
import json
import uuid
from typing import Dict, List, Any, Optional


# ============================================================================
# 标记定义
# ============================================================================

DSML_MARKER = "｜｜DSML｜｜"
DSML_TOOL_CALLS_START = f"<{DSML_MARKER}tool_calls>"
DSML_TOOL_CALLS_END = f"</{DSML_MARKER}tool_calls>"

TOOL_CALL_START = "<tool_call>"
TOOL_CALL_END = "</tool_call>"


# ============================================================================
# DSML 格式解析（保持原样）
# ============================================================================

def contains_dsml(text: str) -> bool:
    """检查文本中是否包含 DSML 标记"""
    return DSML_MARKER in text


def parse_dsml_to_tool_calls(text: str) -> List[Dict[str, Any]]:
    """解析 DSML 格式的工具调用"""
    pattern = rf'<{re.escape(DSML_MARKER)}tool_calls>(.*?)</{re.escape(DSML_MARKER)}tool_calls>'
    blocks = re.findall(pattern, text, re.DOTALL)
    
    tool_calls = []
    for block in blocks:
        invoke_pattern = rf'<{re.escape(DSML_MARKER)}invoke name="([^"]+)"[^>]*>(.*?)</{re.escape(DSML_MARKER)}invoke>'
        for invoke_match in re.finditer(invoke_pattern, block, re.DOTALL):
            tool_name = invoke_match.group(1)
            invoke_body = invoke_match.group(2)
            
            # 解析参数
            param_pattern = rf'<{re.escape(DSML_MARKER)}parameter name="([^"]+)"[^>]*>(.*?)</{re.escape(DSML_MARKER)}parameter>'
            params = {}
            for param_match in re.finditer(param_pattern, invoke_body, re.DOTALL):
                param_name = param_match.group(1)
                param_value = param_match.group(2).strip()
                params[param_name] = param_value
            
            tool_calls.append({
                "id": f"call_{uuid.uuid4().hex[:24]}",
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": json.dumps(params, ensure_ascii=False)
                }
            })
    
    return tool_calls


def remove_dsml_from_content(text: str) -> str:
    """从 content 中移除 DSML 标记和内容"""
    pattern = rf'<{re.escape(DSML_MARKER)}tool_calls>.*?</{re.escape(DSML_MARKER)}tool_calls>'
    cleaned = re.sub(pattern, '', text, flags=re.DOTALL)
    return cleaned.strip()


# ============================================================================
# 简化格式解析（增强版 - 支持所有 3 种变体）
# ============================================================================

def parse_simple_tool_call(call_text: str) -> Optional[Dict[str, Any]]:
    """
    解析简化格式的工具调用（支持多种变体）
    
    支持的格式：
    1. <toolName>X</toolName> + <parameters><Y>Z</Y></parameters>
    2. <tool_name>X</tool_name> + <parameters><Y>Z</Y></parameters>
    3. <invoke name="X"><Y>Z</Y></invoke>
    4. <invoke name="X"><parameter name="Y">Z</parameter></invoke>
    """
    
    # ========== 第 1 步：提取工具名 ==========
    tool_name = None
    
    # 变体 1: <toolName> / <tool_name> 标签
    tool_name_match = re.search(
        r'<(?:toolName|tool_name)>(.*?)</(?:toolName|tool_name)>',
        call_text, re.DOTALL
    )
    if tool_name_match:
        tool_name = tool_name_match.group(1).strip()
    
    # 变体 2: <invoke name="..."> 属性
    if not tool_name:
        invoke_match = re.search(r'<invoke\s+name="([^"]+)"', call_text)
        if invoke_match:
            tool_name = invoke_match.group(1)
    
    if not tool_name:
        return None
    
    # ========== 第 2 步：解析参数 ==========
    args = {}
    
    # 方法 A: <parameters> 块内的子元素
    params_match = re.search(r'<parameters>(.*?)</parameters>', call_text, re.DOTALL)
    if params_match:
        params_text = params_match.group(1)
        # 递归解析子元素（修复：不再直接作为字符串）
        for match in re.finditer(r'<(\w+)>(.*?)</\1>', params_text, re.DOTALL):
            param_name = match.group(1)
            param_value = match.group(2).strip()
            args[param_name] = param_value
    
    # 方法 B: <invoke> 内的直接子元素（不带 <parameters> 包装）
    if not args:
        invoke_body_match = re.search(
            r'<invoke[^>]*>(.*?)</invoke>',
            call_text, re.DOTALL
        )
        if invoke_body_match:
            invoke_body = invoke_body_match.group(1)
            # 解析所有直接子元素
            for match in re.finditer(r'<(\w+)>(.*?)</\1>', invoke_body, re.DOTALL):
                param_name = match.group(1)
                param_value = match.group(2).strip()
                args[param_name] = param_value
    
    # 方法 C: <parameter name="..."> 标签（DSML 风格但无 DSML 标记）
    if not args:
        for match in re.finditer(
            r'<parameter\s+name="([^"]+)"[^>]*>(.*?)</parameter>',
            call_text, re.DOTALL
        ):
            param_name = match.group(1)
            param_value = match.group(2).strip()
            args[param_name] = param_value
    
    # ========== 第 3 步：构建结果 ==========
    if not args:
        # 如果没有找到参数，尝试将整个 body 作为参数
        # （某些格式可能只有工具名）
        return None
    
    return {
        "id": f"call_{uuid.uuid4().hex[:24]}",
        "type": "function",
        "function": {
            "name": tool_name,
            "arguments": json.dumps(args, ensure_ascii=False)
        }
    }


def extract_simple_tool_call_blocks(text: str) -> List[str]:
    """提取所有 <tool_call> 块"""
    pattern = r'<tool_call>(.*?)</tool_call>'
    matches = re.findall(pattern, text, re.DOTALL)
    return matches


def remove_simple_tool_calls(text: str) -> str:
    """从 content 中移除简化格式的工具调用标记"""
    pattern = r'<tool_call>.*?</tool_call>'
    cleaned = re.sub(pattern, '', text, flags=re.DOTALL)
    return cleaned.strip()


# ============================================================================
# 统一接口
# ============================================================================

def parse_all_tool_calls(text: str) -> List[Dict[str, Any]]:
    """解析文本中所有的工具调用（DSML 和简化格式）"""
    tool_calls = []
    
    # 1. 解析 DSML 格式
    if contains_dsml(text):
        dsml_calls = parse_dsml_to_tool_calls(text)
        tool_calls.extend(dsml_calls)
    
    # 2. 解析简化格式
    simple_blocks = extract_simple_tool_call_blocks(text)
    for block in simple_blocks:
        call = parse_simple_tool_call(block)
        if call:
            tool_calls.append(call)
    
    return tool_calls


def remove_all_tool_call_markers(text: str) -> str:
    """从 content 中移除所有工具调用标记"""
    cleaned = remove_dsml_from_content(text)
    cleaned = remove_simple_tool_calls(cleaned)
    return cleaned


# ============================================================================
# 流式响应缓冲区（保持原样）
# ============================================================================

class UnifiedToolCallBuffer:
    """统一的工具调用缓冲区，支持 DSML 和简化格式的流式累积"""
    
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


# ============================================================================
# 测试代码
# ============================================================================

if __name__ == "__main__":
    print("=== DSML 解析器测试 ===\n")
    
    test_cases = [
        # 测试 1: DSML 标准格式
        {
            "name": "DSML 标准格式",
            "input": '''<｜｜DSML｜｜tool_calls>
<｜｜DSML｜｜invoke name="bash">
<｜｜DSML｜｜parameter name="cmd" string="true">pwd</｜｜DSML｜｜parameter>
</｜｜DSML｜｜invoke>
</｜｜DSML｜｜tool_calls>''',
            "expected_name": "bash",
            "expected_args": '{"cmd": "pwd"}'
        },
        
        # 测试 2: 简化格式（修复前会失败的）
        {
            "name": "简化格式（<parameters> 子元素）",
            "input": '''<tool_call>
<toolName>bash</toolName>
<parameters>
<cmd>ls -la</cmd>
</parameters>
</tool_call>''',
            "expected_name": "bash",
            "expected_args": '{"cmd": "ls -la"}'
        },
        
        # 测试 3: 混合格式（修复前完全不支持的）
        {
            "name": "混合格式（<invoke name> + 直接子元素）",
            "input": '''<tool_call>
<invoke name="exec_command">
<cmd>pwd && ls -la</cmd>
</invoke>
</tool_call>''',
            "expected_name": "exec_command",
            "expected_args": '{"cmd": "pwd && ls -la"}'
        },
        
        # 测试 4: 多参数
        {
            "name": "多参数",
            "input": '''<tool_call>
<invoke name="write_file">
<path>/tmp/test.txt</path>
<content>Hello World</content>
<mode>w</mode>
</invoke>
</tool_call>''',
            "expected_name": "write_file",
            "expected_args": '{"path": "/tmp/test.txt", "content": "Hello World", "mode": "w"}'
        },
        
        # 测试 5: <parameter name> 风格
        {
            "name": "<parameter name> 风格",
            "input": '''<tool_call>
<invoke name="bash">
<parameter name="cmd">echo "test"</parameter>
<parameter name="cwd">/tmp</parameter>
</invoke>
</tool_call>''',
            "expected_name": "bash",
            "expected_args": '{"cmd": "echo \\"test\\"", "cwd": "/tmp"}'
        }
    ]
    
    passed = 0
    failed = 0
    
    for i, test in enumerate(test_cases, 1):
        print(f"测试 {i}: {test['name']}")
        print(f"输入: {test['input'][:60]}...")
        
        result = parse_all_tool_calls(test['input'])
        
        if not result:
            print(f"  ❌ 失败: 没有解析出工具调用")
            failed += 1
            print()
            continue
        
        call = result[0]
        actual_name = call["function"]["name"]
        actual_args = call["function"]["arguments"]
        
        # 比较（忽略 JSON 格式差异）
        expected_args_obj = json.loads(test["expected_args"])
        actual_args_obj = json.loads(actual_args)
        
        if actual_name == test["expected_name"] and actual_args_obj == expected_args_obj:
            print(f"  ✅ 通过")
            print(f"     工具: {actual_name}")
            print(f"     参数: {actual_args}")
            passed += 1
        else:
            print(f"  ❌ 失败:")
            print(f"     期望工具: {test['expected_name']}, 实际: {actual_name}")
            print(f"     期望参数: {test['expected_args']}")
            print(f"     实际参数: {actual_args}")
            failed += 1
        
        print()
    
    print(f"=== 测试结果: {passed} 通过 / {failed} 失败 ===")
