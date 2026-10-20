"""HTTP/SSE error propagation must preserve upstream failure semantics.

All tests use a fake upstream; no CodeBuddy credentials or live API requests.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import codebuddy_proxy.__main__ as proxy


class FakeUpstreamResponse:
    def __init__(self, status=200, lines=(), body=b""):
        self.status_code = status
        self.lines = list(lines)
        self.body = body
        self.closed = False

    async def aread(self):
        return self.body

    async def aiter_lines(self):
        for line in self.lines:
            if isinstance(line, Exception):
                raise line
            yield line

    async def aclose(self):
        self.closed = True


class FakeUpstreamClient:
    def __init__(self, response, exc=None):
        self.response = response
        self.exc = exc
        self.closed = False
        self.post_body = None

    def build_request(self, method, url, **kwargs):
        self.post_body = kwargs.get("json")
        return object()

    async def send(self, request, *, stream=False):
        assert stream is True
        if self.exc:
            raise self.exc
        return self.response

    async def aclose(self):
        self.closed = True


@pytest.fixture
def fake_upstream(monkeypatch):
    client_backend = MagicMock()
    client_backend.profile.chat_path = "/v2/chat/completions"
    client_backend.build_url.return_value = "https://upstream.example/v2/chat/completions"
    client_backend.auth_headers.return_value = {}
    state = SimpleNamespace(
        ensure_auth=lambda: None,
        client=client_backend,
        enable_desensitize=False,
        enable_optimize_context=False,
        verbose_llm=False,
        logger=None,
        json_logger=None,
        write_log=lambda *a, **k: None,
        write_body_log=lambda *a, **k: None,
    )
    monkeypatch.setattr(proxy, "proxy_state", state)

    def use(response=None, exc=None):
        fake = FakeUpstreamClient(response, exc=exc)
        monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda **kwargs: fake)
        return fake

    return use


@pytest.mark.parametrize("path,body", [
    ("/v1/chat/completions", {"model": "test", "stream": True, "messages": [{"role": "user", "content": "hi"}]}),
    ("/v1/responses", {"model": "test", "stream": True, "input": "hi"}),
    ("/v1/messages", {"model": "test", "stream": True, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 100}),
])
def test_upstream_http_400_is_not_wrapped_in_http_200(fake_upstream, path, body):
    upstream = FakeUpstreamResponse(
        status=400,
        body=b'{"code":11148,"msg":"tool calls and tool results do not match"}',
    )
    client = fake_upstream(upstream)
    with TestClient(proxy.app) as http:
        response = http.post(path, json=body)
    assert response.status_code == 400
    assert "text/event-stream" not in response.headers["content-type"]
    assert "11148" in response.text
    assert client.closed and upstream.closed
    assert client.post_body is not None


def test_preflight_transport_error_returns_real_502(fake_upstream):
    import httpx

    # Save the actual exception class before replacing AsyncClient.
    fake_upstream(exc=httpx.ConnectError("connection refused"))
    with TestClient(proxy.app) as http:
        response = http.post("/v1/responses", json={"model": "test", "stream": True, "input": "hi"})
    assert response.status_code == 502
    assert response.json()["error"]["type"] == "upstream_error"


def test_streaming_preflight_retries_connection_and_preserves_real_status(fake_upstream, monkeypatch):
    import httpx

    upstream = FakeUpstreamResponse(lines=[
        'data: {"choices":[{"index":0,"delta":{"content":"ok"}}]}',
        "data: [DONE]",
    ])
    client = fake_upstream(upstream)
    original_send = client.send
    calls = []
    records = []

    async def flaky_send(request, *, stream=False):
        calls.append(request)
        if len(calls) <= 2:
            raise httpx.ConnectError("transient connection failure")
        return await original_send(request, stream=stream)

    async def no_backoff(delay):
        pass

    client.send = flaky_send
    monkeypatch.setattr(proxy.asyncio, "sleep", no_backoff)
    proxy.get_state().write_log = lambda event, **fields: records.append(
        {"event": event, **fields}
    )

    with TestClient(proxy.app) as http:
        response = http.post(
            "/v1/chat/completions",
            json={"model": "test", "stream": True,
                  "messages": [{"role": "user", "content": "hello"}]},
        )

    assert response.status_code == 200
    assert "ok" in response.text
    assert response.text.count("[DONE]") == 1
    assert len(calls) == 3
    assert client.closed and upstream.closed
    retries = [event for event in records if event["event"] == "upstream_connect_retry"]
    assert [event["attempt"] for event in retries] == [1, 2]
    completion = next(event for event in records if event["event"] == "stream_completed")
    assert all(event["request_id"] == completion["request_id"] for event in retries)


def test_responses_success_stream_emits_completed_and_closes_connection(fake_upstream):
    chunk = json.dumps({
        "id": "upstream-1", "model": "test", "choices": [
            {"index": 0, "delta": {"content": "中 文 字"}, "finish_reason": None}
        ],
    }, ensure_ascii=False)
    upstream = FakeUpstreamResponse(lines=["data: " + chunk, "data: [DONE]"])
    client = fake_upstream(upstream)
    with TestClient(proxy.app) as http:
        response = http.post("/v1/responses", json={"model": "test", "stream": True, "input": "hi"})
    assert response.status_code == 200
    assert "event: response.completed" in response.text
    assert "event: response.failed" not in response.text
    assert "中 文 字" in response.text  # C2: never silently rewrite model text
    assert client.closed and upstream.closed


def test_responses_incomplete_upstream_stream_is_failed_not_completed(fake_upstream):
    chunk = json.dumps({
        "id": "upstream-1", "model": "test", "choices": [
            {"index": 0, "delta": {"content": "partial"}, "finish_reason": None}
        ],
    })
    upstream = FakeUpstreamResponse(lines=["data: " + chunk])
    client = fake_upstream(upstream)
    with TestClient(proxy.app) as http:
        response = http.post("/v1/responses", json={"model": "test", "stream": True, "input": "hi"})
    assert response.status_code == 200
    assert "event: response.failed" in response.text
    assert "event: response.completed" not in response.text
    assert client.closed and upstream.closed


def test_responses_embedded_error_does_not_emit_completed(fake_upstream):
    upstream = FakeUpstreamResponse(lines=[
        'data: {"error":{"code":11148,"message":"tool result mismatch"}}',
        "data: [DONE]",
    ])
    fake_upstream(upstream)
    with TestClient(proxy.app) as http:
        response = http.post("/v1/responses", json={"model": "test", "stream": True, "input": "hi"})
    assert response.status_code == 200
    assert "event: response.failed" in response.text
    assert "event: response.completed" not in response.text
    assert "tool result mismatch" in response.text


def test_chat_stream_keeps_exact_cjk_spacing(fake_upstream):
    chunk = json.dumps({"choices": [{"delta": {"content": "我 爱 中 文"}}]}, ensure_ascii=False)
    upstream = FakeUpstreamResponse(lines=["data: " + chunk, "data: [DONE]"])
    fake_upstream(upstream)
    with TestClient(proxy.app) as http:
        response = http.post("/v1/chat/completions", json={
            "model": "test", "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        })
    assert response.status_code == 200
    assert json.dumps("我 爱 中 文", ensure_ascii=False) in response.text


def test_preflight_timeout_returns_504(fake_upstream):
    import httpx

    fake_upstream(exc=httpx.ReadTimeout("upstream timed out"))
    with TestClient(proxy.app) as http:
        response = http.post("/v1/responses", json={
            "model": "test", "stream": True, "input": "hi",
        })
    assert response.status_code == 504


def test_responses_midstream_failure_is_not_false_completion(fake_upstream):
    import httpx

    upstream = FakeUpstreamResponse(lines=[
        'data: {"choices":[{"delta":{"content":"partial"}}]}',
        httpx.ReadError("disconnected"),
    ])
    fake_upstream(upstream)
    with TestClient(proxy.app) as http:
        response = http.post("/v1/responses", json={"model": "test", "stream": True, "input": "hi"})
    assert response.status_code == 200  # Headers were already committed.
    assert "event: response.failed" in response.text
    assert "event: response.completed" not in response.text
