import threading

import pytest

import codebuddy_proxy.codebuddy_client_demo as client_module
from codebuddy_proxy.codebuddy_client_demo import CodeBuddyClient, CodeBuddyError


class _Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_begin_and_complete_login_are_separate_and_require_uid(tmp_path, monkeypatch):
    client = CodeBuddyClient(session_file=tmp_path / "login.json")
    requests = []

    def request(method, path, *, headers=None, body=None, timeout=30):
        requests.append((method, path, headers or {}, timeout))
        if "/auth/state" in path:
            return {"authUrl": "https://login.example.test/start", "state": "state /?"}
        if "/auth/token" in path:
            return {"accessToken": "access-token", "refreshToken": "refresh-token"}
        if "/login/account" in path:
            return {"uid": "u-1", "nickname": "Test User"}
        raise AssertionError(path)

    monkeypatch.setattr(client, "_request", request)
    challenge = client.begin_login()
    assert challenge == {
        "auth_url": "https://login.example.test/start",
        "state": "state /?",
    }
    assert not client.session_file.exists()

    client.complete_login(challenge, timeout=2)
    assert client.session["account"]["uid"] == "u-1"
    assert client.session["auth"]["accessToken"] == "access-token"
    assert any("state=state%20%2F%3F" in path for _, path, _, _ in requests)
    account_request = next(item for item in requests if "/login/account" in item[1])
    assert account_request[2]["Authorization"] == "Bearer access-token"
    assert "X-Domain" in account_request[2]
    assert all(item[3] <= 2 for item in requests if "/auth/state" not in item[1])


def test_complete_login_total_timeout_covers_account_polling(tmp_path, monkeypatch):
    client = CodeBuddyClient(session_file=tmp_path / "never-saved.json")
    clock = _Clock()
    monkeypatch.setattr(client_module, "time", clock)
    account_calls = 0
    request_timeouts = []

    def request(method, path, *, headers=None, body=None, timeout=30):
        nonlocal account_calls
        request_timeouts.append(timeout)
        if "/auth/token" in path:
            return {"accessToken": "access-token"}
        if "/login/account" in path:
            account_calls += 1
            return {"nickname": "not ready"}
        raise AssertionError(path)

    monkeypatch.setattr(client, "_request", request)
    with pytest.raises(CodeBuddyError, match="登录超时.*uid"):
        client.complete_login({"auth_url": "https://login.test", "state": "s"}, timeout=2)

    assert account_calls == 2
    assert request_timeouts == [2, 2, 1]
    assert clock.now == 2
    assert not client.session_file.exists()


def test_complete_login_cancellation_during_account_poll_does_not_save(tmp_path, monkeypatch):
    client = CodeBuddyClient(session_file=tmp_path / "cancelled.json")
    cancel = threading.Event()

    def request(method, path, *, headers=None, body=None, timeout=30):
        if "/auth/token" in path:
            return {"accessToken": "access-token"}
        if "/login/account" in path:
            cancel.set()
            return {"nickname": "not ready"}
        raise AssertionError(path)

    monkeypatch.setattr(client, "_request", request)
    with pytest.raises(CodeBuddyError, match="登录已取消"):
        client.complete_login({"state": "s"}, timeout=30, cancel_event=cancel)
    assert not client.session_file.exists()


def test_ensure_authenticated_can_forbid_interactive_login(tmp_path, monkeypatch):
    client = CodeBuddyClient(session_file=tmp_path / "empty.json")
    monkeypatch.setattr(client, "refresh", lambda: False)
    login_calls = []
    monkeypatch.setattr(client, "login", lambda **kwargs: login_calls.append(kwargs))

    with pytest.raises(CodeBuddyError, match="禁止自动打开登录流程"):
        client.ensure_authenticated(open_browser=False, allow_login=False)
    assert login_calls == []
    assert not client.session_file.exists()


def test_concurrent_ensure_authentication_refreshes_once(tmp_path, monkeypatch):
    client = CodeBuddyClient(session_file=tmp_path / "refresh-once.json")
    client._save_session(
        {
            "auth": {
                "accessToken": "expired-access",
                "refreshToken": "shared-refresh-token",
                "expiresAt": 1,
            },
            "account": {"uid": "u-1"},
            "machineId": "machine-1",
        }
    )
    request_started = threading.Event()
    release_request = threading.Event()
    calls = []
    errors = []
    barrier = threading.Barrier(3)

    def refresh_request(method, path, *, headers=None, body=None, timeout=30):
        calls.append((method, path, headers))
        request_started.set()
        assert release_request.wait(5)
        return {
            "accessToken": "fresh-access",
            "refreshToken": "rotated-refresh-token",
            "expiresAt": int(client_module.time.time() * 1000) + 3_600_000,
        }

    monkeypatch.setattr(client, "_request", refresh_request)

    def ensure():
        barrier.wait(5)
        try:
            client.ensure_authenticated(allow_login=False)
        except Exception as exc:  # collected for assertion in the test thread
            errors.append(exc)

    workers = [threading.Thread(target=ensure) for _ in range(2)]
    for worker in workers:
        worker.start()
    barrier.wait(5)
    assert request_started.wait(5)
    release_request.set()
    for worker in workers:
        worker.join(5)

    assert all(not worker.is_alive() for worker in workers)
    assert errors == []
    assert len(calls) == 1
    assert client.session["auth"]["accessToken"] == "fresh-access"
    assert client.session["auth"]["refreshToken"] == "rotated-refresh-token"


def test_login_cli_entry_reuses_begin_and_complete(tmp_path, monkeypatch):
    client = CodeBuddyClient(session_file=tmp_path / "cli.json")
    seen = []
    monkeypatch.setattr(
        client, "begin_login", lambda: {"auth_url": "https://login.test", "state": "s"}
    )
    monkeypatch.setattr(client, "complete_login", lambda challenge, timeout: seen.append((challenge, timeout)))
    opened = []
    monkeypatch.setattr(client_module.webbrowser, "open", lambda url: opened.append(url))

    client.login(open_browser=True, timeout=37)

    assert opened == ["https://login.test"]
    assert seen == [({"auth_url": "https://login.test", "state": "s"}, 37)]
