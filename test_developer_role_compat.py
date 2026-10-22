"""Compatibility regression for developer messages on the domestic CodeBuddy gateway.

No live CodeBuddy credentials or network are used in this module.
"""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import codebuddy_proxy.__main__ as proxy
from codebuddy_proxy.role_compat import adapt_developer_roles, uses_legacy_chat_roles


def test_legacy_role_detection_is_strictly_scoped():
    assert uses_legacy_chat_roles("domestic", "https://copilot.tencent.com/v2/chat/completions")
    assert not uses_legacy_chat_roles("global", "https://www.codebuddy.ai/v2/chat/completions")
    assert not uses_legacy_chat_roles("domestic", "https://example.test/v2/chat/completions?host=copilot.tencent.com")
    assert not uses_legacy_chat_roles("domestic", "https://copilot.tencent.com.example.test/v2/chat/completions")
    assert not uses_legacy_chat_roles("global", "https://copilot.tencent.com/v2/chat/completions")


def test_developer_role_mapping_preserves_order_data_and_input():
    messages = [
        {"role": "system", "content": "platform policy"},
        {"role": "developer", "content": [{"type": "text", "text": "coding policy"}], "name": "rules"},
        {"role": "user", "content": "run"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "shell", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        {"role": "developer", "content": "second rule"},
    ]
    original = deepcopy(messages)

    converted, count = adapt_developer_roles(messages)

    assert count == 2
    assert [m["role"] for m in converted] == [
        "system", "system", "user", "assistant", "tool", "system",
    ]
    assert converted[1]["content"] == original[1]["content"]
    assert converted[1]["name"] == "rules"
    assert converted[3:5] == original[3:5]
    assert messages == original
    assert converted[1] is not messages[1]
    assert converted[1]["content"] is messages[1]["content"]


def test_no_developer_does_not_modify_messages():
    messages = [{"role": "user", "content": "hi"}]
    result, count = adapt_developer_roles(messages)
    assert result is messages
    assert count == 0


class StubResponse:
    status_code = 200

    async def aiter_lines(self):
        yield 'data: {"id":"msg-test","model":"test","choices":[{"index":0,"delta":{"content":"OK"},"finish_reason":null}]}'
        yield "data: [DONE]"

    async def aclose(self):
        pass


class StubAsyncClient:
    def __init__(self):
        self.sent_body = None
        self.closed = False

    def build_request(self, method, url, *, headers, json):
        self.sent_body = json
        return object()

    async def send(self, request, *, stream):
        return StubResponse()

    async def aclose(self):
        self.closed = True


@pytest.fixture
def capture_gateway(monkeypatch):
    gateway = SimpleNamespace(
        profile=SimpleNamespace(key="domestic", chat_path="/v2/chat/completions"),
        build_url=lambda path: "https://copilot.tencent.com" + path,
        auth_headers=lambda: {},
    )
    proxy_state = SimpleNamespace(
        client=gateway,
        ensure_auth=lambda: None,
        enable_desensitize=True,
        enable_optimize_context=False,
        verbose_llm=False,
        logger=None,
        json_logger=None,
        write_log=lambda *args, **kwargs: None,
        write_body_log=lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(proxy, "proxy_state", proxy_state)
    return gateway


@pytest.mark.parametrize("stream", [True, False])
def test_chat_endpoint_sends_compatible_role_before_desensitize(
    monkeypatch, capture_gateway, stream
):
    body = {
        "model": "test",
        "stream": stream,
        "messages": [
            {"role": "developer", "content": "You are a coding agent running in the Codex CLI"},
            {"role": "user", "content": "hi"},
        ],
    }
    sent = []
    if stream:
        stub = StubAsyncClient()
        monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda **kwargs: stub)
    else:
        async def collect(url, headers, upstream_body, protocol):
            sent.append(upstream_body)
            return {"model": "test", "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"},
            ]}
        monkeypatch.setattr(proxy, "collect_upstream", collect)

    with TestClient(proxy.app) as http:
        result = http.post("/v1/chat/completions", json=body)

    assert result.status_code == 200
    upstream_body = stub.sent_body if stream else sent[0]
    assert [m["role"] for m in upstream_body["messages"]] == ["system", "user"]
    assert upstream_body["messages"][0]["content"] != body["messages"][0]["content"]
    assert body["messages"][0]["role"] == "developer"
    if stream:
        assert "[DONE]" in result.text


@pytest.mark.parametrize("endpoint,backend,expected_roles", [
    ("https://www.codebuddy.ai", "global", ["developer", "user"]),
    ("https://custom.example.test", "domestic", ["developer", "user"]),
    ("https://copilot.tencent.com", "domestic", ["system", "user"]),
])
def test_gateway_policy_applies_only_to_supported_backend(
    monkeypatch, capture_gateway, endpoint, backend, expected_roles
):
    capture_gateway.profile.key = backend
    capture_gateway.build_url = lambda path: endpoint + path
    capture_gateway.conversation_headers = lambda body: {}
    fake = StubAsyncClient()
    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda **kwargs: fake)
    with TestClient(proxy.app) as http:
        response = http.post("/v1/chat/completions", json={
            "model": "test", "stream": True,
            "messages": [
                {"role": "developer", "content": "do the work"},
                {"role": "user", "content": "hi"},
            ],
        })
    assert response.status_code == 200
    assert [m["role"] for m in fake.sent_body["messages"]] == expected_roles


@pytest.mark.parametrize("path,body", [
    ("/v1/responses", {
        "model": "test", "stream": False,
        "input": [{"role": "developer", "content": "coding guidance"}, {"role": "user", "content": "hi"}],
    }),
    ("/v1/messages", {
        "model": "test", "stream": False, "max_tokens": 32,
        "system": "coding guidance", "messages": [{"role": "user", "content": "hi"}],
    }),
])
def test_other_protocols_keep_instruction_roles(monkeypatch, capture_gateway, path, body):
    sent = []
    stub = StubAsyncClient()
    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda **kwargs: stub)

    async def collect(url, headers, upstream_body, protocol):
        sent.append(upstream_body)
        return {"model": "test", "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"},
        ]}
    monkeypatch.setattr(proxy, "collect_upstream", collect)
    with TestClient(proxy.app) as http:
        response = http.post(path, json=body)
    assert response.status_code == 200, response.text
    upstream_body = stub.sent_body if path == "/v1/responses" else sent[0]
    assert upstream_body["messages"][0]["role"] == "system"
    assert upstream_body["messages"][0]["content"] == "coding guidance"
