"""Account changes through the real proxy and startup boundary (offline)."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import codebuddy_proxy.__main__ as proxy
import codebuddy_proxy.billing_proxy as billing
from codebuddy_proxy.account_manager import AccountManager
from codebuddy_proxy.account_store import AccountStore
from codebuddy_proxy.backend_profile import DOMESTIC_PROFILE, GLOBAL_PROFILE, resolve_backend
from codebuddy_proxy.codebuddy_client_demo import CodeBuddyClient


def make_client(tmp_path, profile, uid, token=None):
    client = CodeBuddyClient(backend=resolve_backend(
        profile=profile, session_file=tmp_path / f"{profile.key}-{uid}.json",
    ))
    client._save_session({
        "auth": {"accessToken": token or f"token-{profile.key}-{uid}"},
        "account": {"uid": uid, "nickname": f"User {uid}"},
        "machineId": "test-machine",
    })
    return client


@pytest.fixture
def managed(tmp_path, monkeypatch):
    store = AccountStore(tmp_path / "accounts")
    domestic = make_client(tmp_path, DOMESTIC_PROFILE, "same-uid")
    global_client = make_client(tmp_path, GLOBAL_PROFILE, "same-uid")
    first = store.import_client(domestic)
    second = store.import_client(global_client)
    manager = AccountManager(store)
    state = proxy.ProxyState(domestic, None, None, account_manager=manager)
    monkeypatch.setattr(proxy, "proxy_state", state)
    yield store, manager, state, first, second
    manager.close()


def test_switch_updates_health_models_and_preserves_captured_request(managed):
    store, manager, state, first, second = managed
    snapshot = state.for_request()
    with TestClient(proxy.app) as app:
        before = app.get("/v1/models").json()
        assert app.get("/health").json()["backend"] == "domestic"
        manager.activate(second)
        assert app.get("/health").json()["backend"] == "global"
        assert app.get("/v1/models").json() != before
        assert snapshot.client.profile.key == "domestic"
        assert state.for_request().client.profile.key == "global"
        assert snapshot.client.session["auth"]["accessToken"] == "token-domestic-same-uid"
    assert AccountStore(store.root).active_account_id == second


@pytest.mark.parametrize("path,body", [
    ("/v1/chat/completions", {"messages": [{"role": "user", "content": "hello"}]}),
    ("/v1/responses", {"input": "hello"}),
    ("/v1/messages", {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 10}),
])
def test_three_protocols_switch_endpoint_and_credentials(managed, monkeypatch, path, body):
    store, manager, state, first, second = managed
    seen = []
    real_client = httpx.AsyncClient

    async def upstream(request):
        seen.append((request.url.host, request.headers["authorization"]))
        data = {"id": "test", "model": "test", "choices": [
            {"index": 0, "delta": {"content": "OK"}, "finish_reason": None},
        ]}
        return httpx.Response(200, content=("data: " + json.dumps(data) + "\n\ndata: [DONE]\n\n").encode(),
                              headers={"content-type": "text/event-stream"})

    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(upstream), **kw,
    ))
    with TestClient(proxy.app) as app:
        assert app.post(path, json={"model": "test", "stream": False, **body}).status_code == 200
        manager.activate(second)
        assert app.post(path, json={"model": "test", "stream": False, **body}).status_code == 200
    assert seen == [("copilot.tencent.com", "Bearer token-domestic-same-uid"),
                    ("www.codebuddy.ai", "Bearer token-global-same-uid")]


def test_billing_refresh_during_switch_stays_with_original_account(managed, monkeypatch):
    store, manager, state, first, second = managed
    old_client = state.client
    seen = []
    refreshed = []
    real_client = httpx.AsyncClient

    def refresh():
        refreshed.append(old_client.profile.key)
        old_client._save_session({**old_client.session, "auth": {"accessToken": "refreshed-domestic"}})
        return True

    monkeypatch.setattr(old_client, "refresh", refresh)

    async def upstream(request):
        seen.append((request.url.host, request.headers["authorization"]))
        if len(seen) == 1:
            manager.activate(second)
            return httpx.Response(401, json={"error": "expired"})
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(billing.httpx, "AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(upstream), **kw,
    ))
    with TestClient(proxy.app) as app:
        assert app.post("/v1/checkin").status_code == 200
    assert refreshed == ["domestic"]
    assert seen == [("copilot.tencent.com", "Bearer token-domestic-same-uid"),
                    ("copilot.tencent.com", "Bearer refreshed-domestic")]
    assert state.client.profile.key == "global"


def test_account_switch_while_streaming_preserves_original_request(managed, monkeypatch):
    store, manager, state, first, second = managed
    seen = []
    real_client = httpx.AsyncClient

    class SwitchingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            chunk = {"id": "stream-test", "model": "test", "choices": [
                {"index": 0, "delta": {"content": "old-account"}, "finish_reason": None},
            ]}
            yield ("data: " + json.dumps(chunk) + "\n\n").encode()
            manager.activate(second)
            yield b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
            yield b"data: [DONE]\n\n"

    async def upstream(request):
        seen.append((request.url.host, request.headers["authorization"]))
        return httpx.Response(200, stream=SwitchingStream(), headers={"content-type": "text/event-stream"})

    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(upstream), **kw,
    ))
    with TestClient(proxy.app) as app:
        body = {"model": "test", "stream": True, "messages": [{"role": "user", "content": "hi"}]}
        response = app.post("/v1/chat/completions", json=body)
        assert response.status_code == 200
        assert "old-account" in response.text
        assert "[DONE]" in response.text
        assert state.client.profile.key == "global"
        assert app.post("/v1/chat/completions", json=body).status_code == 200
    assert seen == [("copilot.tencent.com", "Bearer token-domestic-same-uid"),
                    ("www.codebuddy.ai", "Bearer token-global-same-uid")]


@pytest.mark.parametrize("path,body", [
    ("/v1/chat/completions", {"messages": [{"role": "user", "content": "hello"}]}),
    ("/v1/responses", {"input": "hello"}),
    ("/v1/messages", {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 10}),
])
def test_request_pins_account_before_body_parsing(managed, monkeypatch, path, body):
    store, manager, state, first, second = managed
    original_parser = proxy.parse_request_body
    seen = []
    real_client = httpx.AsyncClient

    async def switch_during_body_read(request):
        manager.activate(second)
        return await original_parser(request)

    async def upstream(request):
        seen.append((request.url.host, request.headers["authorization"]))
        return httpx.Response(200, content=b'data: {"choices":[{"index":0,"delta":{"content":"OK"}}]}\n\ndata: [DONE]\n\n')

    monkeypatch.setattr(proxy, "parse_request_body", switch_during_body_read)
    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(upstream), **kw,
    ))
    with TestClient(proxy.app) as app:
        assert app.post(path, json={"model": "test", "stream": False, **body}).status_code == 200
    assert seen == [("copilot.tencent.com", "Bearer token-domestic-same-uid")]
    assert manager.client.profile.key == "global"


def test_empty_account_starts_and_requests_do_not_open_browser(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CODEBUDDY_ENDPOINT", raising=False)
    args = proxy.parse_args(["--accounts-dir", str(tmp_path / "accounts")])
    manager, fallback = proxy.initialize_account_manager(args, resolve_backend())
    monkeypatch.setattr(proxy, "proxy_state", proxy.ProxyState(fallback, None, None, account_manager=manager))
    monkeypatch.setattr(CodeBuddyClient, "login", lambda *a, **kw: pytest.fail("unexpected browser login"))
    with TestClient(proxy.app) as app:
        assert app.get("/health").status_code == 200
        assert app.get("/health").json()["authenticated"] is False
        assert app.get("/v1/models").status_code == 200
        response = app.post("/v1/chat/completions", json={"model": "test", "messages": []})
        assert response.status_code == 401
        assert "authentication_error" in response.text


def test_legacy_import_once_restore_and_explicit_region(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CODEBUDDY_ENDPOINT", raising=False)
    legacy = CodeBuddyClient(backend=resolve_backend())
    legacy._save_session({"auth": {"accessToken": "legacy"}, "account": {"uid": "old"}})
    original = legacy.session_file.read_bytes()
    args = proxy.parse_args(["--accounts-dir", str(tmp_path / "accounts")])
    manager, client = proxy.initialize_account_manager(args, resolve_backend())
    assert client.session["auth"]["accessToken"] == "legacy"
    client._save_session({**client.session, "auth": {"accessToken": "fresh"}})
    manager.close()
    manager, client = proxy.initialize_account_manager(args, resolve_backend())
    assert client.session["auth"]["accessToken"] == "fresh"
    assert legacy.session_file.read_bytes() == original
    manager.close()
    global_args = proxy.parse_args(["--global", "--accounts-dir", str(tmp_path / "accounts")])
    manager, client = proxy.initialize_account_manager(global_args, resolve_backend(global_mode=True))
    assert manager.client is None
    assert client.profile.key == "global"
    assert len(manager.snapshot()["accounts"]) == 1
    manager.close()


def test_cli_login_preserves_legacy_and_existing_accounts(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CODEBUDDY_ENDPOINT", raising=False)
    legacy = CodeBuddyClient(backend=resolve_backend())
    legacy._save_session({"auth": {"accessToken": "old-token"}, "account": {"uid": "old"}})
    original = legacy.session_file.read_bytes()

    def login(client, **kwargs):
        client._save_session({"auth": {"accessToken": "new-token"}, "account": {"uid": "new"}})

    monkeypatch.setattr(CodeBuddyClient, "login", login)
    args = proxy.parse_args(["--login", "--no-browser", "--accounts-dir", str(tmp_path / "accounts")])
    manager, client = proxy.initialize_account_manager(args, resolve_backend())
    assert client.session["account"]["uid"] == "new"
    assert len(manager.snapshot()["accounts"]) == 2
    assert legacy.session_file.read_bytes() == original
    manager.close()


def test_cli_login_with_no_legacy_session_or_existing_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CODEBUDDY_ENDPOINT", raising=False)

    def login(client, **kwargs):
        client._save_session({"auth": {"accessToken": "first-token"}, "account": {"uid": "first"}})

    monkeypatch.setattr(CodeBuddyClient, "login", login)
    args = proxy.parse_args(["--login", "--accounts-dir", str(tmp_path / "new" / "accounts")])
    manager, client = proxy.initialize_account_manager(args, resolve_backend())
    assert client.session["account"]["uid"] == "first"
    manager.close()


def test_deleted_legacy_account_is_not_reimported_on_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CODEBUDDY_ENDPOINT", raising=False)
    legacy = CodeBuddyClient(backend=resolve_backend())
    legacy._save_session({"auth": {"accessToken": "old-token"}, "account": {"uid": "old"}})
    args = proxy.parse_args(["--accounts-dir", str(tmp_path / "accounts")])
    manager, _ = proxy.initialize_account_manager(args, resolve_backend())
    old_id = manager.store.active_account_id
    other = make_client(tmp_path, GLOBAL_PROFILE, "other")
    new_id = manager.store.import_client(other, activate=True)
    manager.delete(old_id)
    manager.close()
    manager, client = proxy.initialize_account_manager(args, resolve_backend())
    assert manager.store.active_account_id == new_id
    assert len(manager.snapshot()["accounts"]) == 1
    assert client.profile.key == "global"
    manager.close()


def test_explicit_endpoint_ignores_unmatched_default_legacy_session(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CODEBUDDY_ENDPOINT", raising=False)
    legacy = CodeBuddyClient(backend=resolve_backend())
    legacy._save_session({"auth": {"accessToken": "old-token"}, "account": {"uid": "old"}})
    original = legacy.session_file.read_bytes()
    args = proxy.parse_args(["--endpoint", "https://staging.example.test",
                             "--accounts-dir", str(tmp_path / "accounts")])
    manager, fallback = proxy.initialize_account_manager(args, proxy.resolve_backend_from_args(args))
    assert manager.client is None
    assert fallback.endpoint == "https://staging.example.test"
    assert not fallback.session
    assert len(manager.snapshot()["accounts"]) == 1
    assert legacy.session_file.read_bytes() == original
    manager.close()
