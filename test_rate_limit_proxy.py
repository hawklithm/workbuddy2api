"""Offline end-to-end checks for account-aware HTTP 429 handling."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import threading
from email.utils import format_datetime

import httpx
import pytest
from fastapi.testclient import TestClient

import codebuddy_proxy.__main__ as proxy
from codebuddy_proxy.account_manager import AccountManager
from codebuddy_proxy.account_store import AccountStore
from codebuddy_proxy.backend_profile import DOMESTIC_PROFILE, GLOBAL_PROFILE, resolve_backend
from codebuddy_proxy.codebuddy_client_demo import CodeBuddyClient


MODES = [
    ("/v1/chat/completions", {"messages": [{"role": "user", "content": "hello"}]}),
    ("/v1/responses", {"input": "hello"}),
    (
        "/v1/messages",
        {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 16},
    ),
]
MODEL_A = "rate-limit-test"
MODEL_B = "other-model"
_FUTURE_HTTP_DATE = format_datetime(
    datetime.now(timezone.utc) + timedelta(minutes=5), usegmt=True
)
RETRY_AFTER_CASES = [
    pytest.param("17", "17", id="seconds"),
    pytest.param(_FUTURE_HTTP_DATE, _FUTURE_HTTP_DATE, id="http-date"),
    pytest.param(None, None, id="absent"),
]


def _add_account(store, tmp_path, *, uid, token=None, profile=DOMESTIC_PROFILE, endpoint=None):
    source = CodeBuddyClient(
        backend=resolve_backend(
            profile=profile,
            endpoint=endpoint,
            session_file=tmp_path / f"source-{profile.key}-{uid}.json",
            environment={},
        )
    )
    access_token = token or f"token-{profile.key}-{uid}"
    source._save_session(
        {
            "auth": {"accessToken": access_token, "refreshToken": f"refresh-{access_token}"},
            "account": {"uid": uid, "nickname": f"User {uid}"},
            "machineId": "rate-limit-test-machine",
        }
    )
    account_id = store.import_client(source)
    return account_id, store.get_client(account_id)


def _environment(tmp_path, monkeypatch, accounts):
    store = AccountStore(tmp_path / "accounts")
    clients = {}
    account_ids = {}
    for specification in accounts:
        account_id, client = _add_account(store, tmp_path, **specification)
        clients[specification["uid"]] = client
        account_ids[specification["uid"]] = account_id
    manager = AccountManager(store)
    state = proxy.ProxyState(
        store.active_client(), None, None, account_manager=manager
    )
    monkeypatch.setattr(proxy, "proxy_state", state)
    return store, manager, state, clients, account_ids


def _install_transport(monkeypatch, handler):
    real_async_client = httpx.AsyncClient

    def mock_async_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(proxy.httpx, "AsyncClient", mock_async_client)


def _request_body(path, body, stream):
    return {"model": MODEL_A, "stream": stream, **body}


def _request_body_for_model(model, body, stream=False):
    return {"model": model, "stream": stream, **body}


def _model_cooldown(account, model):
    for item in account.get("model_cooldowns", []):
        if isinstance(item, dict) and item.get("model") == model:
            return item
    return None


def _is_model_limited(account, model):
    cooldown = _model_cooldown(account, model)
    return cooldown is not None and cooldown.get("rate_limited") is True


def _success_response(content="recovered"):
    first = {
        "id": "rate-limit-response",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "rate-limit-test",
        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
    }
    final = {
        "id": "rate-limit-response",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "rate-limit-test",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    payload = (
        f"data: {json.dumps(first)}\n\n"
        f"data: {json.dumps(final)}\n\n"
        "data: [DONE]\n\n"
    )
    return httpx.Response(
        200,
        content=payload.encode(),
        headers={"content-type": "text/event-stream"},
    )


def _error_response(uid, *, status=429, retry_after="60", body=None):
    headers = {"content-type": "application/json"}
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    content = body if body is not None else json.dumps({"msg": f"limited-{uid}"}).encode()
    return httpx.Response(status, content=content, headers=headers)


@pytest.mark.parametrize("path,body", MODES)
@pytest.mark.parametrize("stream", [False, True], ids=["non-stream", "stream"])
@pytest.mark.parametrize("retry_after,expected_header", RETRY_AFTER_CASES)
def test_retry_after_is_preserved_for_all_protocol_modes_with_default_off(
    tmp_path, monkeypatch, path, body, stream, retry_after, expected_header
):
    store, manager, state, clients, account_ids = _environment(
        tmp_path,
        monkeypatch,
        [{"uid": "a"}, {"uid": "b"}],
    )
    seen = []
    rate_limit_logs = []
    state.write_log = lambda event, **fields: rate_limit_logs.append((event, fields))

    async def upstream(request):
        seen.append(request.headers["authorization"])
        return _error_response("a", retry_after=retry_after)

    _install_transport(monkeypatch, upstream)
    try:
        assert manager.snapshot()["settings"]["auto_switch_on_429"] is False
        with TestClient(proxy.app, raise_server_exceptions=False) as app:
            response = app.post(path, json=_request_body(path, body, stream))
        assert response.status_code == 429
        assert seen == ["Bearer token-domestic-a"]
        if expected_header is None:
            assert "retry-after" not in response.headers
        else:
            assert response.headers.get("retry-after") == expected_header

        snapshot = manager.snapshot()
        limited = next(account for account in snapshot["accounts"] if account["uid"] == "a")
        assert isinstance(snapshot["server_time"], float)
        cooldown = _model_cooldown(limited, MODEL_A)
        assert cooldown is not None
        assert "cooldown_until" in cooldown
        assert isinstance(cooldown["cooldown_remaining_seconds"], int)
        assert cooldown["cooldown_remaining_seconds"] >= 0
        assert cooldown["last_rate_limited_at"] is not None
        assert cooldown["rate_limited"] is True
        assert cooldown["cooldown_source"] == (
            "retry_after" if retry_after is not None else "default"
        )
        assert "rate_limited" not in limited
        assert snapshot["active_account_id"] == account_ids["a"]
        assert any(
            fields.get("account_id") == account_ids["a"]
            and fields.get("model") == MODEL_A
            for _, fields in rate_limit_logs
        )
        assert clients["a"].session["auth"]["accessToken"] == "token-domestic-a"
        assert clients["b"].session["auth"]["accessToken"] == "token-domestic-b"
        assert len(store.list_accounts()) == 2
    finally:
        manager.close()


@pytest.mark.parametrize("path,body", MODES)
@pytest.mark.parametrize("stream", [False, True], ids=["non-stream", "stream"])
def test_same_region_429_retries_once_on_distinct_uid_for_each_protocol_mode(
    tmp_path, monkeypatch, path, body, stream
):
    store, manager, _, clients, account_ids = _environment(
        tmp_path,
        monkeypatch,
        [{"uid": "a"}, {"uid": "b"}],
    )
    manager.set_settings(auto_switch_on_429=True)
    seen = []

    async def upstream(request):
        authorization = request.headers["authorization"]
        seen.append(authorization)
        if authorization == "Bearer token-domestic-a":
            return _error_response("a", retry_after="120")
        return _success_response("recovered-from-b")

    _install_transport(monkeypatch, upstream)
    try:
        with TestClient(proxy.app, raise_server_exceptions=False) as app:
            response = app.post(path, json=_request_body(path, body, stream))
        assert response.status_code == 200
        assert seen == ["Bearer token-domestic-a", "Bearer token-domestic-b"]
        assert "recovered-from-b" in response.text
        assert manager.store.active_account_id == account_ids["b"]

        snapshot = manager.snapshot()
        by_uid = {account["uid"]: account for account in snapshot["accounts"]}
        assert set(by_uid) == {"a", "b"}
        assert _is_model_limited(by_uid["a"], MODEL_A)
        assert not _is_model_limited(by_uid["b"], MODEL_A)
        assert clients["a"].session["auth"]["accessToken"] == "token-domestic-a"
        assert clients["b"].session["auth"]["accessToken"] == "token-domestic-b"
        assert clients["a"].session_file != clients["b"].session_file
        assert len(store.list_accounts()) == 2
    finally:
        manager.close()


@pytest.mark.parametrize(
    "candidate",
    [
        pytest.param({"uid": "other-region", "profile": GLOBAL_PROFILE}, id="other-region"),
        pytest.param(
            {
                "uid": "other-endpoint",
                "endpoint": "https://domestic-alt.example.test",
            },
            id="other-endpoint",
        ),
    ],
)
def test_429_never_uses_candidate_from_another_region_or_endpoint(
    tmp_path, monkeypatch, candidate
):
    store, manager, _, _, account_ids = _environment(
        tmp_path, monkeypatch, [{"uid": "a"}, candidate]
    )
    manager.set_settings(auto_switch_on_429=True)
    seen = []

    async def upstream(request):
        seen.append((request.url.host, request.headers["authorization"]))
        return _error_response("a", retry_after="60")

    _install_transport(monkeypatch, upstream)
    try:
        with TestClient(proxy.app, raise_server_exceptions=False) as app:
            response = app.post(
                "/v1/chat/completions",
                json={"model": "rate-limit-test", "stream": False,
                      "messages": [{"role": "user", "content": "hello"}]},
            )
        assert response.status_code == 429
        assert seen == [("copilot.tencent.com", "Bearer token-domestic-a")]
        assert manager.store.active_account_id == account_ids["a"]
        limited = manager.snapshot()["accounts"][0]
        assert _is_model_limited(limited, MODEL_A)
    finally:
        manager.close()


def test_all_same_scope_accounts_are_attempted_at_most_once_and_last_429_is_returned(
    tmp_path, monkeypatch
):
    _, manager, _, _, account_ids = _environment(
        tmp_path, monkeypatch, [{"uid": "a"}, {"uid": "b"}, {"uid": "c"}]
    )
    manager.set_settings(auto_switch_on_429=True)
    retry_after_by_token = {
        "Bearer token-domestic-a": "11",
        "Bearer token-domestic-b": "22",
        "Bearer token-domestic-c": "33",
    }
    uid_by_token = {
        "Bearer token-domestic-a": "a",
        "Bearer token-domestic-b": "b",
        "Bearer token-domestic-c": "c",
    }
    seen = []

    async def upstream(request):
        token = request.headers["authorization"]
        seen.append(token)
        uid = uid_by_token[token]
        return _error_response(uid, retry_after=retry_after_by_token[token])

    _install_transport(monkeypatch, upstream)
    try:
        with TestClient(proxy.app, raise_server_exceptions=False) as app:
            response = app.post(
                "/v1/chat/completions",
                json={"model": "rate-limit-test", "stream": False,
                      "messages": [{"role": "user", "content": "hello"}]},
            )
        assert response.status_code == 429
        assert len(seen) == 3
        assert len(set(seen)) == 3
        assert set(seen) == set(retry_after_by_token)
        assert response.headers.get("retry-after") == retry_after_by_token[seen[-1]]
        assert f"limited-{uid_by_token[seen[-1]]}" in response.text
        assert manager.store.active_account_id in account_ids.values()
        assert all(
            _is_model_limited(account, MODEL_A)
            for account in manager.snapshot()["accounts"]
        )
    finally:
        manager.close()


def test_when_all_other_candidates_are_cooling_request_returns_original_429_once(
    tmp_path, monkeypatch
):
    _, manager, _, clients, account_ids = _environment(
        tmp_path, monkeypatch, [{"uid": "a"}, {"uid": "b"}, {"uid": "c"}]
    )
    manager.set_settings(auto_switch_on_429=True)
    all_ids = set(account_ids.values())
    for uid in ("b", "c"):
        manager.handle_rate_limit(
            clients[uid], "300", model=MODEL_A, excluded_account_ids=all_ids
        )
    manager.store.set_active(account_ids["a"])
    seen = []

    async def upstream(request):
        seen.append(request.headers["authorization"])
        return _error_response("a", retry_after="30")

    _install_transport(monkeypatch, upstream)
    try:
        with TestClient(proxy.app, raise_server_exceptions=False) as app:
            response = app.post(
                "/v1/chat/completions",
                json={"model": "rate-limit-test", "stream": False,
                      "messages": [{"role": "user", "content": "hello"}]},
            )
        assert response.status_code == 429
        assert seen == ["Bearer token-domestic-a"]
        assert manager.store.active_account_id == account_ids["a"]
        limited = {a["uid"]: a for a in manager.snapshot()["accounts"]}
        assert all(
            _is_model_limited(limited[uid], MODEL_A) for uid in ("a", "b", "c")
        )
    finally:
        manager.close()


@pytest.mark.parametrize("path,body", MODES)
@pytest.mark.parametrize("stream", [False, True], ids=["non-stream", "stream"])
def test_same_account_model_cooldowns_are_isolated_across_all_protocol_modes(
    tmp_path, monkeypatch, path, body, stream
):
    _, manager, state, _, account_ids = _environment(
        tmp_path, monkeypatch, [{"uid": "a"}, {"uid": "b"}]
    )
    seen = []
    logs = []
    state.write_log = lambda event, **fields: logs.append((event, fields))

    async def upstream(request):
        payload = json.loads(request.content)
        model = payload["model"]
        seen.append((model, request.headers["authorization"]))
        if model == MODEL_A:
            return _error_response("a", retry_after="90")
        return _success_response("model-b-works")

    _install_transport(monkeypatch, upstream)
    manager.set_settings(auto_switch_on_429=False)
    try:
        with TestClient(proxy.app, raise_server_exceptions=False) as app:
            limited = app.post(
                path,
                json=_request_body_for_model(MODEL_A, body, stream),
            )
            recovered = app.post(
                path,
                json=_request_body_for_model(MODEL_B, body, stream),
            )
        assert limited.status_code == 429
        assert recovered.status_code == 200
        assert "model-b-works" in recovered.text
        assert seen == [
            (MODEL_A, "Bearer token-domestic-a"),
            (MODEL_B, "Bearer token-domestic-a"),
        ]
        snapshot = manager.snapshot()
        by_uid = {item["uid"]: item for item in snapshot["accounts"]}
        assert _is_model_limited(by_uid["a"], MODEL_A)
        assert not _is_model_limited(by_uid["a"], MODEL_B)
        assert not by_uid["b"]["model_cooldowns"]
        assert manager.store.active_account_id == account_ids["a"]
        assert any(
            fields.get("account_id") == account_ids["a"]
            and fields.get("model") == MODEL_A
            for _, fields in logs
        )
    finally:
        manager.close()


@pytest.mark.parametrize("candidate_cooling_model", [MODEL_A, MODEL_B])
def test_candidate_is_excluded_only_when_requested_model_is_cooling(
    tmp_path, monkeypatch, candidate_cooling_model
):
    _, manager, _, clients, account_ids = _environment(
        tmp_path, monkeypatch, [{"uid": "a"}, {"uid": "b"}, {"uid": "c"}]
    )
    manager.set_settings(auto_switch_on_429=True)
    manager.handle_rate_limit(
        clients["b"], "300", model=candidate_cooling_model, allow_switch=False
    )
    seen = []

    if candidate_cooling_model == MODEL_A:
        requested_model = MODEL_A
        expected_retry_uid = "c"
    else:
        requested_model = MODEL_B
        # A is cooled for MODEL_A only and must remain eligible for MODEL_B.
        manager.handle_rate_limit(clients["a"], "300", model=MODEL_A, allow_switch=False)
        manager.store.set_active(account_ids["b"])
        expected_retry_uid = "a"

    async def upstream(request):
        token = request.headers["authorization"]
        seen.append(token)
        if token == f"Bearer token-domestic-{expected_retry_uid}":
            return _success_response("eligible-model-candidate")
        return _error_response(token.rsplit("-", 1)[-1], retry_after="30")

    _install_transport(monkeypatch, upstream)
    try:
        body = {
            "model": requested_model,
            "stream": False,
            "messages": [{"role": "user", "content": "model candidate"}],
        }
        with TestClient(proxy.app, raise_server_exceptions=False) as app:
            result = app.post("/v1/chat/completions", json=body)
        starting_uid = "a" if candidate_cooling_model == MODEL_A else "b"
        assert result.status_code == 200
        assert "eligible-model-candidate" in result.text
        assert seen == [
            f"Bearer token-domestic-{starting_uid}",
            f"Bearer token-domestic-{expected_retry_uid}",
        ]
        assert manager.store.active_account_id == account_ids[expected_retry_uid]
    finally:
        manager.close()


def test_model_none_does_not_record_cooldown_or_switch(tmp_path, monkeypatch):
    _, manager, _, clients, account_ids = _environment(
        tmp_path, monkeypatch, [{"uid": "a"}, {"uid": "b"}]
    )
    manager.set_settings(auto_switch_on_429=True)
    try:
        result = manager.handle_rate_limit(clients["a"], "60", model=None)
        assert result is None
        assert manager.store.active_account_id == account_ids["a"]
        assert all(
            not account["model_cooldowns"] for account in manager.snapshot()["accounts"]
        )
    finally:
        manager.close()


def test_delayed_old_account_429_does_not_replace_manual_active_account(
    tmp_path, monkeypatch
):
    _, manager, _, _, account_ids = _environment(
        tmp_path,
        monkeypatch,
        [
            {"uid": "a"},
            {"uid": "b"},
            {"uid": "manual-global", "profile": GLOBAL_PROFILE},
        ],
    )
    manager.set_settings(auto_switch_on_429=True)
    seen = []

    async def upstream(request):
        authorization = request.headers["authorization"]
        seen.append(authorization)
        if authorization == "Bearer token-domestic-a":
            # The request has already pinned A. A human changes the active
            # account while A's upstream response is being processed.
            manager.activate(account_ids["manual-global"])
            return _error_response("a", retry_after="90")
        if authorization == "Bearer token-domestic-b":
            return _success_response("retried-on-b")
        pytest.fail(f"ineligible account was used: {authorization}")

    _install_transport(monkeypatch, upstream)
    try:
        with TestClient(proxy.app, raise_server_exceptions=False) as app:
            response = app.post(
                "/v1/chat/completions",
                json={"model": "rate-limit-test", "stream": False,
                      "messages": [{"role": "user", "content": "hello"}]},
            )
        assert response.status_code == 429
        assert "limited-a" in response.text
        assert seen == ["Bearer token-domestic-a"]
        assert manager.store.active_account_id == account_ids["manual-global"]
        states = {account["uid"]: account for account in manager.snapshot()["accounts"]}
        assert _is_model_limited(states["a"], MODEL_A)
        assert not _is_model_limited(states["b"], MODEL_A)
        assert not _is_model_limited(states["manual-global"], MODEL_A)
    finally:
        manager.close()


def test_billing_429_preserves_status_body_and_retry_after_without_replay(
    tmp_path, monkeypatch
):
    _, manager, _, _, account_ids = _environment(
        tmp_path, monkeypatch, [{"uid": "a"}, {"uid": "b"}]
    )
    manager.set_settings(auto_switch_on_429=True)
    body = b'{"billing":"slow down","request":"single-checkin"}\n'
    seen = []

    async def upstream(request):
        seen.append((request.method, request.url.host, request.headers["authorization"]))
        return httpx.Response(
            429,
            content=body,
            headers={"content-type": "application/problem+json", "Retry-After": "45"},
        )

    _install_transport(monkeypatch, upstream)
    try:
        with TestClient(proxy.app, raise_server_exceptions=False) as app:
            response = app.post("/v1/checkin")
        assert response.status_code == 429
        assert response.content == body
        assert response.headers.get("retry-after") == "45"
        assert response.headers.get("content-type") == "application/problem+json"
        assert seen == [
            ("POST", "copilot.tencent.com", "Bearer token-domestic-a")
        ]
        assert manager.store.active_account_id == account_ids["a"]
        limited = {a["uid"]: a for a in manager.snapshot()["accounts"]}
        assert limited["a"]["model_cooldowns"] == []
        assert limited["b"]["model_cooldowns"] == []
    finally:
        manager.close()


@pytest.mark.parametrize("status", [401, 503])
def test_non_429_statuses_never_rotate_accounts(tmp_path, monkeypatch, status):
    _, manager, _, _, account_ids = _environment(
        tmp_path, monkeypatch, [{"uid": "a"}, {"uid": "b"}]
    )
    manager.set_settings(auto_switch_on_429=True)
    seen = []

    async def upstream(request):
        seen.append(request.headers["authorization"])
        return httpx.Response(status, json={"msg": f"status-{status}"})

    _install_transport(monkeypatch, upstream)
    try:
        with TestClient(proxy.app, raise_server_exceptions=False) as app:
            response = app.post(
                "/v1/chat/completions",
                json={"model": "rate-limit-test", "stream": False,
                      "messages": [{"role": "user", "content": "hello"}]},
            )
        assert response.status_code == status
        assert seen == ["Bearer token-domestic-a"]
        assert manager.store.active_account_id == account_ids["a"]
        assert all(not account["model_cooldowns"] for account in manager.snapshot()["accounts"])
    finally:
        manager.close()


def test_started_sse_is_never_replayed_after_upstream_stream_failure(
    tmp_path, monkeypatch
):
    _, manager, _, _, account_ids = _environment(
        tmp_path, monkeypatch, [{"uid": "a"}, {"uid": "b"}]
    )
    manager.set_settings(auto_switch_on_429=True)
    seen = []

    class BrokenAfterFirstChunk(httpx.AsyncByteStream):
        async def __aiter__(self):
            chunk = {
                "id": "partial-stream",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "rate-limit-test",
                "choices": [
                    {"index": 0, "delta": {"content": "stream-started"},
                     "finish_reason": None}
                ],
            }
            yield f"data: {json.dumps(chunk)}\n\n".encode()
            raise httpx.ReadError("injected failure after first SSE chunk")

        async def aclose(self):
            return None

    async def upstream(request):
        seen.append(request.headers["authorization"])
        return httpx.Response(
            200,
            stream=BrokenAfterFirstChunk(),
            headers={"content-type": "text/event-stream"},
        )

    _install_transport(monkeypatch, upstream)
    try:
        with TestClient(proxy.app, raise_server_exceptions=False) as app:
            response = app.post(
                "/v1/chat/completions",
                json={"model": "rate-limit-test", "stream": True,
                      "messages": [{"role": "user", "content": "hello"}]},
            )
        assert response.status_code == 200
        assert "stream-started" in response.text
        assert seen == ["Bearer token-domestic-a"]
        assert manager.store.active_account_id == account_ids["a"]
        assert all(
            not account["model_cooldowns"]
            for account in manager.snapshot()["accounts"]
        )
    finally:
        manager.close()


def test_concurrent_requests_keep_their_body_and_account_snapshot_isolated(
    tmp_path, monkeypatch
):
    _, manager, _, _, account_ids = _environment(
        tmp_path, monkeypatch, [{"uid": "a"}, {"uid": "b"}, {"uid": "c"}]
    )
    manager.set_settings(auto_switch_on_429=True)
    first_a_started = threading.Event()
    release_first_a = threading.Event()
    seen = []

    async def upstream(request):
        authorization = request.headers["authorization"]
        payload = json.loads(request.content)
        prompt = payload["messages"][-1]["content"]
        seen.append((prompt, authorization))
        if prompt == "request-one" and authorization == "Bearer token-domestic-a":
            first_a_started.set()
            await asyncio.to_thread(release_first_a.wait, 5)
            return _error_response("a", retry_after="90")
        return _success_response(f"answer-{prompt}")

    _install_transport(monkeypatch, upstream)
    body_one = {
        "model": "rate-limit-test", "stream": False,
        "messages": [{"role": "user", "content": "request-one"}],
    }
    body_two = {
        "model": "rate-limit-test", "stream": False,
        "messages": [{"role": "user", "content": "request-two"}],
    }
    try:
        with TestClient(proxy.app, raise_server_exceptions=False) as app:
            with ThreadPoolExecutor(max_workers=1) as pool:
                first_future = pool.submit(app.post, "/v1/chat/completions", json=body_one)
                try:
                    assert first_a_started.wait(5)
                    manager.activate(account_ids["b"])
                    second_response = app.post("/v1/chat/completions", json=body_two)
                finally:
                    release_first_a.set()
                first_response = first_future.result(timeout=10)

        assert second_response.status_code == 200
        assert "answer-request-two" in second_response.text
        assert first_response.status_code == 429
        assert "limited-a" in first_response.text
        first_tokens = [token for prompt, token in seen if prompt == "request-one"]
        second_tokens = [token for prompt, token in seen if prompt == "request-two"]
        assert first_tokens[0] == "Bearer token-domestic-a"
        assert first_tokens == ["Bearer token-domestic-a"]
        assert second_tokens == ["Bearer token-domestic-b"]
        assert manager.store.active_account_id == account_ids["b"]
    finally:
        release_first_a.set()
        manager.close()


def test_concurrent_model_429s_keep_cooldowns_and_logs_isolated(
    tmp_path, monkeypatch
):
    _, manager, state, _, account_ids = _environment(
        tmp_path, monkeypatch, [{"uid": "a"}, {"uid": "b"}]
    )
    manager.set_settings(auto_switch_on_429=False)
    entered = {MODEL_A: threading.Event(), MODEL_B: threading.Event()}
    both_entered = threading.Event()
    release = threading.Event()
    seen = []
    logs = []
    seen_lock = threading.Lock()
    logs_lock = threading.Lock()

    def record_log(event, **fields):
        if event == "account_rate_limited":
            with logs_lock:
                logs.append(fields)

    state.write_log = record_log

    async def upstream(request):
        model = json.loads(request.content)["model"]
        with seen_lock:
            seen.append((model, request.headers["authorization"]))
            entered[model].set()
            if all(event.is_set() for event in entered.values()):
                both_entered.set()
        await asyncio.to_thread(release.wait, 5)
        retry_after = "19" if model == MODEL_A else "73"
        return _error_response("a", retry_after=retry_after)

    _install_transport(monkeypatch, upstream)
    body_a = {
        "model": MODEL_A,
        "stream": False,
        "messages": [{"role": "user", "content": "parallel model A"}],
    }
    body_b = {
        "model": MODEL_B,
        "stream": False,
        "messages": [{"role": "user", "content": "parallel model B"}],
    }
    try:
        with TestClient(proxy.app, raise_server_exceptions=False) as app:
            with ThreadPoolExecutor(max_workers=2) as pool:
                future_a = pool.submit(app.post, "/v1/chat/completions", json=body_a)
                future_b = pool.submit(app.post, "/v1/chat/completions", json=body_b)
                assert both_entered.wait(5), "both model requests must reach upstream before release"
                release.set()
                response_a = future_a.result(timeout=10)
                response_b = future_b.result(timeout=10)

        assert response_a.status_code == response_b.status_code == 429
        assert response_a.headers.get("retry-after") == "19"
        assert response_b.headers.get("retry-after") == "73"
        assert len(seen) == 2
        assert set(seen) == {
            (MODEL_A, "Bearer token-domestic-a"),
            (MODEL_B, "Bearer token-domestic-a"),
        }
        assert manager.store.active_account_id == account_ids["a"]

        snapshot = manager.snapshot()
        by_uid = {account["uid"]: account for account in snapshot["accounts"]}
        cooldowns = {
            entry["model"]: entry
            for entry in by_uid["a"]["model_cooldowns"]
        }
        assert set(cooldowns) == {MODEL_A, MODEL_B}
        assert cooldowns[MODEL_A]["rate_limited"] is True
        assert cooldowns[MODEL_B]["rate_limited"] is True
        assert cooldowns[MODEL_A]["cooldown_source"] == "retry_after"
        assert cooldowns[MODEL_B]["cooldown_source"] == "retry_after"
        # The requests are released together; the deadline gap should reflect
        # the 54-second difference between their independent Retry-After values.
        assert 40 <= cooldowns[MODEL_B]["cooldown_until"] - cooldowns[MODEL_A]["cooldown_until"] <= 68
        assert not by_uid["b"]["model_cooldowns"]
        assert len(logs) == 2
        assert {
            (entry.get("account_id"), entry.get("model")) for entry in logs
        } == {
            (account_ids["a"], MODEL_A),
            (account_ids["a"], MODEL_B),
        }
    finally:
        release.set()
        manager.close()
