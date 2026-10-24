"""HTTP contract and refresh behavior for PR #7 check-in endpoints.

All tests use httpx.MockTransport, not real CodeBuddy credentials/network.
"""

import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import codebuddy_proxy.__main__ as proxy
import codebuddy_proxy.billing_proxy as billing


class FakeCodeBuddyClient:
    def __init__(self):
        self.token = "old"
        self.refresh_calls = 0
        self.refresh_outcome = True

    def build_url(self, path):
        return "https://upstream.example" + path

    def auth_headers(self):
        return {"Authorization": "Bearer " + self.token, "X-Product-Code": "codebuddy"}

    def refresh(self):
        self.refresh_calls += 1
        if isinstance(self.refresh_outcome, Exception):
            raise self.refresh_outcome
        if self.refresh_outcome:
            self.token = "new"
        return self.refresh_outcome


@pytest.fixture
def environment(monkeypatch):
    client = FakeCodeBuddyClient()
    state = SimpleNamespace(
        client=client, ensure_auth=lambda: None, logger=None, json_logger=None,
    )
    monkeypatch.setattr(proxy, "proxy_state", state)

    real_async_client = httpx.AsyncClient

    def install(handler):
        transport = httpx.MockTransport(handler)
        monkeypatch.setattr(
            billing.httpx, "AsyncClient",
            lambda **kwargs: real_async_client(transport=transport, **kwargs),
        )

    return client, state, install, real_async_client


@pytest.mark.parametrize("method,path,upstream_method,upstream_path", [
    ("GET", "/v1/checkin-status", "POST", "/v2/billing/meter/checkin-activity-status"),
    ("POST", "/v1/checkin-status", "POST", "/v2/billing/meter/checkin-activity-status"),
    ("POST", "/v1/checkin", "POST", "/v2/billing/meter/daily-checkin"),
])
def test_forward_method_url_headers_and_raw_business_body(environment, method, path, upstream_method, upstream_path):
    client, state, install, _ = environment
    requests = []
    payload = {"code": 10001, "msg": "already checked in", "data": None}

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=payload)

    install(handler)
    with TestClient(proxy.app) as app:
        result = app.request(method, path)

    assert result.status_code == 200
    assert result.json() == payload
    assert list(result.json()) == ["code", "msg", "data"]
    assert len(requests) == 1
    upstream = requests[0]
    assert upstream.method == upstream_method
    assert upstream.url.path == upstream_path
    assert upstream.headers["authorization"] == "Bearer old"
    if upstream_method == "GET":
        assert upstream.content == b""
        assert "content-type" not in upstream.headers
    else:
        assert json.loads(upstream.content) == {}


@pytest.mark.parametrize("status", [400, 401, 429, 500, 503])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_forwards_upstream_error_status_and_body(environment, status, method):
    client, _, install, _ = environment
    client.refresh_outcome = False
    payload = b'{"code":11148,"msg":"upstream error","data":null}'
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, content=payload, headers={"Content-Type": "application/json; charset=utf-8"})

    install(handler)
    with TestClient(proxy.app) as app:
        result = app.request(method, "/v1/checkin-status")

    assert result.status_code == status
    assert result.content == payload
    assert result.headers["content-type"] == "application/json; charset=utf-8"
    assert len(calls) == 1
    assert client.refresh_calls == (1 if status == 401 else 0)


@pytest.mark.parametrize("body,content_type", [
    (b"<html>gateway failed</html>", "text/html; charset=latin-1"),
    (b"service unavailable", "text/plain"),
    (b"\xff\x00\x7f", "application/octet-stream"),
    (b"[]", "application/json"),
])
def test_preserves_non_dict_non_json_and_binary_responses(environment, body, content_type):
    _, _, install, _ = environment
    install(lambda request: httpx.Response(502, content=body, headers={"content-type": content_type}))
    with TestClient(proxy.app) as app:
        result = app.post("/v1/checkin")
    assert result.status_code == 502
    assert result.content == body
    assert result.headers["content-type"] == content_type


@pytest.mark.parametrize("method,path", [
    ("GET", "/v1/checkin-status"),
    ("POST", "/v1/checkin"),
])
def test_401_refresh_success_uses_new_authorization_once(environment, method, path):
    client, _, install, _ = environment
    tokens = []

    def handler(request):
        tokens.append(request.headers["authorization"])
        if tokens[-1] == "Bearer old":
            return httpx.Response(401, json={"code":401,"msg":"expired"})
        return httpx.Response(200, json={"code":0,"msg":"ok","data":{"checked":True}})

    install(handler)
    with TestClient(proxy.app) as app:
        result = app.request(method, path)
    assert result.status_code == 200
    assert result.json()["code"] == 0
    assert tokens == ["Bearer old", "Bearer new"]
    assert client.refresh_calls == 1


def test_second_401_stops_after_single_refresh(environment):
    client, _, install, _ = environment
    seen_tokens = []

    def handler(request):
        seen_tokens.append(request.headers["authorization"])
        return httpx.Response(401, content=b"still forbidden",
                              headers={"content-type": "text/plain"})

    install(handler)
    with TestClient(proxy.app) as app:
        result = app.post("/v1/checkin")

    assert result.status_code == 401
    assert result.content == b"still forbidden"
    assert seen_tokens == ["Bearer old", "Bearer new"]
    assert client.refresh_calls == 1


@pytest.mark.parametrize("failure", [False, RuntimeError("refresh failed"), True])
def test_refresh_failure_or_unchanged_token_returns_original_401(environment, failure):
    client, _, install, _ = environment
    client.refresh_outcome = failure
    if failure is True:
        def unchanged_token():
            client.refresh_calls += 1
            return True
        client.refresh = unchanged_token
    calls = []
    body = b"upstream authentication rejected"

    def handler(request):
        calls.append(request)
        return httpx.Response(401, content=body, headers={"content-type": "text/plain"})

    install(handler)
    with TestClient(proxy.app) as app:
        result = app.get("/v1/checkin-status")
    assert result.status_code == 401
    assert result.content == body
    assert len(calls) == 1
    assert client.refresh_calls == 1


@pytest.mark.parametrize("exc,status,error_type", [
    (httpx.ReadTimeout("slow"), 504, "upstream_timeout"),
    (httpx.ConnectError("unreachable"), 502, "upstream_connection_error"),
])
def test_transport_errors_are_explicit(environment, exc, status, error_type):
    _, _, install, _ = environment

    def handler(request):
        raise exc

    install(handler)
    with TestClient(proxy.app) as app:
        result = app.get("/v1/checkin-status")
    assert result.status_code == status
    assert result.json()["error"]["type"] == error_type


def test_local_auth_failure_remains_401_and_does_not_contact_upstream(environment):
    _, state, install, _ = environment
    state.ensure_auth = lambda: (_ for _ in ()).throw(HTTPException(401, "not authenticated"))
    calls = []
    install(lambda request: calls.append(request) or httpx.Response(200))
    with TestClient(proxy.app) as app:
        response = app.get("/v1/checkin-status")
    assert response.status_code == 401
    assert not calls


def test_concurrent_401_refresh_is_serialized(environment):
    client, _, install, original_async_client = environment
    requests = []

    def handler(request):
        authorization = request.headers["authorization"]
        requests.append(authorization)
        return httpx.Response(
            401 if authorization == "Bearer old" else 200,
            json={"code":401 if authorization == "Bearer old" else 0},
        )

    install(handler)
    original_refresh = client.refresh

    def slow_refresh():
        time.sleep(0.035)
        return original_refresh()

    client.refresh = slow_refresh

    async def run():
        transport = httpx.ASGITransport(app=proxy.app)
        async with original_async_client(transport=transport, base_url="http://testserver") as app:
            return await asyncio.gather(
                app.get("/v1/checkin-status"),
                app.get("/v1/checkin-status"),
                app.post("/v1/checkin"),
            )

    results = asyncio.run(run())
    assert [r.status_code for r in results] == [200, 200, 200]
    assert client.refresh_calls == 1
    assert requests.count("Bearer new") == 3
