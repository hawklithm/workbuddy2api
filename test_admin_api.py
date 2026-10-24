from fastapi import FastAPI
from fastapi.testclient import TestClient

from codebuddy_proxy import admin_api
from codebuddy_proxy.admin_api import create_admin_router


class FakeManager:
    def __init__(self):
        self.login_calls = []
        self.activated = []
        self.deleted = []
        self.settings = {
            "auto_switch_on_429": False,
            "default_cooldown_seconds": 60,
            "auto_checkin_enabled": False,
        }
        self.checkins = FakeCheckins()

    def has_account(self, account_id):
        return account_id == "account-1"

    def snapshot(self):
        return {
            "accounts": [
                {
                    "id": "account-1",
                    "backend": "domestic",
                    "endpoint": "https://copilot.tencent.com",
                    "uid": "user-1",
                    "nickname": "Local user",
                    "active": True,
                    "model_cooldowns": [
                        {
                            "model": "model-a",
                            "rate_limited": True,
                            "cooldown_until": 1234.5,
                            "cooldown_remaining_seconds": 7,
                            "last_rate_limited_at": 1227.5,
                            "cooldown_source": "retry_after",
                            "session": {"accessToken": "private-token-value"},
                        },
                        {
                            "model": "bad\nmodel",
                            "rate_limited": True,
                            "cooldown_source": "default",
                        },
                        {
                            "model": "x" * 257,
                            "rate_limited": True,
                            "cooldown_source": "default",
                        },
                    ],
                    # Previous account-wide cooldown fields must not leak into
                    # the public response even if a stale caller provides them.
                    "rate_limited": True,
                    "cooldown_until": 9999.0,
                    "checkin": {
                        "date": "2026-10-24",
                        "status": "checked",
                        "checked": True,
                        "last_checked_at": 1220,
                        "last_checkin_at": 1210,
                        "next_checkin_at": 1729766400,
                        "error": None,
                        "session": {"accessToken": "private-token-value"},
                    },
                    "session": {"auth": {"accessToken": "private-token-value"}},
                }
            ],
            "active_account_id": "account-1",
            "settings": self.settings,
            "server_time": 1227.5,
            "raw_session": "private-token-value",
        }

    def get_settings(self):
        return dict(self.settings)

    def set_settings(self, auto_switch_on_429=None, *, auto_checkin_enabled=None):
        if auto_switch_on_429 is not None:
            self.settings["auto_switch_on_429"] = auto_switch_on_429
        if auto_checkin_enabled is not None:
            self.settings["auto_checkin_enabled"] = auto_checkin_enabled
        return dict(self.settings)

    def activate(self, account_id):
        self.activated.append(account_id)

    def delete(self, account_id):
        self.deleted.append(account_id)

    def start_login(self, backend):
        self.login_calls.append(backend)
        return {
            "id": "job-1",
            "backend": backend,
            "status": "pending",
            "auth_url": "https://login.example.test/auth",
            "error": None,
            "account_id": None,
            "state": "private-state",
            "session": {"auth": {"accessToken": "private-token-value"}},
        }

    def get_login(self, job_id):
        return {
            "id": job_id,
            "backend": "global",
            "status": "failed",
            "auth_url": None,
            "error": "登录失败，请重试",
            "account_id": None,
            "exception": "private-token-value",
        }

    def cancel_login(self, job_id):
        return {"id": job_id, "backend": "global", "status": "cancelled"}


class FakeCheckins:
    def __init__(self):
        self.calls = []
        self.status_result = {
            "date": "2026-10-24",
            "status": "unchecked",
            "checked": False,
            "last_checked_at": 1220.0,
            "last_checkin_at": None,
            "next_checkin_at": 1729766400.0,
            "error": None,
            "session": {"accessToken": "private-token-value"},
        }
        self.checkin_result = {
            "date": "2026-10-24",
            "status": "checked",
            "checked": True,
            "last_checked_at": 1230.0,
            "last_checkin_at": 1230.0,
            "next_checkin_at": 1729766400.0,
            "error": None,
            "raw_response": "private-token-value",
        }

    async def check_status(self, account_id, *, reason):
        self.calls.append(("status", account_id, reason))
        return self.status_result

    async def check_in(self, account_id, *, reason):
        self.calls.append(("checkin", account_id, reason))
        return self.checkin_result


def _app(manager):
    app = FastAPI()
    app.include_router(create_admin_router(lambda: manager))
    return app


def _client(manager):
    return TestClient(
        _app(manager),
        base_url="http://localhost:8123",
        client=("127.0.0.1", 50000),
    )


def test_account_and_job_responses_are_sanitized_and_uncached():
    manager = FakeManager()
    with _client(manager) as client:
        accounts = client.get("/admin/api/accounts")
        assert accounts.status_code == 200
        assert accounts.headers["cache-control"] == "no-store"
        assert accounts.json() == {
            "accounts": [
                {
                    "id": "account-1",
                    "backend": "domestic",
                    "endpoint": "https://copilot.tencent.com",
                    "uid": "user-1",
                    "nickname": "Local user",
                    "active": True,
                    "model_cooldowns": [
                        {
                            "model": "model-a",
                            "rate_limited": True,
                            "cooldown_until": 1234.5,
                            "cooldown_remaining_seconds": 7,
                            "last_rate_limited_at": 1227.5,
                            "cooldown_source": "retry_after",
                        }
                    ],
                    "checkin": {
                        "date": "2026-10-24",
                        "status": "checked",
                        "checked": True,
                        "last_checked_at": 1220.0,
                        "last_checkin_at": 1210.0,
                        "error": None,
                        "next_checkin_at": 1729766400.0,
                    },
                }
            ],
            "active_account_id": "account-1",
                "settings": {
                    "auto_switch_on_429": False,
                    "auto_checkin_enabled": False,
                    "default_cooldown_seconds": 60,
                },
                "server_time": 1227.5,
        }

        job = client.get("/admin/api/logins/job-1")
        assert job.status_code == 200
        assert job.headers["cache-control"] == "no-store"
        assert "private-token-value" not in job.text
        assert "exception" not in job.json()


def test_write_routes_require_header_and_same_origin_with_port_check():
    manager = FakeManager()
    with _client(manager) as client:
        denied = client.post("/admin/api/logins", json={"backend": "global"})
        assert denied.status_code == 403
        assert manager.login_calls == []

        cross_port = client.post(
            "/admin/api/logins",
            json={"backend": "global"},
            headers={
                "X-Account-Manager": "1",
                "Origin": "http://localhost:8124",
            },
        )
        assert cross_port.status_code == 403
        assert manager.login_calls == []

        accepted = client.post(
            "/admin/api/logins",
            json={"backend": "global"},
            headers={
                "X-Account-Manager": "1",
                "Origin": "http://localhost:8123",
            },
        )
        assert accepted.status_code == 202
        assert accepted.headers["cache-control"] == "no-store"
        assert accepted.json() == {
            "id": "job-1",
            "backend": "global",
            "status": "pending",
            "auth_url": "https://login.example.test/auth",
            "error": None,
            "account_id": None,
        }
        assert manager.login_calls == ["global"]

        originless = client.post(
            "/admin/api/accounts/account-1/activate",
            headers={"X-Account-Manager": "1"},
        )
        assert originless.status_code == 200
        assert manager.activated == ["account-1"]


def test_settings_read_write_is_protected_strict_and_no_store():
    manager = FakeManager()
    with _client(manager) as client:
        read = client.get("/admin/api/settings")
        assert read.status_code == 200
        assert read.headers["cache-control"] == "no-store"
        assert read.json() == {
            "auto_switch_on_429": False,
            "auto_checkin_enabled": False,
            "default_cooldown_seconds": 60,
        }

        denied = client.patch(
            "/admin/api/settings",
            json={"auto_switch_on_429": True},
        )
        assert denied.status_code == 403
        assert manager.settings["auto_switch_on_429"] is False

        cross_port = client.patch(
            "/admin/api/settings",
            json={"auto_switch_on_429": True},
            headers={
                "X-Account-Manager": "1",
                "Origin": "http://localhost:8124",
            },
        )
        assert cross_port.status_code == 403
        assert manager.settings["auto_switch_on_429"] is False

        invalid_payloads = (
            {"auto_switch_on_429": 1},
            {"auto_switch_on_429": True, "unknown": "value"},
            {},
            {"auto_checkin_enabled": "true"},
        )
        for payload in invalid_payloads:
            response = client.patch(
                "/admin/api/settings",
                json=payload,
                headers={"X-Account-Manager": "1"},
            )
            assert response.status_code == 400
        duplicate_key = client.patch(
            "/admin/api/settings",
            content='{"auto_switch_on_429":false,"auto_switch_on_429":true}',
            headers={"X-Account-Manager": "1", "Content-Type": "application/json"},
        )
        assert duplicate_key.status_code == 400
        assert manager.settings["auto_switch_on_429"] is False

        updated = client.patch(
            "/admin/api/settings",
            json={"auto_switch_on_429": True},
            headers={"X-Account-Manager": "1"},
        )
        assert updated.status_code == 200
        assert updated.headers["cache-control"] == "no-store"
        assert updated.json() == {
            "auto_switch_on_429": True,
            "auto_checkin_enabled": False,
            "default_cooldown_seconds": 60,
        }
        assert client.get("/admin/api/settings").json()["auto_switch_on_429"] is True

        checkin_enabled = client.patch(
            "/admin/api/settings",
            json={"auto_checkin_enabled": True},
            headers={"X-Account-Manager": "1"},
        )
        assert checkin_enabled.status_code == 200
        assert checkin_enabled.json() == {
            "auto_switch_on_429": True,
            "auto_checkin_enabled": True,
            "default_cooldown_seconds": 60,
        }


def test_checkin_routes_keep_shape_sanitize_errors_and_never_change_active():
    manager = FakeManager()
    with _client(manager) as client:
        status = client.get("/admin/api/accounts/account-1/checkin-status")
        assert status.status_code == 200
        assert status.headers["cache-control"] == "no-store"
        assert status.json() == {
            "date": "2026-10-24",
            "status": "unchecked",
            "checked": False,
            "last_checked_at": 1220.0,
            "last_checkin_at": None,
            "error": None,
            "next_checkin_at": 1729766400.0,
        }
        assert manager.checkins.calls == [("status", "account-1", "manual_status")]

        denied = client.post("/admin/api/accounts/account-1/checkin")
        assert denied.status_code == 403
        assert len(manager.checkins.calls) == 1

        cross_port = client.post(
            "/admin/api/accounts/account-1/checkin",
            headers={
                "X-Account-Manager": "1",
                "Origin": "http://localhost:8124",
            },
        )
        assert cross_port.status_code == 403
        assert len(manager.checkins.calls) == 1

        checked = client.post(
            "/admin/api/accounts/account-1/checkin",
            headers={"X-Account-Manager": "1", "Origin": "http://localhost:8123"},
        )
        assert checked.status_code == 200
        assert checked.json()["status"] == "checked"
        assert checked.json()["checked"] is True
        assert "private-token-value" not in checked.text
        assert manager.checkins.calls[-1] == ("checkin", "account-1", "manual")
        assert manager.snapshot()["active_account_id"] == "account-1"

        manager.checkins.status_result = {
            "date": "2026-10-24",
            "status": "error",
            "error": "upstream token private-token-value",
            "exception": "secret",
        }
        sanitized = client.get("/admin/api/accounts/account-1/checkin-status")
        assert sanitized.status_code == 200
        assert sanitized.json()["error"] == "签到状态查询失败"
        assert "private-token-value" not in sanitized.text

        unknown = client.get("/admin/api/accounts/missing/checkin-status")
        assert unknown.status_code == 404
        assert client.post(
            "/admin/api/accounts/missing/checkin",
            headers={"X-Account-Manager": "1"},
        ).status_code == 404


def test_cross_site_origin_host_and_non_loopback_peer_are_rejected():
    manager = FakeManager()
    with _client(manager) as client:
        cross_site = client.get(
            "/admin/api/accounts",
            headers={"Origin": "http://evil.example"},
        )
        assert cross_site.status_code == 403

        bad_host = client.get("/admin/api/accounts", headers={"Host": "evil.example"})
        assert bad_host.status_code == 403

    remote = TestClient(
        _app(manager),
        base_url="http://localhost:8123",
        client=("192.0.2.4", 50000),
    )
    with remote:
        response = remote.get("/admin/api/accounts")
    assert response.status_code == 403
    assert manager.login_calls == []


def test_admin_page_is_loaded_as_a_no_store_package_resource(monkeypatch):
    class Resource:
        def joinpath(self, _name):
            return self

        def read_text(self, *, encoding):
            assert encoding == "utf-8"
            return "<!doctype html><title>Account manager</title>"

    monkeypatch.setattr(admin_api.resources, "files", lambda _package: Resource())
    with _client(FakeManager()) as client:
        for path in ("/admin", "/admin/"):
            response = client.get(path)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            assert "Account manager" in response.text


def test_unmatched_admin_path_is_no_store_and_has_generic_error():
    with _client(FakeManager()) as client:
        response = client.get("/admin/api/not-real")
    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"
    assert "private-token-value" not in response.text
