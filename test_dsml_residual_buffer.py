"""回归测试：DSML 流式缓冲器的残留 buffer 不再吞掉含 '<' 的普通文本。

复现背景：用户用 grok 让代理梳理项目架构，代理收到的完整回答约 4996 字符，
但代理只转发了前 3088 字符（止于「支持三种格式：DeepSeek DSML（」），
其后 1908 字符被 ToolCallStreamBuffer 永久扣留并在流结束时丢失。
根因：原 add_chunk 逻辑「只要 buffer 中存在 '<' 就整段扣留、且流结束从不 flush」，
而架构文档里举例写了 <｜｜DSML｜｜>、<tool_call><invoke> 这类标签示例。

修复后：只扣留最后一个可能是工具调用开头的 '<' 后缀，先吐出前缀；
流结束时调用 flush() 强制吐出残留内容，保证零丢失。
"""

import os
import json
import pytest

from codebuddy_proxy.dsml_parser import ToolCallStreamBuffer


def stream_through(text: str, chunk_size: int = 16):
    """模拟把 text 切成小块逐块喂入缓冲器，并在流结束时 flush。"""
    buf = ToolCallStreamBuffer()
    out = []
    for i in range(0, len(text), chunk_size):
        cleaned, _calls = buf.add_chunk(text[i:i + chunk_size])
        if cleaned:
            out.append(cleaned)
    residual = buf.flush()
    if residual:
        out.append(residual)
    return "".join(out)


# 典型的「文档里举例工具调用标签」场景，后半段包含示例标签与后续正文
DOC_WITH_TAG_EXAMPLES = (
    "前文内容……整体架构分层如下。支持三种格式：DeepSeek DSML（"
    "<｜｜DSML｜｜>）、Claude 风格（<tool_call><invoke>）示例说明。"
    "第三节 脱敏模块：desensitize.py 对敏感词插入零宽空格以缓解审核误拦。"
    "第四节 上下文压缩：responses_projection.py 做最小语义闭包省 token。"
    "第五节 日志系统：proxy 记录每次请求的 metadata 与耗时。"
    "需要我针对某个具体模块深入分析，或指出代码中存在的问题清单吗？"
)


def test_tag_examples_no_loss():
    """含工具调用标签示例的文档分块后必须零丢失，且后续正文出现在输出中。"""
    result = stream_through(DOC_WITH_TAG_EXAMPLES, chunk_size=20)
    assert result == DOC_WITH_TAG_EXAMPLES, "含示例标签的文档不应被截断"
    assert "第五节 日志系统" in result, "示例标签之后的正文必须出现"
    assert "需要我针对某个具体模块深入分析" in result, "收尾问句必须出现"
    assert "DeepSeek DSML（" in result


def test_literal_lt_not_hoarded():
    """普通文本里的 '<'（如 '<50%'、'a < b'）不应被整段扣留。"""
    text = "当前进度 <50%，且 a < b 时触发分支，流程结束。"
    assert stream_through(text, chunk_size=8) == text


def test_real_tool_call_still_detected():
    """真正的工具调用仍应被识别，而非作为文本原样吐出。"""
    text = (
        "先看下目录结构：<tool_calls>"
        "<invoke name=\"bash\"><parameter name=\"cmd\">ls -la</parameter></invoke>"
        "</tool_calls>已列出。"
    )
    buf = ToolCallStreamBuffer()
    detected = []
    emitted = []
    for i in range(0, len(text), 12):
        cleaned, calls = buf.add_chunk(text[i:i + 12])
        if cleaned:
            emitted.append(cleaned)
        if calls:
            detected.extend(calls)
    residual = buf.flush()
    if residual:
        emitted.append(residual)

    assert detected, "真正的 <tool_calls> 应被解析为工具调用"
    joined = "".join(emitted)
    assert "ls -la" not in joined, "工具调用参数不应以纯文本泄漏"
    assert "先看下目录结构：" in joined
    assert "已列出。" in joined


def test_incomplete_tool_call_flushed_as_text():
    """未闭合（被截断）的工具调用片段在流结束时应作为文本 flush，而非丢失。"""
    text = "开始处理<tool_calls><invoke name=\"bash\"><parameter name=\"cmd\">"
    result = stream_through(text, chunk_size=10)
    assert result == text, "被截断的工具调用片段也应原样保留，不能丢"


def test_real_log_replay_no_loss():
    """最强验证：用用户真实日志里的上游 chunk 复现，确认完整回答不再被截断。

    日志路径来自用户提供的 C:\\Users\\marcus\\.workbuddy2api\\codebuddy-proxy.jsonl；
    若本机不存在该日志则跳过（不视为失败）。
    """
    log_path = "C:/Users/marcus/.workbuddy2api/codebuddy-proxy.jsonl"
    if not os.path.exists(log_path):
        pytest.skip(f"真实日志不存在，跳过复现：{log_path}")

    # 抽取最终一轮（17:08:46）上游 SSE 的所有 delta.content
    events = []
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    evt = next((e for e in events
                if e.get("event") == "upstream_response"
                and e.get("timestamp") == "2026-08-19T17:08:46+0800"), None)
    assert evt is not None, "未找到目标请求的上游响应日志"

    chunks = []
    for raw in evt["body_text"].splitlines():
        raw = raw.strip()
        if not raw.startswith("data:"):
            continue
        payload = raw[5:].strip()
        if payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        delta = ((obj.get("choices") or [{}])[0].get("delta") or {})
        c = delta.get("content")
        if c:
            chunks.append(c)

    full = "".join(chunks)
    result = stream_through("".join(chunks), chunk_size=1)
    assert result == full, (
        f"真实日志复现失败：代理应完整转发 {len(full)} 字符，"
        f"实际仅 {len(result)} 字符（丢失 {len(full) - len(result)}）"
    )
    assert result.endswith("需要我针对某个具体模块深入分析，或指出代码中存在的问题清单吗？")
