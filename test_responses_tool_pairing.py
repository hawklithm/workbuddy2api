"""Regression tests for Responses to Chat tool-call/result pairing."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.responses import StreamingResponse
from httpx import ASGITransport, AsyncClient

import codebuddy_proxy.__main__ as proxy
from codebuddy_proxy.tool_pairing import ToolPairingError
from codebuddy_proxy.responses_adapter import (
    ResponsesStreamConverter, _convert_input_items, responses_request_to_chat,
)
from codebuddy_proxy.responses_projection import project_responses_chat_body


def _call(call_id, name="shell"):
    return {
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": '{"command": "pwd"}',
    }


def _result(call_id, output="ok"):
    return {"type": "function_call_output", "call_id": call_id, "output": output}


def _assert_well_formed_tool_groups(messages):
    """Every tool result belongs to the immediately preceding assistant call group."""
    expected = set()
    for message in messages:
        if message["role"] == "assistant":
            assert not expected
            calls = message.get("tool_calls") or []
            ids = [call["id"] for call in calls]
            assert len(ids) == len(set(ids))
            expected = set(ids)
        elif message["role"] == "tool":
            assert message["tool_call_id"] in expected
            expected.remove(message["tool_call_id"])
        else:
            assert not expected
    assert not expected


def test_d1_interleaved_developer_message_is_deferred():
    """Issue #2 D1: legal Responses history must not split a Chat tool result group."""
    items = [
        {"type": "message", "role": "user", "content": "run two commands"},
        _call("call_a1"),
        _call("call_a2"),
        _result("call_a1", "out-1"),
        {"type": "message", "role": "developer", "content": "ctx"},
        _result("call_a2", "out-2"),
    ]

    messages = _convert_input_items(items)
    _assert_well_formed_tool_groups(messages)
    assert [msg["role"] for msg in messages] == [
        "user", "assistant", "tool", "tool", "system",
    ]
    assert [m["tool_call_id"] for m in messages if m["role"] == "tool"] == [
        "call_a1", "call_a2",
    ]
    assert messages[-1]["content"] == "ctx"

    chat = responses_request_to_chat({"model": "test", "input": items})
    assert chat["messages"] == messages

    # The Codex-specific projection runs after request conversion, too.
    chat["tools"] = [
        {"type": "function", "function": {"name": "exec_command", "parameters": {}}}
    ]
    projected, stats = project_responses_chat_body(chat)
    assert stats["aggressive"] is True
    _assert_well_formed_tool_groups(projected["messages"])


def test_deferred_messages_keep_their_relative_order():
    messages = _convert_input_items([
        _call("a"), _call("b"),
        _result("a"),
        {"type": "message", "role": "developer", "content": "guidance"},
        {"type": "message", "role": "user", "content": "later"},
        {"type": "message", "role": "assistant", "content": "ack"},
        _result("b"),
    ])
    _assert_well_formed_tool_groups(messages)
    assert [m["role"] for m in messages] == [
        "assistant", "tool", "tool", "system", "user", "assistant",
    ]
    assert [m["content"] for m in messages[-3:]] == ["guidance", "later", "ack"]


def test_incomplete_group_cannot_erase_unanswered_execution():
    with pytest.raises(ToolPairingError, match="unanswered_tool_calls") as error:
        _convert_input_items([
            {"type": "message", "role": "user", "content": "go"},
            _call("a"), _call("b"),
            _result("a"),
            {"type": "message", "role": "user", "content": "next"},
        ])
    assert error.value.call_ids == ("b",)


def test_unanswered_group_fails_closed():
    with pytest.raises(ToolPairingError, match="unanswered_tool_calls"):
        _convert_input_items([
            {"type": "message", "role": "assistant", "content": "I'll run it"},
            _call("a"),
            {"type": "message", "role": "user", "content": "continue"},
        ])

    with pytest.raises(ToolPairingError, match="unanswered_tool_calls"):
        _convert_input_items([_call("a")])


def test_orphan_duplicate_missing_id_outputs_and_empty_assistants_are_dropped():
    messages = _convert_input_items([
        _result("orphan"),
        {"type": "message", "role": "assistant", "content": ""},
        _call("a"),
        _result("a", "first"),
        _result("a", "first"),
        {"type": "function_call_output", "output": "missing id"},
        {"type": "message", "role": "user", "content": "next"},
    ])
    _assert_well_formed_tool_groups(messages)
    assert [msg["role"] for msg in messages] == ["assistant", "tool", "user"]
    assert [call["id"] for call in messages[0]["tool_calls"]] == ["a"]
    assert messages[1]["content"] == "first"


def test_call_and_output_can_fall_back_to_item_id():
    messages = _convert_input_items([
        {"type": "function_call", "id": "item_a", "name": "shell", "arguments": "{}"},
        {"type": "function_call_output", "id": "item_a", "output": "ok"},
    ])
    _assert_well_formed_tool_groups(messages)
    assert messages[0]["tool_calls"][0]["id"] == "item_a"
    assert messages[1]["tool_call_id"] == "item_a"


def test_multiple_completed_groups_stay_separate():
    messages = _convert_input_items([
        _call("a"), _result("a"),
        {"type": "message", "role": "assistant", "content": "next step"},
        _call("b"), _result("b"),
    ])
    _assert_well_formed_tool_groups(messages)
    assert [msg["role"] for msg in messages] == [
        "assistant", "tool", "assistant", "tool",
    ]
    assert messages[2]["content"] == "next step"


def test_malformed_result_id_cannot_crash_conversion():
    messages = _convert_input_items([
        _call("a"),
        {"type": "function_call_output", "call_id": ["bad"], "output": "ignore"},
        _result("a", "correct"),
    ])
    _assert_well_formed_tool_groups(messages)
    assert len(messages) == 2
    assert messages[1]["content"] == "correct"


def test_already_valid_groups_are_left_in_order():
    items = [
        {"type": "message", "role": "user", "content": "run"},
        _call("a"),
        _call("b"),
        _result("a", "first"),
        _result("b", "second"),
        {"type": "message", "role": "user", "content": "continue"},
    ]
    messages = _convert_input_items(items)
    _assert_well_formed_tool_groups(messages)
    assert [message["role"] for message in messages] == [
        "user", "assistant", "tool", "tool", "user",
    ]
    assert [message["content"] for message in messages if message["role"] == "tool"] == [
        "first", "second",
    ]


@pytest.mark.parametrize("optimize", [False, True])
def test_responses_http_route_handles_long_interleaved_history(monkeypatch, optimize):
    """Exercise the actual /v1/responses route and resulting Responses SSE stream.

    The upstream is deterministic and mocked; this is not a live CodeBuddy test.
    The default proxy mode must forward all valid messages without a length cap.
    """
    original = [{"type": "message", "role": "user", "content": "run commands"}]
    for index in range(1200):
        call_a, call_b = f"call_{index}_a", f"call_{index}_b"
        original.extend([
            _call(call_a), _call(call_b),
            _result(call_a, f"out-{index}-a"),
            {"type": "message", "role": "developer", "content": f"ctx-{index}"},
            _result(call_b, f"out-{index}-b"),
        ])
    original.append({"type": "message", "role": "user", "content": "finish"})
    captured = []
    state = SimpleNamespace(
        enable_optimize_context=optimize,
        verbose_llm=False,
        write_log=lambda *a, **k: None,
        json_logger=None,
        logger=None,
    )
    monkeypatch.setattr(proxy, "proxy_state", state)

    async def fake_forward_chat(body, protocol, original=None):
        assert protocol == "responses"
        captured.extend(body["messages"])
        converter = ResponsesStreamConverter(body.get("model", "test"))

        async def fake_sse():
            chunks = [
                {"choices": [{"delta": {"content": "处理成功"}, "finish_reason": None}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            ]
            for chunk in chunks:
                for event, data in converter.feed_chunk(chunk):
                    yield f"event: {event}\\ndata: {json.dumps(data, ensure_ascii=False)}\\n\\n"
            for event, data in converter.finish():
                yield f"event: {event}\\ndata: {json.dumps(data, ensure_ascii=False)}\\n\\n"

        return StreamingResponse(fake_sse(), media_type="text/event-stream")

    monkeypatch.setattr(proxy, "forward_chat", fake_forward_chat)

    async def send():
        transport = ASGITransport(app=proxy.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post("/v1/responses", json={
                "model": "test",
                "stream": True,
                "input": original,
                "tools": [{
                    "type": "function",
                    "name": "exec_command",
                    "parameters": {"type": "object", "properties": {}},
                }],
            })

    response = asyncio.run(send())
    assert response.status_code == 200
    assert "event: response.completed" in response.text
    assert "处理成功" in response.text
    _assert_well_formed_tool_groups(captured)
    if not optimize:
        # 1200 full two-tool groups + 1200 developer messages + two users.
        assert len(captured) == 4802
        assert sum(msg["role"] == "tool" for msg in captured) == 2400
        assert captured[-1]["content"] == "finish"
    else:
        assert len(captured) < 4802
        assert captured[-1]["content"] == "finish"


def test_responses_cross_group_pending_call_fails_closed():
    with pytest.raises(ToolPairingError, match="unanswered_tool_calls"):
        _convert_input_items([
            _call("a"), _call("b"), _result("a"),
            {"type": "message", "role": "assistant", "content": "new"},
            _call("c"), _result("b"), _result("c"),
        ])


def test_responses_conflicting_duplicate_result_fails_closed():
    with pytest.raises(ToolPairingError, match="conflicting_duplicate_result"):
        _convert_input_items([
            _call("a"), _result("a", "success"), _result("a", "failure"),
        ])


def test_responses_http_route_rejects_incomplete_history(monkeypatch):
    state = SimpleNamespace(
        enable_optimize_context=False,
        verbose_llm=False,
        write_log=lambda *args, **kwargs: None,
        json_logger=None,
        logger=None,
    )
    monkeypatch.setattr(proxy, "proxy_state", state)
    forwarded = []

    async def fake_forward_chat(body, protocol, original=None):
        forwarded.append(body)
        raise AssertionError("Invalid history must not be forwarded upstream")

    monkeypatch.setattr(proxy, "forward_chat", fake_forward_chat)

    async def send():
        transport = ASGITransport(app=proxy.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post("/v1/responses", json={
                "model": "test",
                "input": [_call("a"), _call("b"), _result("a")],
            })

    response = asyncio.run(send())
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "unanswered_tool_calls"
    assert response.json()["detail"]["tool_call_ids"] == ["b"]
    assert not forwarded
