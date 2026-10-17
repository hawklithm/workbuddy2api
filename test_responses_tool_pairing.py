"""Regression tests for Responses to Chat tool-call/result pairing."""

from codebuddy_proxy.responses_adapter import _convert_input_items, responses_request_to_chat
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


def test_incomplete_group_keeps_only_answered_calls():
    messages = _convert_input_items([
        {"type": "message", "role": "user", "content": "go"},
        _call("a"), _call("b"),
        _result("a"),
        {"type": "message", "role": "user", "content": "next"},
    ])
    _assert_well_formed_tool_groups(messages)
    assert [c["id"] for c in messages[1]["tool_calls"]] == ["a"]
    assert messages[2]["tool_call_id"] == "a"
    assert messages[3]["content"] == "next"


def test_unanswered_group_removed_but_assistant_text_preserved():
    messages = _convert_input_items([
        {"type": "message", "role": "assistant", "content": "I'll run it"},
        _call("a"),
        {"type": "message", "role": "user", "content": "continue"},
    ])
    assert messages == [
        {"role": "assistant", "content": "I'll run it"},
        {"role": "user", "content": "continue"},
    ]

    messages = _convert_input_items([_call("a")])
    assert messages == []


def test_orphan_duplicate_missing_id_outputs_and_empty_assistants_are_dropped():
    messages = _convert_input_items([
        _result("orphan"),
        {"type": "message", "role": "assistant", "content": ""},
        _call("a"),
        _call("a"),
        _result("a", "first"),
        _result("a", "duplicate"),
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
