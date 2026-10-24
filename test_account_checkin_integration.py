"""Exercise per-account check-in and automatic triggers through the real app."""
import asyncio
import json
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

import codebuddy_proxy.__main__ as proxy
from codebuddy_proxy.account_manager import AccountManager
from codebuddy_proxy.account_store import AccountStore
from test_account_integration import make_client
from codebuddy_proxy.backend_profile import DOMESTIC_PROFILE, GLOBAL_PROFILE

ADMIN_HEADERS = {"X-Account-Manager": "1"}


@pytest.fixture
def checkin_environment(tmp_path, monkeypatch):
    store = AccountStore(tmp_path / "accounts")
    ids = {}
    for profile, uid in [(DOMESTIC_PROFILE, "first"), (GLOBAL_PROFILE, "second")]:
        source = make_client(tmp_path, profile, uid, token="PRIVATE-" + uid)
        ids[uid] = store.import_client(source)
    manager = AccountManager(store)
    state = proxy.ProxyState(manager.client, None, None, account_manager=manager)
    monkeypatch.setattr(proxy, "proxy_state", state)
    real_async_client = httpx.AsyncClient
    seen = []
    signed = set()

    async def handler(request):
        uid = request.headers["X-User-Id"]
        seen.append((uid, request.url.host, request.url.path, request.method, request.content))
        assert request.content == b"{}"
        if request.url.path == "/v2/billing/meter/checkin-activity-status":
            return httpx.Response(200, json={
                "code": 0, "msg": "OK",
                "data": {"active": True, "today_checked_in": uid in signed},
            })
        assert request.url.path.endswith("/daily-checkin")
        signed.add(uid)
        return httpx.Response(200, json={"code": 0, "msg": "OK", "data": {}})

    def install(custom_handler=None):
        monkeypatch.setattr(
            httpx, "AsyncClient",
            lambda **kw: real_async_client(
                transport=httpx.MockTransport(custom_handler or handler), **kw,
            ),
        )
    install()
    yield manager, store, ids, seen, signed, install, handler
    manager.close()


def app_client():
    return TestClient(proxy.app, base_url="http://127.0.0.1", client=("127.0.0.1", 50500))


def test_checkin_of_inactive_account_preserves_active_and_uses_its_own_credentials(
    checkin_environment, capsys
):
    manager, store, ids, seen, signed, _, _ = checkin_environment
    active = store.active_account_id
    with app_client() as app:
        response = app.get(f"/admin/api/accounts/{ids['second']}/checkin-status")
        assert response.status_code == 200
        assert response.json()["checked"] is False
        response = app.post(f"/admin/api/accounts/{ids['second']}/checkin", headers=ADMIN_HEADERS)
        assert response.status_code == 200
        assert response.json()["status"] == "checked"
        assert response.json()["checked"] is True
        row = next(row for row in app.get("/admin/api/accounts").json()["accounts"]
                   if row["id"] == ids["second"])
        assert row["checkin"]["checked"] is True
        assert store.active_account_id == active
    assert signed == {"second"}
    assert {uid for uid, *_ in seen} == {"second"}
    assert {host for _, host, *_ in seen} == {"www.codebuddy.ai"}
    assert sum(path.endswith("/daily-checkin") for _, _, path, *_ in seen) == 1
    output = capsys.readouterr().out
    assert "[Checkin]" in output
    assert "PRIVATE-" not in output


@pytest.mark.parametrize("uid", ["first", "second"], ids=["domestic", "global"])
@pytest.mark.parametrize("entrypoint", ["admin", "v1-get", "v1-post"])
def test_current_activity_status_reports_already_signed_without_daily_post(
    checkin_environment, uid, entrypoint
):
    """Legacy status may say inactive while the current activity is signed."""
    manager, store, ids, seen, _, install, _ = checkin_environment
    if entrypoint != "admin":
        manager.activate(ids[uid])
    active = store.active_account_id
    current_payload = {
        "code": 0,
        "msg": "OK",
        "data": {
            "active": True,
            "today_checked_in": True,
            "streak_days": 1,
            "start_time": "2026-09-30 00:00:00",
            "end_time": "2026-12-31 23:59:59",
            "season": 10,
        },
    }

    async def activity_status(request):
        seen.append((request.headers["X-User-Id"], request.url.host,
                     request.url.path, request.method, request.content))
        assert request.method == "POST"
        assert request.content == b"{}"
        if request.url.path == "/v2/billing/meter/checkin-status":
            return httpx.Response(200, json={
                "code": 0, "msg": "OK",
                "data": {"active": False, "today_checked_in": False,
                         "start_time": "", "end_time": "", "season": 0},
            })
        assert request.url.path == "/v2/billing/meter/checkin-activity-status"
        return httpx.Response(200, json=current_payload)

    install(activity_status)
    with app_client() as app:
        if entrypoint == "admin":
            result = app.get(f"/admin/api/accounts/{ids[uid]}/checkin-status")
            assert result.status_code == 200
            assert result.json()["status"] == "checked"
            assert result.json()["checked"] is True
            assert result.json()["error"] is None
            row = next(a for a in app.get("/admin/api/accounts").json()["accounts"]
                       if a["id"] == ids[uid])
            assert row["checkin"]["status"] == "checked"
            # A manual sign request must also recognize today's existing sign.
            result = app.post(f"/admin/api/accounts/{ids[uid]}/checkin",
                              headers=ADMIN_HEADERS)
            assert result.status_code == 200
            assert result.json()["status"] == "checked"
        else:
            method = "GET" if entrypoint == "v1-get" else "POST"
            result = app.request(method, "/v1/checkin-status")
            assert result.status_code == 200
            assert result.json() == current_payload
        assert store.active_account_id == active

    assert seen
    assert {user for user, *_ in seen} == {uid}
    expected_host = "copilot.tencent.com" if uid == "first" else "www.codebuddy.ai"
    assert {host for _, host, *_ in seen} == {expected_host}
    assert {path for _, _, path, *_ in seen} == {
        "/v2/billing/meter/checkin-activity-status"
    }


def test_signed_account_is_not_signed_again(checkin_environment):
    _, _, ids, seen, signed, _, _ = checkin_environment
    signed.add("first")
    with app_client() as app:
        response = app.post(f"/admin/api/accounts/{ids['first']}/checkin", headers=ADMIN_HEADERS)
        assert response.status_code == 200
        assert response.json()["checked"] is True
    assert not any(path.endswith("/daily-checkin") for _, _, path, *_ in seen)


@pytest.mark.parametrize("startup", [False, True], ids=["enable-immediately", "startup-enabled"])
def test_automatic_trigger_checks_all_accounts_including_inactive(
    checkin_environment, startup
):
    manager, store, ids, seen, signed, _, _ = checkin_environment
    active = store.active_account_id
    if startup:
        manager.set_settings(auto_checkin_enabled=True)
    with app_client() as app:
        if not startup:
            response = app.patch("/admin/api/settings", headers=ADMIN_HEADERS,
                                 json={"auto_checkin_enabled": True})
            assert response.status_code == 200
            assert response.json()["auto_checkin_enabled"] is True
            assert response.json()["auto_switch_on_429"] is False
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and signed != {"first", "second"}:
            time.sleep(0.01)
        assert signed == {"first", "second"}
        assert store.active_account_id == active
    assert sum(path.endswith("/daily-checkin") for _, _, path, *_ in seen) == 2


def test_auto_switch_429_does_not_change_active_for_per_account_checkin(checkin_environment, tmp_path):
    manager, store, ids, seen, _, install, _ = checkin_environment
    active = store.active_account_id
    store.import_client(make_client(tmp_path, DOMESTIC_PROFILE, "backup"))
    manager.set_settings(auto_switch_on_429=True)

    async def limited(request):
        seen.append((request.headers["X-User-Id"], request.url.host,
                     request.url.path, request.method, request.content))
        return httpx.Response(429, json={"code": 429}, headers={"Retry-After": "19"})

    install(limited)
    with app_client() as app:
        response = app.get(f"/admin/api/accounts/{ids['first']}/checkin-status")
        assert response.status_code == 200
        assert response.json()["checked"] is None
        assert response.json()["status"] == "error"
        assert store.active_account_id == active
        first = next(row for row in manager.snapshot()["accounts"] if row["id"] == active)
        assert first["model_cooldowns"] == []
    assert len(seen) == 1


def test_disabling_while_status_is_pending_prevents_auto_sign(checkin_environment):
    manager, _, _, seen, signed, install, original = checkin_environment
    status_entered = threading.Event()
    release_status = threading.Event()

    async def pending(request):
        if request.url.path == "/v2/billing/meter/checkin-activity-status":
            status_entered.set()
            while not release_status.is_set():
                await asyncio.sleep(0.01)
        return await original(request)

    install(pending)
    try:
        with app_client() as app:
            app.patch("/admin/api/settings", headers=ADMIN_HEADERS,
                      json={"auto_checkin_enabled": True})
            assert status_entered.wait(3)
            response = app.patch("/admin/api/settings", headers=ADMIN_HEADERS,
                                 json={"auto_checkin_enabled": False})
            assert response.status_code == 200
            release_status.set()
            time.sleep(0.1)
            assert not signed
    finally:
        release_status.set()
    assert not any(path.endswith("/daily-checkin") for _, _, path, *_ in seen)
