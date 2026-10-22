"""Golden contracts for the non-streaming protocol formatter strategies."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

import codebuddy_proxy.__main__ as proxy
import codebuddy_proxy.protocol_response as formats
from codebuddy_proxy.protocol_response import NonStreamResponseRegistry


@pytest.fixture
def fixed_clock_and_id(monkeypatch):
    monkeypatch.setattr(formats.uuid, "uuid4", lambda: SimpleNamespace(hex="deadbeef"))
    monkeypatch.setattr(proxy, "now_s", lambda: 1710000000)


def _data(*, content="hello", calls=None):
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": "upstream-model",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content, "tool_calls": calls},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    }


def _calls():
    return [
        {"id": "a", "function": {"name": "read", "arguments": '{"path":"a.txt"}'}},
        {"id": "b", "function": {"name": "shell", "arguments": "not json"}},
    ]


@pytest.mark.parametrize("protocol", ["openai", "unknown", ""])
def test_openai_and_unknown_protocols_preserve_identity(protocol):
    data = _data(calls=_calls())
    assert proxy.convert_nonstream(data, protocol, None) is data


def test_anthropic_text_only_contract(fixed_clock_and_id):
    data = _data()
    original = deepcopy(data)
    output = proxy.convert_nonstream(data, "anthropic", {"model": "client-model"})
    assert output == {
        "id": "msg_deadbeef",
        "type": "message",
        "role": "assistant",
        "model": "client-model",
        "content": [{"type": "text", "text": "hello"}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 11, "output_tokens": 7},
    }
    assert data == original


def test_anthropic_tool_call_args_fallback_preserves_existing_structure(fixed_clock_and_id):
    output = proxy.convert_nonstream(
        _data(content="", calls=_calls()), "anthropic", None,
    )
    assert output == {
        "id": "msg_deadbeef",
        "type": "message",
        "role": "assistant",
        "model": "upstream-model",
        "content": [
            {"type": "tool_use", "id": "a", "name": "read", "input": {"path": "a.txt"}},
            {"type": "tool_use", "id": "b", "name": "shell", "input": "not json"},
        ],
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": {"input_tokens": 11, "output_tokens": 7},
    }


def test_responses_text_and_tools_contract(fixed_clock_and_id):
    data = _data(content="ready", calls=_calls())
    original = deepcopy(data)
    assert proxy.convert_nonstream(data, "responses", None) == {
        "id": "resp_deadbeef",
        "object": "response",
        "created_at": 1710000000,
        "status": "completed",
        "output": [{
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "output_text", "text": "ready"},
                {"type": "function_call", "id": "a", "name": "read", "arguments": {"path": "a.txt"}},
                {"type": "function_call", "id": "b", "name": "shell", "arguments": "not json"},
            ],
        }],
    }
    assert data == original


@pytest.mark.parametrize("protocol", ["anthropic", "responses"])
def test_empty_choice_and_missing_usage_keep_defaults(fixed_clock_and_id, protocol):
    output = proxy.convert_nonstream(
        {"model": "upstream", "choices": [], "usage": None}, protocol, None,
    )
    if protocol == "anthropic":
        assert output["content"] == []
        assert output["stop_reason"] == "end_turn"
        assert output["usage"] == {"input_tokens": 0, "output_tokens": 0}
        assert output["model"] == "upstream"
    else:
        assert output["output"][0]["content"] == []


def test_missing_function_arguments_becomes_empty_dict(fixed_clock_and_id):
    tool = {"id": "t", "function": {"name": "read"}}
    anthropic = proxy.convert_nonstream(_data(calls=[tool]), "anthropic", None)
    responses = proxy.convert_nonstream(_data(calls=[tool]), "responses", None)
    assert anthropic["content"][1]["input"] == {}
    assert responses["output"][0]["content"][1]["arguments"] == {}


def test_custom_protocol_strategy_can_be_registered_without_changing_routes():
    class Custom:
        def format(self, data, original, now_s):
            return {"custom": True, "original": original, "now": now_s()}

    registry = NonStreamResponseRegistry({"new_protocol": Custom()})
    data = _data()
    assert registry.convert(data, "new_protocol", {"model": "m"}, lambda: 24) == {
        "custom": True, "original": {"model": "m"}, "now": 24,
    }
    assert registry.convert(data, "unknown", None, lambda: 24) is data
