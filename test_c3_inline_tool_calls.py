"""回归测试：C3 —— 内联代码 `<tool_calls>` 被误判为工具调用导致输出中断。

根因（两层）：
1. DSML 流式缓冲器把内联代码 `` `<tool_calls>` `` 误当工具调用开标签（流式分块下
   反引号与标签跨 chunk），导致 `<tool_calls>` 之后的全部内容被扣留到流末尾。
2. openai 分支流结束 flush 时手搓的 final_chunk 缺顶层 id/object/created/model，
   违反 OpenAI ChatCompletionChunk schema，导致 grok 报 `missing field id`。

修复：
- find_tool_markup_tag_outside_ignored：未闭合的单/双反引号（且不在 fence 内）视为
  待闭合 code span，不再把其后的 `<tool_calls>` 当标签。
- add_chunk 尾部：扣留「未闭合 code span」或「疑似工具调用开头 <」的后缀。
- _is_tool_call_start：tail 已含 '>'（标签已闭合）时不再视为工具调用开头。
- __main__._build_openai_flush_chunk：补全 flush chunk 的顶层字段。
"""

import json

import pytest

from codebuddy_proxy.dsml_parser import ToolCallStreamBuffer
from codebuddy_proxy.__main__ import _build_openai_flush_chunk


def stream_through(text: str, chunk_size: int = 1):
    """逐 chunk 喂入缓冲器，返回 (add_chunk 累计吐出, flush 残留, detected_calls)。"""
    buf = ToolCallStreamBuffer()
    out = []
    detected = []
    for i in range(0, len(text), chunk_size):
        cleaned, calls = buf.add_chunk(text[i:i + chunk_size])
        if cleaned:
            out.append(cleaned)
        if calls:
            detected.extend(calls)
    residual = buf.flush()
    return "".join(out), residual, detected


def test_inline_tool_calls_tag_flows_inline():
    """内联代码 `<tool_calls>` 不应被扣留到流末尾，而应按序流出。"""
    text = "前文：自动修复缺失 `<tool_calls>` 包装器，把文本标记转成标准 `tool_calls`。"
    emitted, residual, detected = stream_through(text, chunk_size=1)
    assert detected == [], "内联代码里的 `<tool_calls>` 不应被识别为工具调用"
    assert residual == "", "内联代码不应被扣留到 flush"
    assert emitted == text, "内容应按序无损流出"


def test_complete_inline_code_span_not_hoarded():
    """完整的 `<tool_calls>` 内联代码应原样保留、不产生 tool_call。"""
    text = "使用 `<tool_calls>` 标记包裹工具调用。"
    emitted, residual, detected = stream_through(text, chunk_size=3)
    assert detected == []
    assert emitted + residual == text


def test_real_tool_call_still_detected():
    """真正的工具调用仍应被解析（不被修复破坏）。"""
    text = ('先看目录：<tool_calls><invoke name="bash">'
            '<parameter name="cmd">ls -la</parameter></invoke></tool_calls>完成。')
    buf = ToolCallStreamBuffer()
    detected = []
    emitted = []
    for i in range(0, len(text), 8):
        cleaned, calls = buf.add_chunk(text[i:i + 8])
        if cleaned:
            emitted.append(cleaned)
        if calls:
            detected.extend(calls)
    residual = buf.flush()
    if residual:
        emitted.append(residual)
    assert detected, "真正的 <tool_calls> 应被解析为工具调用"
    assert "ls -la" not in "".join(emitted), "工具调用参数不应泄漏为文本"


def test_truncated_tool_call_flushed_as_text():
    """被截断（未闭合）的工具调用片段仍应在 flush 时作为文本保留，不丢。"""
    text = '开始<tool_calls><invoke name="bash"><parameter name="cmd">'
    emitted, residual, detected = stream_through(text, chunk_size=10)
    assert detected == []
    assert emitted + residual == text, "被截断的片段不能丢"


def test_fence_content_still_ignored():
    """markdown 三反引号 fence 内的工具调用仍应被忽略（回归保护）。"""
    text = ('执行：\n\n<invoke name="bash"><command>ls</command></invoke>\n\n'
            '```xml\n<invoke name="bash"><command>rm -rf /</command></invoke>\n```\n\n'
            '再执行：<invoke name="bash"><command>pwd</command></invoke>')
    # 用非流式解析器验证 fence 忽略（与 parse_tool_calls 一致）
    from codebuddy_proxy.dsml_parser import parse_tool_calls
    result = parse_tool_calls(text)
    commands = [json.loads(c["function"]["arguments"]).get("command") for c in result]
    assert "ls" in commands and "pwd" in commands
    assert "rm -rf /" not in commands, "fence 内的工具调用应被忽略"


def test_flush_chunk_has_required_fields():
    """flush 时构造的 OpenAI chunk 必须含 id/object/created/model。"""
    chunk = _build_openai_flush_chunk("残 留 文 本", "cmb-xxx", 1787190176, "deepseek-v4-flash")
    for field in ("id", "object", "created", "model"):
        assert field in chunk, f"flush chunk 缺顶层字段 {field}"
    assert chunk["object"] == "chat.completion.chunk"
    assert chunk["id"] == "cmb-xxx"
    assert chunk["choices"][0]["delta"]["content"] == "残 留 文 本"
