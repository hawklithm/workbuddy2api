"""Offline regressions for the Buddy-inspired, stateless context compatibility slice."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import codebuddy_proxy.__main__ as proxy
from codebuddy_proxy.responses_adapter import responses_request_to_chat, ResponsesContextError
from codebuddy_proxy.upstream_request import ChatRequestPreparer


def test_output_text_history_and_parallel_calls_preserve_content():
    body = {"input": [
        {"role": "developer", "content": "Keep original instruction"},
        {"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": "First "},
            {"type": "output_text", "text": "answer 中文"},
        ]},
        {"type": "function_call", "call_id": "a", "name": "read", "arguments": "{}"},
        {"type": "reasoning", "summary": []},
        {"type": "function_call", "call_id": "b", "name": "read", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "a", "output": "A"},
        {"type": "function_call_output", "call_id": "b", "output": "B"},
        {"role": "user", "content": "Continue"},
    ], "parallel_tool_calls": False}
    original = deepcopy(body)
    chat = responses_request_to_chat(body)
    assert chat["parallel_tool_calls"] is False
    assert chat["messages"][0] == {"role": "system", "content": "Keep original instruction"}
    assert chat["messages"][1]["content"] == "First answer 中文"
    assert [c["id"] for c in chat["messages"][1]["tool_calls"]] == ["a", "b"]
    assert [m["content"] for m in chat["messages"][2:]] == ["A", "B", "Continue"]
    assert body == original


@pytest.mark.parametrize("image", ["https://example.invalid/image.png", {"url": "data:image/png;base64,TEST"}])
def test_image_history_preserves_part_order_without_fetching(image):
    body = {"input": [{"role": "user", "content": [
        {"type": "input_text", "text": "Before"},
        {"type": "input_image", "image_url": image, "detail": "low"},
        {"type": "input_text", "text": "After"},
    ]}]}
    original = deepcopy(body)
    content = responses_request_to_chat(body)["messages"][0]["content"]
    assert [p["type"] for p in content] == ["text", "image_url", "text"]
    assert content[1]["image_url"] == {"url": image["url"] if isinstance(image, dict) else image, "detail": "low"}
    content[1]["image_url"]["url"] = "changed"
    assert body == original


def test_shorthand_assistant_output_text_is_not_erased():
    chat = responses_request_to_chat({"input": [
        {"role": "assistant", "content": [{"type": "output_text", "text": "Prior answer"}]},
        {"role": "user", "content": "next"},
    ]})
    assert chat["messages"][0] == {"role": "assistant", "content": "Prior answer"}


def test_named_tool_choice_survives_preparation_as_exact_name():
    body = {"input": "hello", "tool_choice": {"type": "function", "name": "lookup"}}
    original = deepcopy(body)
    chat = responses_request_to_chat(body)
    client = SimpleNamespace(
        profile=SimpleNamespace(key="custom", chat_path="/chat"),
        build_url=lambda path: "https://example.invalid" + path,
        auth_headers=lambda: {},
    )
    prepared = ChatRequestPreparer(
        client=client, enable_desensitize=False,
        desensitize_body=lambda value, **kwargs: value,
        normalize_tool_choice=proxy._normalize_tool_choice,
        diagnostic=lambda *args, **kwargs: None,
    ).prepare(chat, "responses")
    assert prepared.body["tool_choice"] == "lookup"
    assert body == original


@pytest.mark.parametrize("choice", ["auto", "none", "required", {"type": "function", "function": {"name": "read"}}])
def test_existing_tool_choices_are_unchanged(choice):
    chat = responses_request_to_chat({"input": "hello", "tool_choice": choice})
    assert chat["tool_choice"] == choice


def test_state_reference_fails_explicitly_instead_of_losing_history():
    with pytest.raises(ResponsesContextError, match="complete input history"):
        responses_request_to_chat({"input": "next", "previous_response_id": "resp_private"})


def test_http_state_reference_rejected_before_forwarding(monkeypatch):
    state = SimpleNamespace(verbose_llm=False, json_logger=None, logger=None,
                            write_log=lambda *a, **k: None)
    monkeypatch.setattr(proxy, "proxy_state", state)

    async def forbidden(*a, **k):
        pytest.fail("No upstream forwarding allowed")
    monkeypatch.setattr(proxy, "forward_chat", forbidden)
    with TestClient(proxy.app) as http:
        response = http.post("/v1/responses", json={
            "input": "next", "previous_response_id": "resp_private",
        })
    assert response.status_code == 400
    assert "unsupported_context" in response.text
    assert "resp_private" not in response.text
