"""Offline end-to-end codec contracts: declarations, choice, history and output."""
from copy import deepcopy
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from codebuddy_proxy.responses_tools import ResponsesTools, ToolTranslationError
from codebuddy_proxy.responses_adapter import responses_request_to_chat, ResponsesStreamConverter
import codebuddy_proxy.__main__ as proxy


def original():
    return {"stream": True, "tools": [
        {"type": "namespace", "namespace": "mcp_search", "tools": [
            {"type": "function", "name": "run", "parameters": {"type": "object"}},
            {"type": "custom", "name": "patch", "description": "Apply edits"},
        ]},
        {"type": "function", "name": "run", "parameters": {"type": "object"}},
    ], "input": "start"}


def chunk(name, arguments, *, first=True):
    return {"choices": [{"delta": {"tool_calls": [{
        "index": 0, **({"id": "call_a"} if first else {}),
        "function": {**({"name": name} if first else {}), "arguments": arguments},
    }]}}]}


def test_namespace_declaration_choice_and_history_round_trip():
    body = original()
    body["tool_choice"] = {"type": "function", "namespace": "mcp_search", "name": "run"}
    before = deepcopy(body)
    codec = ResponsesTools(body)
    alias = codec.by_identity[("mcp_search", "run")]
    chat = responses_request_to_chat(body)
    assert chat["tools"][0]["function"]["name"] == alias
    assert chat["tools"][-1]["function"]["name"] == "run"
    assert chat["tool_choice"]["function"]["name"] == alias
    converter = ResponsesStreamConverter("test", original=body)
    events = converter.feed_chunk(chunk(alias, '{"x":1}')) + converter.finish()
    item = next(d["response"]["output"][0] for e, d in events if e == "response.completed")
    assert item["name"] == "run" and item["namespace"] == "mcp_search"
    body2 = {**body, "input": [item, {"type": "function_call_output", "call_id": "call_a", "output": "ok"}]}
    history = responses_request_to_chat(body2)["messages"]
    assert history[0]["tool_calls"][0]["function"]["name"] == alias
    assert history[1]["tool_call_id"] == "call_a"
    assert body == before


@pytest.mark.parametrize("raw", ["*** Begin Patch\n中文 \\\"quoted\\\"\n*** End Patch", "", '{"literal":true}'])
def test_custom_fragmented_stream_and_multiturn_are_lossless(raw):
    body = original()
    alias = ResponsesTools(body).by_identity[("mcp_search", "patch")]
    encoded = json.dumps({"input": raw}, ensure_ascii=False)
    converter = ResponsesStreamConverter("test", original=body)
    events = []
    for i, piece in enumerate(encoded):
        events += converter.feed_chunk(chunk(alias, piece, first=i == 0))
    # No envelope fragments must reach the client as custom tool input.
    assert not any(e.endswith("_arguments.delta") for e, _ in events)
    events += converter.finish()
    item = next(d["response"]["output"][0] for e, d in events if e == "response.completed")
    assert item["type"] == "custom_tool_call"
    assert item["input"] == raw and item["namespace"] == "mcp_search"
    deltas = [d["delta"] for e, d in events if e == "response.custom_tool_call_input.delta"]
    assert "".join(deltas) == raw
    history = responses_request_to_chat({**body, "input": [
        item, {"type": "custom_tool_call_output", "call_id": "call_a", "output": "done"}
    ]})["messages"]
    fn = history[0]["tool_calls"][0]["function"]
    assert fn["name"] == alias and json.loads(fn["arguments"])["input"] == raw


def test_custom_invalid_envelope_fails_without_completed():
    body = original()
    alias = ResponsesTools(body).by_identity[("mcp_search", "patch")]
    converter = ResponsesStreamConverter("test", original=body)
    converter.feed_chunk(chunk(alias, '{"input":123}'))
    events = converter.finish()
    assert [e for e, _ in events] == ["response.failed"]


def test_alias_is_request_scoped_and_collisions_fail():
    body = original()
    codec = ResponsesTools(body)
    alias = codec.by_identity[("mcp_search", "run")]
    assert ResponsesTools({}).restore_item({"name": alias}) == {"name": alias}
    body["tools"].append({"type": "function", "name": alias})
    with pytest.raises(ToolTranslationError, match="collides"):
        ResponsesTools(body)


@pytest.mark.parametrize("fmt", [{"type": "grammar", "syntax": "lark", "definition": "x"}, {"type": "json_schema"}, "grammar"])
def test_unsupported_custom_format_is_rejected_not_silently_dropped(fmt):
    body = {"tools": [{"type": "custom", "name": "patch", "format": fmt}], "input": "x"}
    with pytest.raises(ToolTranslationError, match="formats are not supported"):
        responses_request_to_chat(body)


def test_interleaved_parallel_tools_have_stable_distinct_output_indices():
    body = original()
    codec = ResponsesTools(body)
    normal = codec.by_identity[("mcp_search", "run")]
    custom = codec.by_identity[("mcp_search", "patch")]
    converter = ResponsesStreamConverter("test", original=body)
    events = converter.feed_chunk(chunk(normal, "{}"))
    events += converter.feed_chunk({"choices": [{"delta": {"content": "middle"}}]})
    events += converter.feed_chunk({"choices": [{"delta": {"tool_calls": [{
        "index": 1, "id": "call_b", "function": {"name": custom, "arguments": '{"input":"raw"}'}
    }]}}]})
    events += converter.finish()
    added = [(d["item"]["id"], d["output_index"]) for e, d in events if e == "response.output_item.added"]
    assert [index for _, index in added] == [0, 1, 2]
    expected = dict(added)
    for event, data in events:
        item_id = (data.get("item") or {}).get("id") or data.get("item_id")
        if item_id:
            assert data["output_index"] == expected[item_id], (event, data)
    completed = next(d["response"] for e, d in events if e == "response.completed")
    assert [item["id"] for item in completed["output"]] == [item_id for item_id, _ in added]
    assert completed["output"][2]["type"] == "custom_tool_call"


def test_plain_custom_tool_round_trip():
    body = {"tools": [{"type": "custom", "name": "patch"}], "input": "edit",
            "tool_choice": {"type": "custom", "name": "patch"}}
    chat = responses_request_to_chat(body)
    alias = chat["tools"][0]["function"]["name"]
    assert chat["tool_choice"]["function"]["name"] == alias
    converter = ResponsesStreamConverter("test", original=body)
    events = converter.feed_chunk(chunk(alias, '{"input":"raw"}')) + converter.finish()
    item = next(d["response"]["output"][0] for e, d in events if e == "response.completed")
    assert item["name"] == "patch" and item["input"] == "raw"
    assert "namespace" not in item


def test_http_nonstream_custom_tool_uses_json_and_restores_identity(monkeypatch):
    client = SimpleNamespace(profile=SimpleNamespace(key="custom", chat_path="/chat"),
        build_url=lambda path: "https://example.invalid" + path, auth_headers=lambda: {})
    state = SimpleNamespace(client=client, ensure_auth=lambda: None,
        enable_desensitize=False, enable_optimize_context=False, verbose_llm=False,
        json_logger=None, logger=None, write_log=lambda *a, **k: None,
        write_body_log=lambda *a, **k: None)
    monkeypatch.setattr(proxy, "proxy_state", state)
    async def collect(url, headers, body, protocol):
        assert body["stream"] is True  # upstream SSE, downstream JSON
        alias = body["tools"][1]["function"]["name"]
        return {"choices": [{"message": {"tool_calls": [{"id": "x", "function": {
            "name": alias, "arguments": '{"input":"literal patch"}'}}]}}]}
    monkeypatch.setattr(proxy, "collect_upstream", collect)
    body = original()
    body["stream"] = False
    with TestClient(proxy.app) as http:
        response = http.post("/v1/responses", json=body)
    assert response.status_code == 200, response.text
    assert "application/json" in response.headers["content-type"]
    item = response.json()["output"][0]
    assert item["type"] == "custom_tool_call" and item["input"] == "literal patch"
    assert item["namespace"] == "mcp_search" and item["name"] == "patch"


def test_duplicate_declaration_rejected_without_overwrite():
    body = original()
    body["tools"][0]["tools"].append(body["tools"][0]["tools"][0])
    with pytest.raises(ToolTranslationError, match="Duplicate"):
        responses_request_to_chat(body)


def test_nonstream_output_restores_custom_and_namespace():
    body = original()
    body["stream"] = False
    alias = ResponsesTools(body).by_identity[("mcp_search", "patch")]
    assert responses_request_to_chat(body)["stream"] is False
    response = proxy.convert_nonstream({"choices": [{"message": {
        "content": "ready", "tool_calls": [{"id": "a", "function": {
            "name": alias, "arguments": json.dumps({"input": "patch text"})}}]
    }}]}, "responses", body)
    assert response["output"][1] == {"type": "custom_tool_call", "id": "a",
        "call_id": "a", "name": "patch", "namespace": "mcp_search", "input": "patch text"}


@pytest.mark.parametrize("lines", [
    ['data: {"error":{"message":"not allowed"}}', "data: [DONE]"],
    ['data: {"choices":[{"delta":{"content":"partial"}}]}'],
])
def test_nonstream_never_turns_embedded_error_or_eof_into_success(monkeypatch, lines):
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    class Response:
        status_code = 200
        async def aiter_lines(self):
            for line in lines: yield line
    @asynccontextmanager
    async def fake_open(*args, **kwargs):
        yield Response()
    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda **kwargs: Client())
    monkeypatch.setattr(proxy, "open_upstream_stream", fake_open)
    monkeypatch.setattr(proxy, "proxy_state", SimpleNamespace(verbose_llm=False))
    with pytest.raises(HTTPException) as error:
        asyncio.run(proxy.collect_upstream("https://invalid", {}, {}, "responses"))
    assert error.value.status_code == 502
