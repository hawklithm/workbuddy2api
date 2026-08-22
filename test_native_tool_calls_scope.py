"""回归测试：native_tool_calls 必须在协议分支前定义。

根因：`stream_upstream` 里 `native_tool_calls` 原来只在 openai 分支内定义，
但 responses/anthropic 分支的 B2 覆盖条件 `and not native_tool_calls` 也引用它。
当走 responses/anthropic 协议且 DSML 检测到工具调用时，会触发
UnboundLocalError（未初始化的局部变量），被 stream_upstream 的 except Exception
兜底为 stream_error。

修复：把 `native_tool_calls` 的提取移到协议分支之前，三种协议共享。
"""

import asyncio
import json
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import codebuddy_proxy.__main__ as m


@pytest.fixture
def fake_state(monkeypatch):
    state = types.SimpleNamespace(
        write_log=lambda *a, **k: None,
        write_body_log=lambda *a, **k: None,
        verbose_llm=False,
        logger=None,
        json_logger=None,
    )
    monkeypatch.setattr(m, "proxy_state", state)
    return state


class _FakeResp:
    status_code = 200

    def __init__(self, lines):
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line


def _sse_lines(content_chunks):
    lines = []
    for c in content_chunks:
        obj = {
            "id": "cmb-1", "model": "deepseek-v4-flash",
            "object": "chat.completion.chunk", "created": 1,
            "choices": [{"index": 0, "delta": {"content": c}, "finish_reason": ""}],
            "usage": None,
        }
        lines.append("data: " + json.dumps(obj, ensure_ascii=False))
    lines.append("data: [DONE]")
    return lines


async def _collect(agen):
    return [x async for x in agen]


def _run_stream(protocol, content_chunks):
    resp = _FakeResp(_sse_lines(content_chunks))
    with patch.object(m.httpx, "AsyncClient") as mock_cls:
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=resp)
        ctx.__aexit__ = AsyncMock(return_value=False)
        client.stream = MagicMock(return_value=ctx)
        mock_cls.return_value = client

        return asyncio.run(_collect(m.stream_upstream(
            "http://x", {}, {"model": "deepseek-v4-flash", "stream": True},
            protocol, None,
        )))


# DSML 文本标记形式的工具调用（上游以文本返回工具的兜底场景）
DSML_TOOL_CALL = (
    '<tool_calls><invoke name="bash"><parameter name="cmd">ls</parameter></invoke></tool_calls>'
)


def test_responses_protocol_dsml_tool_call_no_unbound_local(fake_state):
    """responses 协议 + DSML 工具调用时，不应抛 UnboundLocalError。"""
    out = _run_stream("responses", [DSML_TOOL_CALL])
    assert len(out) >= 1, "应正常产出事件（而非抛异常）"


def test_anthropic_protocol_dsml_tool_call_no_unbound_local(fake_state):
    """anthropic 协议 + DSML 工具调用时，不应抛 UnboundLocalError。"""
    out = _run_stream("anthropic", [DSML_TOOL_CALL])
    assert len(out) >= 1, "应正常产出事件（而非抛异常）"


def test_openai_protocol_native_tool_calls_name_backfill(fake_state):
    """openai 协议 + 原生 tool_calls 首片带 name、后续空 name 时，应回填 name。"""
    # 首 chunk 带 name + 空 arguments；第二 chunk name 为空 + arguments 分片
    chunks = [
        {"id": "cmb-1", "model": "deepseek-v4-flash", "object": "chat.completion.chunk",
         "created": 1, "choices": [{"index": 0, "delta": {
             "tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                             "function": {"name": "bash", "arguments": ""}}]
         }, "finish_reason": ""}], "usage": None},
        {"id": "cmb-1", "model": "deepseek-v4-flash", "object": "chat.completion.chunk",
         "created": 1, "choices": [{"index": 0, "delta": {
             "tool_calls": [{"index": 0, "function": {"name": "", "arguments": "ls"}}]
         }, "finish_reason": "tool_calls"}], "usage": None},
    ]
    lines = ["data: " + json.dumps(c, ensure_ascii=False) for c in chunks] + ["data: [DONE]"]

    resp = _FakeResp(lines)
    with patch.object(m.httpx, "AsyncClient") as mock_cls:
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=resp)
        ctx.__aexit__ = AsyncMock(return_value=False)
        client.stream = MagicMock(return_value=ctx)
        mock_cls.return_value = client

        out = asyncio.run(_collect(m.stream_upstream(
            "http://x", {}, {"model": "deepseek-v4-flash", "stream": True},
            "openai", None,
        )))

    # 收集所有 openai chunk 里的 tool_calls name
    names = []
    for raw in out:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        if not text.startswith("data:"):
            continue
        payload = text[5:].strip()
        if payload == "[DONE]":
            continue
        obj = json.loads(payload)
        for tc in (obj.get("choices") or [{}])[0].get("delta", {}).get("tool_calls") or []:
            names.append((tc.get("function") or {}).get("name"))
    # 后续空 name 分片应被回填为 "bash"，而非空字符串
    assert "" not in names, "后续 chunk 的空 name 应被回填"
    assert "bash" in names
