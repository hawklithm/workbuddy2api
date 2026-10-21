"""Regression tests for Anthropic to Chat tool-call/result pairing.

The CodeBuddy backend rejects Chat histories whose tool results are not
adjacent to the assistant message declaring the call IDs with
``code 11148: tool calls and tool results do not match``.  Legal Anthropic
histories routinely produce such shapes (tool_use blocks answered in later
turns, user text between a tool_use and its tool_result, duplicated
tool_result blocks), so conversion must repair them before forwarding.
"""

import json

from codebuddy_proxy.anthropic_adapter import anthropic_request_to_chat


def _tool_use(call_id, name="Bash"):
    return {"type": "tool_use", "id": call_id, "name": name, "input": {"cmd": "ls"}}


def _tool_result(call_id, output="ok"):
    return {"type": "tool_result", "tool_use_id": call_id, "content": output}


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


def _convert(messages):
    """Convert an Anthropic history through the real request path."""
    return anthropic_request_to_chat({"model": "m", "messages": messages})["messages"]


def test_user_text_between_tool_use_and_result_is_deferred():
    """The D1 shape: text interleaved inside a tool call group."""
    messages = _convert([
        {"role": "user", "content": [{"type": "text", "text": "run it"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "sure"}, _tool_use("c1")]},
        {"role": "user", "content": [{"type": "text", "text": "ctx"}, _tool_result("c1")]},
    ])
    _assert_well_formed_tool_groups(messages)
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "user"]
    assert messages[2]["tool_call_id"] == "c1"
    assert messages[3]["content"] == "ctx"

    chat = anthropic_request_to_chat({"model": "m", "messages": [
        {"role": "user", "content": [{"type": "text", "text": "run it"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "sure"}, _tool_use("c1")]},
        {"role": "user", "content": [{"type": "text", "text": "ctx"}, _tool_result("c1")]},
    ]})
    _assert_well_formed_tool_groups(chat["messages"])


def test_parallel_tool_uses_answered_in_separate_turns():
    """Parallel calls whose results arrive in later turns stay adjacent."""
    messages = _convert([
        {"role": "assistant", "content": [_tool_use("c1"), _tool_use("c2", "Read")]},
        {"role": "user", "content": [_tool_result("c1", "r1")]},
        {"role": "user", "content": [_tool_result("c2", "r2")]},
    ])
    _assert_well_formed_tool_groups(messages)
    assert [m["role"] for m in messages] == ["assistant", "tool", "tool"]
    assert [m["tool_call_id"] for m in messages if m["role"] == "tool"] == ["c1", "c2"]


def test_duplicate_result_for_answered_call_is_dropped():
    """A repeated tool_result must not reach the upstream service."""
    messages = _convert([
        {"role": "assistant", "content": [_tool_use("c1")]},
        {"role": "user", "content": [_tool_result("c1", "r1")]},
        {"role": "assistant", "content": [_tool_use("c2", "Read")]},
        {"role": "user", "content": [_tool_result("c2", "r2"), _tool_result("c1", "dup")]},
    ])
    _assert_well_formed_tool_groups(messages)
    assert [m["tool_call_id"] for m in messages if m["role"] == "tool"] == ["c1", "c2"]


def test_unanswered_tool_use_is_removed_but_text_kept():
    """A trailing tool_use with no result is dropped, its text survives."""
    messages = _convert([
        {"role": "assistant", "content": [{"type": "text", "text": "I'll run it"}, _tool_use("c1")]},
        {"role": "user", "content": [{"type": "text", "text": "continue"}]},
    ])
    assert messages == [
        {"role": "assistant", "content": "I'll run it"},
        {"role": "user", "content": "continue"},
    ]


def test_orphan_result_without_declaring_assistant_is_dropped():
    messages = _convert([
        {"role": "assistant", "content": [{"type": "text", "text": "plain"}]},
        {"role": "user", "content": [_tool_result("c9", "orphan")]},
    ])
    assert messages == [{"role": "assistant", "content": "plain"}]


def test_missing_tool_use_id_drops_unmatched_group():
    """An id-less tool_use generates a random call id; an unmatched result is dropped."""
    messages = _convert([
        {"role": "assistant", "content": [{"type": "tool_use", "name": "Bash", "input": {}}]},
        {"role": "user", "content": [_tool_result("call_generated", "r")]},
    ])
    _assert_well_formed_tool_groups(messages)
    assert messages == []


def test_long_agentic_history_stays_well_formed():
    """The 59-message Claude Code shape from the reported 11148 failure."""
    items = [{"role": "user", "content": [{"type": "text", "text": "start"}]}]
    for idx in range(29):
        items.append({
            "role": "assistant",
            "content": [{"type": "text", "text": f"step {idx}"}, _tool_use(f"call_{idx}")],
        })
        items.append({"role": "user", "content": [_tool_result(f"call_{idx}", f"out {idx}")]})

    messages = _convert(items)
    _assert_well_formed_tool_groups(messages)
    assert len(messages) == 1 + 29 * 2

    chat = anthropic_request_to_chat({"model": "deepseek-v4.1-flash", "messages": items})
    _assert_well_formed_tool_groups(chat["messages"])
    assert len(chat["messages"]) == 59


def test_interleaved_messages_are_deferred_until_group_completes():
    """Legal Anthropic history must not split a Chat tool result group."""
    messages = _convert([
        {"role": "user", "content": [{"type": "text", "text": "run two"}]},
        {"role": "assistant", "content": [_tool_use("a1"), _tool_use("a2", "Read")]},
        {"role": "user", "content": [_tool_result("a1", "out-1")]},
        {"role": "user", "content": [{"type": "text", "text": "ctx"}]},
        {"role": "user", "content": [_tool_result("a2", "out-2")]},
    ])
    _assert_well_formed_tool_groups(messages)
    assert [m["role"] for m in messages] == [
        "user", "assistant", "tool", "tool", "user",
    ]
    assert [m["tool_call_id"] for m in messages if m["role"] == "tool"] == ["a1", "a2"]
    assert messages[-1]["content"] == "ctx"


def test_deferred_messages_keep_their_relative_order():
    messages = _convert([
        {"role": "assistant", "content": [_tool_use("a"), _tool_use("b", "Read")]},
        {"role": "user", "content": [_tool_result("a")]},
        {"role": "user", "content": [{"type": "text", "text": "guidance"}]},
        {"role": "user", "content": [{"type": "text", "text": "later"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "ack"}]},
        {"role": "user", "content": [_tool_result("b")]},
    ])
    _assert_well_formed_tool_groups(messages)
    assert [m["role"] for m in messages] == [
        "assistant", "tool", "tool", "user", "user", "assistant",
    ]
    assert [m["content"] for m in messages[-3:]] == ["guidance", "later", "ack"]


def test_plain_conversation_is_untouched():
    body = {
        "system": "be helpful",
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ],
    }
    chat = anthropic_request_to_chat(body)
    assert chat["messages"] == [
        {"role": "system", "content": "be helpful"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]


def test_tool_call_arguments_survive_sanitization():
    """Repair must not drop or alter tool call arguments."""
    arguments = json.dumps({"cmd": "echo hi"})
    messages = _convert([
        {"role": "assistant", "content": [{
            "type": "tool_use", "id": "c1", "name": "Bash", "input": {"cmd": "echo hi"},
        }]},
        {"role": "user", "content": [_tool_result("c1", "hi")]},
    ])
    _assert_well_formed_tool_groups(messages)
    assert messages[0]["tool_calls"][0]["function"]["arguments"] == arguments
