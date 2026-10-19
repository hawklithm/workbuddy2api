"""INTL 会话标识头族的回归测试（issue #11 / code 11128）。

背景：2026-10-16 起 www.codebuddy.ai（国际站）对 chat/completions 强制校验
X-Conversation-* 头族，缺失即 400 / code 11128。国内站 copilot.tencent.com 无此闸门。

本测试锁定三件事：
  1. conversation_headers() 产出完整头族，且 X-Conversation-ID 在同一会话内稳定
  2. Message-ID / Request-ID 每请求新生成（不复用）
  3. 注入条件严格限定 INTL —— 国内站 URL 不得带这些头
"""

import json

import codebuddy_proxy.__main__ as proxy
from codebuddy_proxy.codebuddy_client_demo import CodeBuddyClient

REQUIRED_HEADERS = (
    "X-Conversation-ID",
    "X-Conversation-Message-ID",
    "X-Conversation-Request-ID",
)


def _client() -> CodeBuddyClient:
    """构造一个不触网、不读凭据的 client（只看 header 构造逻辑）。"""
    return CodeBuddyClient.__new__(CodeBuddyClient)


def _body(system_text: str, user_text: str = "hi") -> dict:
    return {
        "model": "deepseek-v4.1-flash",
        "messages": [
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_text},
        ],
    }


def test_conversation_headers_present_and_shaped():
    """头族三个键齐全，且 ID 形态正确（32 位 hex / UUID）。"""
    headers = _client().conversation_headers(_body("You are a helpful assistant."))

    for key in REQUIRED_HEADERS:
        assert key in headers, f"缺少 {key}"

    cid = headers["X-Conversation-ID"]
    assert isinstance(cid, str) and len(cid) == 32
    assert all(c in "0123456789abcdef" for c in cid), "X-Conversation-ID 应为 hex"

    for key in ("X-Conversation-Message-ID", "X-Conversation-Request-ID"):
        assert len(headers[key]) >= 32, f"{key} 形态异常"


def test_conversation_id_stable_within_session():
    """同一会话（同首条 system）跨轮必须复用同一个 X-Conversation-ID。

    上游按它做 prompt 前缀缓存分桶，若每轮都变会击穿缓存。
    """
    client = _client()
    body = _body("You are a helpful assistant.")

    first = client.conversation_headers(body)
    second = client.conversation_headers(body)

    assert first["X-Conversation-ID"] == second["X-Conversation-ID"], \
        "同一会话的 X-Conversation-ID 必须稳定"


def test_message_and_request_ids_fresh_per_request():
    """Message-ID / Request-ID 每请求必须新生成。"""
    client = _client()
    body = _body("You are a helpful assistant.")

    first = client.conversation_headers(body)
    second = client.conversation_headers(body)

    assert first["X-Conversation-Message-ID"] != second["X-Conversation-Message-ID"]
    assert first["X-Conversation-Request-ID"] != second["X-Conversation-Request-ID"]


def test_conversation_id_differs_across_sessions():
    """不同会话（首条 system 不同）应得到不同的 X-Conversation-ID。"""
    client = _client()

    a = client.conversation_headers(_body("You are a helpful assistant."))
    b = client.conversation_headers(_body("You are a coding assistant in Codex CLI."))

    assert a["X-Conversation-ID"] != b["X-Conversation-ID"]


def test_conversation_id_stable_when_only_user_turn_changes():
    """多轮对话：只有 user 轮变化时，X-Conversation-ID 仍应稳定（首条 system 未变）。"""
    client = _client()

    turn1 = client.conversation_headers(_body("You are a helpful assistant.", "first"))
    turn2 = client.conversation_headers(_body("You are a helpful assistant.", "second"))

    assert turn1["X-Conversation-ID"] == turn2["X-Conversation-ID"], \
        "首条 system 不变时，跨轮 ID 必须稳定"


def test_headers_tolerate_missing_or_malformed_body():
    """空 body / 无 messages / 首条非 system 时不得抛异常，且仍产出头族。"""
    client = _client()

    for body in (None, {}, {"messages": []},
                 {"messages": [{"role": "user", "content": "hi"}]}):
        headers = client.conversation_headers(body)
        for key in REQUIRED_HEADERS:
            assert key in headers, f"body={body!r} 时缺少 {key}"


def test_intl_gate_is_scoped_to_codebuddy_ai():
    """注入条件必须严格限定 INTL —— 国内站 URL 不得命中。

    这是作者在 issue #11 里明确要求的「避免影响国内站现有逻辑」。
    以源码里的判定表达式为准，防止后续重构把范围放宽。
    """
    import inspect

    source = inspect.getsource(proxy)
    # 找到 forward_chat 里注入 conversation headers 的那处守卫
    marker = "conversation_headers"
    assert marker in source, "forward_chat 未注入 conversation headers"

    idx = source.index(marker)
    window = source[max(0, idx - 600):idx]
    # 只看可执行代码，剔除注释（注释里会提到国内站做说明）
    code = "\n".join(
        line for line in window.splitlines()
        if line.strip() and not line.strip().startswith("#")
    )
    assert "codebuddy.ai" in code, \
        "注入守卫必须包含 codebuddy.ai 判定（仅 INTL）"
    assert "copilot.tencent.com" not in code, \
        "注入守卫不得覆盖国内站"
