"""
完整的工具调用 XML 解析器 - 从 ds2api 移植（完全修复版）

支持：
- 精确标签扫描（状态机）
- 跳过忽略区域（Markdown fence、CDATA、注释、code span）
- 自动修复缺失包装器
- 递归参数解析
- 流式缓冲
- 任意标签名支持

修复：
- ✅ 语法错误（行续字符）
- ✅ 函数体缺失（consume_dsml_prefix）
- ✅ 参数解析失败（match_tool_markup_name）
"""
import re
import json
import uuid
import html
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass


# ============================================================================
# 数据结构
# ============================================================================

@dataclass
class ToolMarkupTag:
    """工具标记标签"""
    start: int
    end: int
    name_start: int
    name_end: int
    name: str
    closing: bool
    self_closing: bool
    dsml_like: bool
    canonical: bool
    attributes: str = ""


@dataclass
class XMLElementBlock:
    """XML 元素块"""
    start: int
    end: int
    attrs: str
    body: str


@dataclass
class ParsedToolCall:
    """解析后的工具调用"""
    id: str
    type: str
    function: Dict[str, Any]


# ============================================================================
# 常量定义
# ============================================================================

DSML_MARKER = "｜｜DSML｜｜"
DSML_VARIANTS = ["｜｜DSML｜｜", "||DSML||", "|DSML|"]

TOOL_MARKUP_NAMES = [
    ("tool_calls", "tool_calls", False),
    ("tool-calls", "tool_calls", True),
    ("toolcalls", "tool_calls", True),
    ("invoke", "invoke", False),
    ("parameter", "parameter", False),
]

FENCE_MARKERS = ["```", "~~~"]
CDATA_START = "<![CDATA["
CDATA_END = "]]>"
COMMENT_START = "<!--"
COMMENT_END = "-->"


# ============================================================================
# 忽略区域检测
# ============================================================================

def skip_xml_ignored_section(text: str, i: int) -> Tuple[int, bool, bool]:
    """
    跳过 XML 忽略区域（CDATA、注释、处理指令）
    返回: (next_position, advanced, blocked)
    """
    if i >= len(text):
        return i, False, False
    
    if text[i:i+9] == CDATA_START:
        end = text.find(CDATA_END, i + 9)
        if end == -1:
            return len(text), False, True
        return end + 3, True, False
    
    if text[i:i+4] == COMMENT_START:
        end = text.find(COMMENT_END, i + 4)
        if end == -1:
            return len(text), False, True
        return end + 3, True, False
    
    if i + 1 < len(text) and text[i:i+2] == "<?":
        end = text.find("?>", i + 2)
        if end == -1:
            return len(text), False, True
        return end + 2, True, False
    
    return i, False, False


def markdown_code_span_end(text: str, start: int) -> Tuple[int, bool]:
    """检查是否是内联代码开始，如果是则找到结束位置"""
    if start >= len(text) or text[start] != '`':
        return start, False
    
    tick_count = 0
    i = start
    while i < len(text) and text[i] == '`':
        tick_count += 1
        i += 1
    
    if tick_count >= 3:
        return start, False
    
    end = i
    while end < len(text):
        if text[end] == '`':
            end_tick_count = 0
            j = end
            while j < len(text) and text[j] == '`':
                end_tick_count += 1
                j += 1
            
            if end_tick_count == tick_count:
                return j, True
            
            end = j
        else:
            end += 1
    
    return len(text), False


def is_inside_markdown_fence(text: str, pos: int) -> bool:
    """检查位置是否在 Markdown fence 块内"""
    fence_depth = 0
    current_fence = None
    i = 0
    
    while i < pos:
        if i == 0 or text[i-1] == '\n':
            for marker in FENCE_MARKERS:
                if text[i:i+len(marker)] == marker:
                    if current_fence is None:
                        current_fence = marker
                        fence_depth += 1
                        line_end = text.find('\n', i)
                        if line_end == -1:
                            i = len(text)
                        else:
                            i = line_end + 1
                        break
                    elif text[i:i+len(current_fence)] == current_fence:
                        fence_depth -= 1
                        if fence_depth == 0:
                            current_fence = None
                        i = text.find('\n', i)
                        if i == -1:
                            i = len(text)
                        else:
                            i += 1
                        break
            else:
                i += 1
        else:
            i += 1
    
    return fence_depth > 0


# ============================================================================
# 标签扫描
# ============================================================================

def normalize_fullwidth_ascii(text: str, start: int) -> Tuple[str, int]:
    """标准化全角 ASCII 字符"""
    if start >= len(text):
        return "", 0
    
    ch = text[start]
    code = ord(ch)
    
    if 0xFF01 <= code <= 0xFF5E:
        normalized = chr(code - 0xFEE0)
        return normalized, 1
    
    return ch, 1


def has_dsml_prefix_at(text: str, start: int) -> bool:
    """检查位置是否是 DSML 前缀"""
    for variant in DSML_VARIANTS:
        if text[start:start+len(variant)] == variant:
            return True
    
    if start + 8 < len(text):
        normalized = ""
        pos = start
        for _ in range(8):
            ch, length = normalize_fullwidth_ascii(text, pos)
            normalized += ch
            pos += length
        
        if normalized in DSML_VARIANTS:
            return True
    
    return False


def consume_dsml_prefix(text: str, idx: int) -> Tuple[int, bool]:
    """
    消费 DSML 前缀
    返回: (next_position, found)
    """
    for variant in DSML_VARIANTS:
        if text[idx:idx+len(variant)] == variant:
            return idx + len(variant), True
    
    if idx + 8 < len(text):
        normalized = ""
        pos = idx
        original_pos = pos
        for _ in range(8):
            ch, length = normalize_fullwidth_ascii(text, pos)
            normalized += ch
            pos += length
        
        if normalized in DSML_VARIANTS:
            return pos, True
    
    return idx, False


def match_tool_markup_name(text: str, start: int) -> Tuple[str, int, bool]:
    """
    匹配工具标记名称（✅ 修复：支持任意标签名）
    
    返回: (canonical_name, end_position, is_dsml_like)
    """
    dsml_like = False
    idx = start
    
    if has_dsml_prefix_at(text, idx):
        idx, _ = consume_dsml_prefix(text, idx)
        dsml_like = True
    
    name_start = idx
    name_end = idx
    
    while name_end < len(text):
        ch = text[name_end]
        if ch.isalnum() or ch in ('_', '-'):
            name_end += 1
        else:
            break
    
    if name_end == name_start:
        return "", start, False
    
    raw_name = text[name_start:name_end].lower()
    
    # 查找标准化名称（特殊标签）
    for raw, canonical, dsml_only in TOOL_MARKUP_NAMES:
        if raw_name == raw:
            if dsml_only and not dsml_like:
                continue
            return canonical, name_end, dsml_like
    
    # ✅ 关键修复：任意其他标签名也接受（用于参数标签如 <cmd>, <path> 等）
    return raw_name, name_end, dsml_like


def scan_tool_markup_tag_at(text: str, start: int) -> Tuple[Optional[ToolMarkupTag], bool]:
    """在指定位置扫描工具标记标签"""
    if start >= len(text) or text[start] != '<':
        return None, False
    
    idx = start + 1
    
    closing = False
    if idx < len(text) and text[idx] == '/':
        closing = True
        idx += 1
    
    while idx < len(text) and text[idx] in (' ', '\t', '\r', '\n'):
        idx += 1
    
    if idx >= len(text):
        return None, False
    
    name_start = idx
    canonical_name, name_end, dsml_like = match_tool_markup_name(text, idx)
    
    if not canonical_name:
        return None, False
    
    idx = name_end
    attr_start = idx
    self_closing = False
    
    while idx < len(text):
        ch = text[idx]
        
        if ch == '>':
            attrs = text[attr_start:idx].strip()
            return ToolMarkupTag(
                start=start,
                end=idx,
                name_start=name_start,
                name_end=name_end,
                name=canonical_name,
                closing=closing,
                self_closing=self_closing,
                dsml_like=dsml_like,
                canonical=not dsml_like,
                attributes=attrs
            ), True
        
        if ch == '/' and idx + 1 < len(text) and text[idx + 1] == '>':
            self_closing = True
            attrs = text[attr_start:idx].strip()
            return ToolMarkupTag(
                start=start,
                end=idx + 1,
                name_start=name_start,
                name_end=name_end,
                name=canonical_name,
                closing=closing,
                self_closing=self_closing,
                dsml_like=dsml_like,
                canonical=not dsml_like,
                attributes=attrs
            ), True
        
        idx += 1
    
    return None, False


def find_tool_markup_tag_outside_ignored(text: str, start: int) -> Tuple[Optional[ToolMarkupTag], bool]:
    """从指定位置开始查找下一个工具标记标签（跳过忽略区域）"""
    i = max(start, 0)
    
    while i < len(text):
        next_pos, advanced, blocked = skip_xml_ignored_section(text, i)
        if blocked:
            return None, False
        if advanced:
            i = next_pos
            continue
        
        if text[i] == '`':
            end, found = markdown_code_span_end(text, i)
            if found:
                i = end
                continue
        
        if is_inside_markdown_fence(text, i):
            line_end = text.find('\n', i)
            if line_end == -1:
                return None, False
            i = line_end + 1
            continue
        
        tag, found = scan_tool_markup_tag_at(text, i)
        if found:
            return tag, True
        
        i += 1
    
    return None, False


def find_matching_tool_markup_close(text: str, open_tag: ToolMarkupTag) -> Tuple[Optional[ToolMarkupTag], bool]:
    """查找匹配的闭标签"""
    depth = 1
    i = open_tag.end + 1
    
    while i < len(text):
        tag, found = find_tool_markup_tag_outside_ignored(text, i)
        if not found:
            break
        
        if tag.name == open_tag.name:
            if tag.closing:
                depth -= 1
                if depth == 0:
                    return tag, True
            else:
                depth += 1
        
        i = tag.end + 1
    
    return None, False


# ============================================================================
# 属性解析
# ============================================================================

def parse_xml_attributes(attrs_text: str) -> Dict[str, str]:
    """解析 XML 属性"""
    attrs = {}
    # 修复正则表达式以支持 - 和 :
    pattern = r'([a-z0-9_:-]+)\s*=\s*["\']([^"\']*)["\']'
    
    for match in re.finditer(pattern, attrs_text, re.IGNORECASE):
        key = match.group(1)
        value = match.group(2)
        attrs[key] = html.unescape(value)
    
    return attrs


# ============================================================================
# 参数解析（✅ 修复）
# ============================================================================

def parse_invoke_parameters(invoke_body: str) -> Dict[str, Any]:
    """
    递归解析 <invoke> 内的参数（✅ 修复双层 while 循环问题）
    
    支持：
    - <parameter name="cmd">pwd</parameter>
    - <cmd>pwd</cmd>
    - 嵌套结构
    """
    params = {}
    i = 0
    
    # ✅ 修复：外层循环不再有错误的条件
    while i < len(invoke_body):
        # 跳过空白
        while i < len(invoke_body) and invoke_body[i] in (' ', '\t', '\r', '\n'):
            i += 1
        
        if i >= len(invoke_body):
            break
        
        # 查找下一个开标签
        tag_start = invoke_body.find('<', i)
        if tag_start == -1:
            break
        
        # 扫描标签
        tag, found = scan_tool_markup_tag_at(invoke_body, tag_start)
        if not found or tag.closing:
            i = tag_start + 1
            continue
        
        # 查找匹配的闭标签
        close_tag, found_close = find_matching_tool_markup_close(invoke_body, tag)
        if not found_close:
            i = tag.end + 1
            continue
        
        # 提取参数名和值
        param_name = None
        
        # 方法 1: <parameter name="cmd">...</parameter>
        if tag.name == "parameter":
            attrs = parse_xml_attributes(tag.attributes)
            param_name = attrs.get("name")
        else:
            # 方法 2: <cmd>...</cmd> (✅ 现在支持任意标签名)
            param_name = tag.name
        
        if param_name:
            # 提取值
            value_start = tag.end + 1
            value_end = close_tag.start
            value_text = invoke_body[value_start:value_end].strip()
            
            # 递归解析（如果值包含子元素）
            if '<' in value_text and '>' in value_text:
                nested_params = parse_invoke_parameters(value_text)
                if nested_params:
                    params[param_name] = nested_params
                else:
                    params[param_name] = html.unescape(value_text)
            else:
                params[param_name] = html.unescape(value_text)
        
        i = close_tag.end + 1
    
    return params


# ============================================================================
# 工具调用解析
# ============================================================================

def parse_single_xml_tool_call(block: XMLElementBlock) -> Tuple[Optional[ParsedToolCall], bool]:
    """解析单个 XML 工具调用块"""
    attrs = parse_xml_attributes(block.attrs)
    tool_name = attrs.get("name")
    
    if not tool_name:
        return None, False
    
    params = parse_invoke_parameters(block.body)
    
    tool_call = ParsedToolCall(
        id=f"call_{uuid.uuid4().hex[:24]}",
        type="function",
        function={
            "name": tool_name,
            "arguments": json.dumps(params, ensure_ascii=False)
        }
    )
    
    return tool_call, True


def find_invoke_blocks(text: str) -> List[XMLElementBlock]:
    """查找所有 <invoke> 块"""
    blocks = []
    i = 0
    
    while i < len(text):
        tag, found = find_tool_markup_tag_outside_ignored(text, i)
        if not found:
            break
        
        if tag.name == "invoke" and not tag.closing:
            close_tag, found_close = find_matching_tool_markup_close(text, tag)
            if found_close:
                blocks.append(XMLElementBlock(
                    start=tag.start,
                    end=close_tag.end + 1,
                    attrs=tag.attributes,
                    body=text[tag.end + 1:close_tag.start]
                ))
                i = close_tag.end + 1
            else:
                i = tag.end + 1
        else:
            i = tag.end + 1
    
    return blocks


def parse_xml_tool_calls(text: str) -> List[ParsedToolCall]:
    """解析 XML 格式的工具调用"""
    tool_calls = []
    invoke_blocks = find_invoke_blocks(text)
    
    for block in invoke_blocks:
        call, ok = parse_single_xml_tool_call(block)
        if ok:
            tool_calls.append(call)
    
    return tool_calls


# ============================================================================
# 自动修复
# ============================================================================

def repair_missing_tool_calls_wrapper(text: str) -> str:
    """自动修复缺失的 <tool_calls> 包装器"""
    # 检查是否已经有 <tool_calls> 包装器
    i = 0
    while i < len(text):
        tag, found = find_tool_markup_tag_outside_ignored(text, i)
        if not found:
            break
        
        if tag.name == "tool_calls" and not tag.closing:
            return text
        
        i = tag.end + 1
    
    # 查找第一个 <invoke> 标签
    i = 0
    first_invoke = None
    while i < len(text):
        tag, found = find_tool_markup_tag_outside_ignored(text, i)
        if not found:
            break
        
        if tag.name == "invoke" and not tag.closing:
            first_invoke = tag
            break
        
        i = tag.end + 1
    
    if not first_invoke:
        return text
    
    # 查找最后一个 </invoke> 闭标签
    last_close_pos = -1
    i = len(text) - 1
    while i >= 0:
        if text[i:i+8] == "</invoke":
            end = text.find('>', i)
            if end != -1:
                last_close_pos = end
                break
        i -= 1
    
    if last_close_pos == -1:
        return text
    
    # 添加包装器
    prefix = text[:first_invoke.start]
    body = text[first_invoke.start:last_close_pos + 1]
    suffix = text[last_close_pos + 1:]
    
    return f"{prefix}<tool_calls>{body}</tool_calls>{suffix}"


# ============================================================================
# 统一解析接口
# ============================================================================

def parse_tool_calls(text: str, auto_repair: bool = True) -> List[Dict[str, Any]]:
    """解析工具调用（统一入口）"""
    if auto_repair:
        text = repair_missing_tool_calls_wrapper(text)
    
    parsed_calls = parse_xml_tool_calls(text)
    
    result = []
    for call in parsed_calls:
        result.append({
            "id": call.id,
            "type": call.type,
            "function": call.function
        })
    
    return result


def remove_tool_call_markup(text: str) -> str:
    """从文本中移除所有工具调用标记"""
    result = []
    i = 0
    
    while i < len(text):
        tag, found = find_tool_markup_tag_outside_ignored(text, i)
        
        if not found:
            result.append(text[i:])
            break
        
        if tag.name == "tool_calls" and not tag.closing:
            result.append(text[i:tag.start])
            
            close_tag, found_close = find_matching_tool_markup_close(text, tag)
            
            if found_close:
                i = close_tag.end + 1
            else:
                i = tag.end + 1
        else:
            result.append(text[i:tag.start])
            i = tag.end + 1
    
    return ''.join(result).strip()


# ============================================================================
# 流式缓冲器
# ============================================================================

class ToolCallStreamBuffer:
    """工具调用流式缓冲器（生产级）"""
    
    def __init__(self):
        self.buffer = ""
        self.detected_calls: List[Dict[str, Any]] = []
        self.calls_emitted = False
    
    def add_chunk(self, content: str) -> Tuple[str, Optional[List[Dict[str, Any]]]]:
        """
        添加一个 chunk 的 content
        
        返回: (应该输出的 content, tool_calls 或 None)
        """
        self.buffer += content
        
        # 尝试查找完整的 <tool_calls>...</tool_calls>
        i = 0
        while i < len(self.buffer):
            tag, found = find_tool_markup_tag_outside_ignored(self.buffer, i)
            if not found:
                break
            
            if tag.name == "tool_calls" and not tag.closing:
                close_tag, found_close = find_matching_tool_markup_close(self.buffer, tag)
                
                if found_close:
                    block = self.buffer[tag.start:close_tag.end + 1]
                    calls = parse_tool_calls(block, auto_repair=False)
                    
                    if calls:
                        self.detected_calls = calls
                        self.calls_emitted = True
                        
                        prefix = self.buffer[:tag.start]
                        suffix = self.buffer[close_tag.end + 1:]
                        
                        self.buffer = ""
                        
                        return prefix.strip(), calls
                else:
                    # 还没有完整的闭标签，继续等待
                    return "", None
            
            i = tag.end + 1
        
        # 尝试查找独立的 <invoke>...</invoke>（自动修复模式）
        i = 0
        while i < len(self.buffer):
            tag, found = find_tool_markup_tag_outside_ignored(self.buffer, i)
            if not found:
                break
            
            if tag.name == "invoke" and not tag.closing:
                close_tag, found_close = find_matching_tool_markup_close(self.buffer, tag)
                
                if found_close:
                    block = self.buffer[tag.start:close_tag.end + 1]
                    calls = parse_tool_calls(block, auto_repair=True)
                    
                    if calls:
                        self.detected_calls = calls
                        self.calls_emitted = True
                        
                        prefix = self.buffer[:tag.start]
                        suffix = self.buffer[close_tag.end + 1:]
                        
                        self.buffer = ""
                        
                        return prefix.strip(), calls
                else:
                    # 还没有完整的闭标签，继续等待
                    return "", None
            
            i = tag.end + 1
        
        # 如果 buffer 中没有任何 < 字符，可以安全输出
        if '<' in self.buffer:
            return "", None
        
        output = self.buffer
        self.buffer = ""
        return output, None
    
    def should_emit_tool_calls(self) -> bool:
        """是否应该发送 tool_calls"""
        return self.calls_emitted
    
    def get_detected_calls(self) -> List[Dict[str, Any]]:
        """获取已检测的工具调用"""
        return self.detected_calls


# ============================================================================
# 向后兼容别名
# ============================================================================

UnifiedToolCallBuffer = ToolCallStreamBuffer
DSMLStreamBuffer = ToolCallStreamBuffer

# 向后兼容函数名
parse_all_tool_calls = parse_tool_calls
remove_all_tool_call_markers = remove_tool_call_markup


# ============================================================================
# 测试代码（如果直接运行）
# ============================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("XML 解析器快速测试")
    print("=" * 60)
    
    tests = [
        ("标准格式", '<tool_calls><invoke name="bash"><parameter name="cmd">pwd</parameter></invoke></tool_calls>'),
        ("DSML 格式", '<｜｜DSML｜｜tool_calls><｜｜DSML｜｜invoke name="bash"><｜｜DSML｜｜parameter name="cmd">ls -la</｜｜DSML｜｜parameter></｜｜DSML｜｜invoke></｜｜DSML｜｜tool_calls>'),
        ("简化格式", '<tool_calls><invoke name="bash"><cmd>ls -la</cmd></invoke></tool_calls>'),
        ("混合格式", '<tool_call><invoke name="exec_command"><cmd>pwd && ls -la</cmd></invoke></tool_call>'),
    ]
    
    passed = 0
    failed = 0
    
    for name, test_input in tests:
        try:
            result = parse_tool_calls(test_input)
            if result:
                try:
                    # ✅ P1-2: JSON 解析异常处理
                    args = json.loads(result[0]['function']['arguments'])
                except (json.JSONDecodeError, KeyError, TypeError, IndexError):
                    args = {}
                if args:
                    print(f"✅ {name}: {result[0]['function']['name']} - {list(args.keys())}")
                    passed += 1
                else:
                    print(f"❌ {name}: 参数为空")
                    failed += 1
            else:
                print(f"❌ {name}: 解析失败")
                failed += 1
        except Exception as e:
            print(f"❌ {name}: 异常 - {e}")
            failed += 1
    
    print(f"\n总计: {passed} 通过 / {failed} 失败")
