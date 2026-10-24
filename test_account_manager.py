import threading
import time
import json
from datetime import datetime, timezone
from email.utils import format_datetime
from types import SimpleNamespace

import pytest

from codebuddy_proxy.account_manager import AccountManager, AccountManagerError
from codebuddy_proxy.account_store import AccountStore
from codebuddy_proxy.codebuddy_client_demo import CodeBuddyClient


class FakeStore:
    def __init__(self, root):
        self.root = root
        self._accounts = []
        self._clients = {}
        self._active_id = None
        self.import_calls = []
        self.set_active_calls = []

    @property
    def active_account_id(self):
        return self._active_id

    def list_accounts(self):
        return [
            {key: account[key] for key in ("id", "backend", "endpoint", "uid", "nickname", "active")}
            for account in self._accounts
        ]

    def active_client(self):
        return self._clients.get(self._active_id)

    def get_client(self, account_id):
        return self._clients[account_id]

    def import_client(self, client, *, activate=False):
        self.import_calls.append(activate)
        profile = client.backend
        endpoint = client.endpoint
        account = client.session["account"]
        existing = next(
            (
                item
                for item in self._accounts
                if item["backend"] == profile
                and item["endpoint"] == endpoint
                and item["uid"] == str(account["uid"])
            ),
            None,
        )
        if existing is None:
            account_id = f"account-{len(self._accounts) + 1}"
            existing = {
                "id": account_id,
                "backend": profile,
                "endpoint": endpoint,
                "uid": str(account["uid"]),
                "nickname": account.get("nickname"),
                "active": False,
            }
            self._accounts.append(existing)
        else:
            account_id = existing["id"]
        self._clients[account_id] = client
        if activate or self._active_id is None:
            self.set_active(account_id)
        return account_id

    def set_active(self, account_id):
        if account_id not in self._clients:
            raise KeyError(account_id)
        self.set_active_calls.append(account_id)
        self._active_id = account_id
        for account in self._accounts:
            account["active"] = account["id"] == account_id
        return self._clients[account_id]

    def delete_account(self, account_id):
        if account_id not in self._clients:
            raise KeyError(account_id)
        self._accounts = [item for item in self._accounts if item["id"] != account_id]
        self._clients.pop(account_id)


class FakeLoginClient:
    def __init__(self, backend, session_file, *, uid, begin_gate=None, complete_gate=None):
        self.backend = backend
        self.endpoint = f"https://{backend}.example.test"
        self.session_file = session_file
        self.uid = uid
        self.begin_gate = begin_gate
        self.complete_gate = complete_gate
        self.begin_started = threading.Event()
        self.complete_started = threading.Event()
        self.session = {}
        self.completed_challenge = None
        self.cancel_event = None

    def begin_login(self):
        self.begin_started.set()
        if self.begin_gate is not None:
            self.begin_gate.wait(5)
        return {"auth_url": "https://login.example.test/authorize?flow=abc", "state": "opaque-state"}

    def complete_login(self, challenge, *, timeout, cancel_event):
        self.complete_started.set()
        self.completed_challenge = challenge
        self.cancel_event = cancel_event
        if self.complete_gate is not None:
            self.complete_gate.wait(5)
        # Deliberately finish successfully even if cancellation won. The
        # manager must still prevent this temporary session from being saved.
        self.session = {
            "auth": {"accessToken": "private-token-value"},
            "account": {"uid": self.uid, "nickname": f"User {self.uid}"},
        }


class StoredClient:
    def __init__(self, backend, uid, *, endpoint=None, auth=None):
        self.backend = backend
        self.profile = SimpleNamespace(key=backend)
        self.endpoint = endpoint or f"https://{backend}.example.test"
        self.session = {
            "auth": auth if auth is not None else {"accessToken": f"token-{uid}"},
            "account": {"uid": uid, "nickname": f"User {uid}"},
        }

    def _snapshot_session(self):
        return self.session, 1


def _wait_for(manager, job_id, status):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = manager.get_login(job_id)
        if result["status"] == status:
            return result
        time.sleep(0.01)
    raise AssertionError(f"login did not reach {status}: {manager.get_login(job_id)}")


def test_login_imports_only_after_success_and_does_not_switch_existing_account(tmp_path):
    store = FakeStore(tmp_path)
    made_clients = []

    def make_client(backend, session_file):
        client = FakeLoginClient(backend, session_file, uid=f"u{len(made_clients) + 1}")
        made_clients.append(client)
        return client

    manager = AccountManager(store, client_factory=make_client)
    try:
        first = manager.start_login("domestic")
        first_result = _wait_for(manager, first["id"], "succeeded")
        assert first_result["account_id"] == "account-1"
        assert manager.snapshot()["active_account_id"] == "account-1"
        assert manager.cancel_login(first["id"])["status"] == "succeeded"

        second = manager.start_login("global")
        second_result = _wait_for(manager, second["id"], "succeeded")
        assert second_result["account_id"] == "account-2"
        assert manager.snapshot()["active_account_id"] == "account-1"
        assert store.import_calls == [True, False]
        assert manager.client is store.active_client()
        assert all("state" not in result for result in (first_result, second_result))
        assert all("session" not in result and "token" not in result for result in (first_result, second_result))
        assert tmp_path in made_clients[0].session_file.parents
        assert not made_clients[0].session_file.exists()
    finally:
        manager.close()


def test_cancel_wins_if_it_happens_before_login_commit(tmp_path):
    store = FakeStore(tmp_path)
    completion_gate = threading.Event()
    client = FakeLoginClient(
        "domestic", tmp_path / "unused", uid="cancelled-user", complete_gate=completion_gate
    )
    manager = AccountManager(store, client_factory=lambda backend, path: client)
    try:
        job = manager.start_login("domestic")
        assert client.complete_started.wait(2)
        cancelled = manager.cancel_login(job["id"])
        assert cancelled["status"] == "cancelled"
        assert client.cancel_event.is_set()

        completion_gate.set()
        time.sleep(0.05)
        assert manager.get_login(job["id"])["status"] == "cancelled"
        assert store.import_calls == []
        assert manager.snapshot()["accounts"] == []
    finally:
        completion_gate.set()
        manager.close()


def test_close_cancels_active_login_and_prevents_store_import(tmp_path):
    store = FakeStore(tmp_path)
    client = FakeLoginClient("domestic", tmp_path / "unused", uid="closed-user")

    def wait_until_cancelled(challenge, *, timeout, cancel_event):
        client.complete_started.set()
        client.completed_challenge = challenge
        client.cancel_event = cancel_event
        assert cancel_event.wait(2)
        # Even a completion racing with shutdown must not enter the real store.
        client.session = {
            "auth": {"accessToken": "private-token-value"},
            "account": {"uid": client.uid},
        }

    client.complete_login = wait_until_cancelled
    manager = AccountManager(store, client_factory=lambda backend, path: client)
    job = manager.start_login("domestic")
    assert client.complete_started.wait(2)
    manager.close()

    assert manager.get_login(job["id"])["status"] == "cancelled"
    assert store.import_calls == []


def test_login_failure_does_not_expose_exception_text(tmp_path):
    store = FakeStore(tmp_path)

    class FailingClient(FakeLoginClient):
        def begin_login(self):
            raise RuntimeError("provider returned private-token-value and raw payload")

    client = FailingClient("domestic", tmp_path / "unused", uid="never-saved")
    manager = AccountManager(store, client_factory=lambda backend, path: client)
    try:
        job = manager.start_login("domestic")
        result = _wait_for(manager, job["id"], "failed")
        assert result["error"] == "登录失败，请稍后重新尝试"
        assert "private-token-value" not in repr(result)
        assert "raw payload" not in repr(result)
        assert store.import_calls == []
    finally:
        manager.close()


def test_login_concurrency_is_bounded_and_invalid_backend_is_rejected(tmp_path):
    store = FakeStore(tmp_path)
    begin_gate = threading.Event()
    client = FakeLoginClient("domestic", tmp_path / "unused", uid="u1", begin_gate=begin_gate)
    manager = AccountManager(
        store,
        max_concurrent_logins=1,
        client_factory=lambda backend, path: client,
    )
    try:
        job = manager.start_login("domestic")
        assert client.begin_started.wait(2)
        with pytest.raises(AccountManagerError) as capacity_error:
            manager.start_login("global")
        assert capacity_error.value.status_code == 429
        with pytest.raises(AccountManagerError):
            manager.start_login("unknown")
        manager.cancel_login(job["id"])
    finally:
        begin_gate.set()
        manager.close()


def test_cancelled_but_running_factory_keeps_its_concurrency_slot(tmp_path):
    store = FakeStore(tmp_path)
    factory_started = threading.Event()
    factory_gate = threading.Event()
    made_clients = []

    def blocking_factory(backend, session_file):
        factory_started.set()
        factory_gate.wait(5)
        client = FakeLoginClient(backend, session_file, uid="u1")
        made_clients.append(client)
        return client

    manager = AccountManager(
        store,
        max_concurrent_logins=1,
        client_factory=blocking_factory,
    )
    try:
        job = manager.start_login("domestic")
        assert factory_started.wait(2)
        assert manager.cancel_login(job["id"])["status"] == "cancelled"

        # Cancellation marks the public state immediately, but the blocked
        # worker still occupies the only slot until its future actually ends.
        for _ in range(3):
            with pytest.raises(AccountManagerError) as capacity_error:
                manager.start_login("global")
            assert capacity_error.value.status_code == 429

        factory_gate.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with manager._lock:
                original = manager._jobs[job["id"]]
                if original.future.done():
                    break
            time.sleep(0.01)
        assert original.future.done()
        assert len(made_clients) == 1
        assert not made_clients[0].begin_started.is_set()
        assert store.import_calls == []
    finally:
        factory_gate.set()
        manager.close()


def test_login_manager_imports_a_real_codebuddy_client_into_account_store(tmp_path, monkeypatch):
    store = AccountStore(tmp_path / "accounts")

    def begin_login(_client):
        return {"auth_url": "https://login.example.test/authorize", "state": "test-state"}

    def complete_login(self, challenge, *, timeout, cancel_event):
        assert challenge == {"auth_url": "https://login.example.test/authorize", "state": "test-state"}
        assert timeout == 300
        assert not cancel_event.is_set()
        self._save_session(
            {
                **self.session,
                "auth": {"accessToken": "test-token"},
                "account": {"uid": "integration-user", "nickname": "Integration user"},
            }
        )

    monkeypatch.setattr(CodeBuddyClient, "begin_login", begin_login)
    monkeypatch.setattr(CodeBuddyClient, "complete_login", complete_login)

    manager = AccountManager(store)
    try:
        job = manager.start_login("global")
        result = _wait_for(manager, job["id"], "succeeded")
        assert result["account_id"]
        accounts = store.list_accounts()
        assert len(accounts) == 1
        assert accounts[0]["backend"] == "global"
        assert accounts[0]["uid"] == "integration-user"
        assert accounts[0]["active"] is True
        assert manager.client.session["auth"]["accessToken"] == "test-token"
        assert "test-token" not in repr(result)
    finally:
        manager.close()


def test_rate_limit_snapshot_defaults_and_runtime_state_persist_without_credentials(tmp_path):
    store = FakeStore(tmp_path)
    client = StoredClient("domestic", "rate-limited")
    store.import_client(client, activate=True)
    manager = AccountManager(store)
    try:
        account_id = manager.account_id_for_client(client)
        assert account_id == "account-1"
        assert manager.get_settings() == {
            "auto_switch_on_429": False,
            "default_cooldown_seconds": 60,
            "auto_checkin_enabled": False,
        }
        manager.set_settings(True)

        before = time.time()
        assert manager.handle_rate_limit(client, "invalid retry value") is None
        snapshot = manager.snapshot()
        limited = snapshot["accounts"][0]
        assert snapshot["settings"]["auto_switch_on_429"] is True
        assert snapshot["server_time"] >= before
        assert limited["rate_limited"] is True
        assert limited["cooldown_source"] == "default"
        assert limited["cooldown_until"] == pytest.approx(before + 60, abs=1)
        assert 1 <= limited["cooldown_remaining_seconds"] <= 60
        assert limited["last_rate_limited_at"] == pytest.approx(before, abs=1)

        state_file = tmp_path / "account-manager-state-v1.json"
        raw_state = state_file.read_text(encoding="utf-8")
        assert "token-rate-limited" not in raw_state
        assert "accessToken" not in raw_state
    finally:
        manager.close()

    reopened = AccountManager(store)
    try:
        assert reopened.get_settings()["auto_switch_on_429"] is True
        restored = reopened.snapshot()["accounts"][0]
        assert restored["rate_limited"] is True
        assert restored["cooldown_source"] == "default"
        assert restored["cooldown_until"] == pytest.approx(limited["cooldown_until"])
    finally:
        reopened.close()


def test_retry_after_zero_and_http_date_are_supported_and_expire_without_poll_writes(
    tmp_path, monkeypatch
):
    store = FakeStore(tmp_path)
    zero_client = StoredClient("domestic", "zero")
    date_client = StoredClient("domestic", "date")
    store.import_client(zero_client, activate=True)
    store.import_client(date_client)
    manager = AccountManager(store)
    try:
        fixed_now = time.time()
        zero_deadline = fixed_now
        date_deadline = fixed_now + 75
        date_value = format_datetime(
            datetime.fromtimestamp(date_deadline, timezone.utc), usegmt=True
        )
        monkeypatch.setattr("codebuddy_proxy.account_manager.time.time", lambda: fixed_now)
        assert manager.handle_rate_limit(zero_client, "0") is None
        assert manager.handle_rate_limit(date_client, date_value) is None

        rows = {item["uid"]: item for item in manager.snapshot()["accounts"]}
        assert rows["zero"]["rate_limited"] is False
        assert rows["zero"]["cooldown_source"] is None
        assert rows["zero"]["last_rate_limited_at"] == pytest.approx(fixed_now)
        assert rows["date"]["rate_limited"] is True
        assert rows["date"]["cooldown_source"] == "retry_after"
        assert rows["date"]["cooldown_until"] == pytest.approx(date_deadline, abs=1)

        persisted_before_expiry = (tmp_path / "account-manager-state-v1.json").read_text(
            encoding="utf-8"
        )
        monkeypatch.setattr(
            "codebuddy_proxy.account_manager.time.time", lambda: fixed_now + 76
        )
        expired = {item["uid"]: item for item in manager.snapshot()["accounts"]}["date"]
        assert expired["rate_limited"] is False
        assert expired["cooldown_until"] is None
        assert expired["cooldown_remaining_seconds"] == 0
        assert expired["last_rate_limited_at"] == pytest.approx(fixed_now)
        assert (
            tmp_path / "account-manager-state-v1.json"
        ).read_text(encoding="utf-8") == persisted_before_expiry
    finally:
        manager.close()


def test_auto_switch_is_opt_in_region_scoped_and_respects_exclusions(tmp_path):
    store = FakeStore(tmp_path)
    original = StoredClient("domestic", "original")
    excluded = StoredClient("domestic", "excluded")
    candidate = StoredClient("domestic", "candidate")
    other_region = StoredClient("global", "global")
    original_id = store.import_client(original, activate=True)
    excluded_id = store.import_client(excluded)
    candidate_id = store.import_client(candidate)
    store.import_client(other_region)
    manager = AccountManager(store)
    try:
        assert manager.handle_rate_limit(original, "30") is None
        assert store.active_account_id == original_id

        manager.set_settings(True)
        switched = manager.handle_rate_limit(
            original, "30", excluded_account_ids={excluded_id}
        )
        assert switched is candidate
        assert store.active_account_id == candidate_id

        # A delayed 429 from the old request records the longer deadline but
        # cannot rotate away from the account that is now active.
        latest = manager.handle_rate_limit(original, "5")
        assert latest is None
        assert store.active_account_id == candidate_id
        row = next(item for item in manager.snapshot()["accounts"] if item["id"] == original_id)
        assert row["cooldown_remaining_seconds"] > 0
        assert row["cooldown_source"] == "retry_after"
    finally:
        manager.close()


def test_concurrent_429s_from_same_active_account_only_switch_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    store = FakeStore(tmp_path)
    original = StoredClient("domestic", "original")
    candidate = StoredClient("domestic", "candidate")
    original_id = store.import_client(original, activate=True)
    candidate_id = store.import_client(candidate)
    manager = AccountManager(store)
    manager.set_settings(True)
    barrier = threading.Barrier(3)

    def rate_limit():
        barrier.wait(timeout=2)
        return manager.handle_rate_limit(original, "45")

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = [executor.submit(rate_limit) for _ in range(2)]
            barrier.wait(timeout=2)
            clients = [future.result(timeout=3) for future in results]
        assert clients.count(candidate) == 1
        assert clients.count(None) == 1
        assert store.active_account_id == candidate_id
        assert store.set_active_calls.count(candidate_id) == 1
        row = next(item for item in manager.snapshot()["accounts"] if item["id"] == original_id)
        assert row["rate_limited"] is True
    finally:
        manager.close()


def test_cooldown_max_deadline_manual_switch_and_refreshable_candidate(tmp_path, monkeypatch):
    store = FakeStore(tmp_path)
    original = StoredClient("domestic", "original")
    expired_candidate = StoredClient(
        "domestic",
        "expired",
        auth={"accessToken": "expired", "expiresAt": 1},
    )
    refreshable = StoredClient(
        "domestic",
        "refreshable",
        auth={"refreshToken": "refresh-only"},
    )
    original_id = store.import_client(original, activate=True)
    store.import_client(expired_candidate)
    refreshable_id = store.import_client(refreshable)
    manager = AccountManager(store)
    try:
        manager.set_settings(True)
        fixed_now = 1_800_000_000.0
        monkeypatch.setattr("codebuddy_proxy.account_manager.time.time", lambda: fixed_now)
        assert manager.handle_rate_limit(original, "120") is refreshable
        assert store.active_account_id == refreshable_id

        # The later shorter default cooldown must not reduce the original 120s.
        assert manager.handle_rate_limit(original, None) is None
        original_row = next(
            item for item in manager.snapshot()["accounts"] if item["id"] == original_id
        )
        assert original_row["cooldown_until"] == fixed_now + 120
        assert original_row["cooldown_source"] == "retry_after"

        # Even with automation enabled, an explicit selection is preserved if
        # a previously active client's 429 arrives afterward.
        manager.activate(original_id)
        assert manager.handle_rate_limit(refreshable, "10") is None
        assert store.active_account_id == original_id
    finally:
        manager.close()


def test_cooldown_is_cleared_when_account_is_deleted_and_bad_runtime_file_is_preserved(tmp_path):
    store = FakeStore(tmp_path)
    active = StoredClient("domestic", "active")
    inactive = StoredClient("domestic", "inactive")
    store.import_client(active, activate=True)
    inactive_id = store.import_client(inactive)
    manager = AccountManager(store)
    try:
        manager.handle_rate_limit(inactive, "45")
        state_file = tmp_path / "account-manager-state-v1.json"
        manager.delete(inactive_id)
        state = json.loads(state_file.read_text(encoding="utf-8"))
        assert inactive_id not in state["cooldowns"]
        assert all(row["id"] != inactive_id for row in manager.snapshot()["accounts"])
    finally:
        manager.close()

    state_file = tmp_path / "account-manager-state-v1.json"
    corrupted_values = (
        "{broken",
        '{"version":2,"settings":{"auto_switch_on_429":false,"default_cooldown_seconds":60},"cooldowns":{}}',
        '{"version":true,"settings":{"auto_switch_on_429":false,"default_cooldown_seconds":60,"auto_checkin_enabled":false},"cooldowns":{}}',
        '{"version":1,"settings":{"auto_switch_on_429":"true","default_cooldown_seconds":60},"cooldowns":{}}',
        '{"version":1,"settings":{"auto_switch_on_429":false,"default_cooldown_seconds":60,"auto_checkin_enabled":1},"cooldowns":{}}',
        '{"version":1,"settings":{"auto_switch_on_429":false,"default_cooldown_seconds":60,"unexpected":false},"cooldowns":{}}',
        '{"version":1,"version":1,"settings":{"auto_switch_on_429":false,"default_cooldown_seconds":60},"cooldowns":{}}',
    )
    for corrupted in corrupted_values:
        state_file.write_text(corrupted, encoding="utf-8")
        with pytest.raises(AccountManagerError) as failure:
            AccountManager(store)
        assert failure.value.message in {
            "账号管理状态文件损坏，未修改任何数据",
            "账号管理状态文件版本不受支持，未修改任何数据",
        }
        assert state_file.read_text(encoding="utf-8") == corrupted


def test_failed_cooldown_persistence_aborts_account_deletion(tmp_path, monkeypatch):
    import codebuddy_proxy.account_manager as account_manager_module

    store = FakeStore(tmp_path)
    active = StoredClient("domestic", "active")
    inactive = StoredClient("domestic", "inactive")
    store.import_client(active, activate=True)
    inactive_id = store.import_client(inactive)
    manager = AccountManager(store)
    try:
        manager.handle_rate_limit(inactive, "45")
        state_file = tmp_path / "account-manager-state-v1.json"
        original_state = state_file.read_text(encoding="utf-8")

        def failed_write(_path, _payload):
            raise OSError("private runtime-state path")

        monkeypatch.setattr(account_manager_module, "_atomic_json", failed_write)
        with pytest.raises(AccountManagerError) as failure:
            manager.delete(inactive_id)

        assert failure.value.message == "无法保存账号管理状态"
        assert manager.has_account(inactive_id)
        row = next(
            account for account in manager.snapshot()["accounts"]
            if account["id"] == inactive_id
        )
        assert row["rate_limited"] is True
        assert state_file.read_text(encoding="utf-8") == original_state
    finally:
        manager.close()


def test_failed_store_deletion_restores_persisted_cooldown(tmp_path, monkeypatch):
    store = FakeStore(tmp_path)
    active = StoredClient("domestic", "active")
    inactive = StoredClient("domestic", "inactive")
    store.import_client(active, activate=True)
    inactive_id = store.import_client(inactive)
    manager = AccountManager(store)
    try:
        manager.handle_rate_limit(inactive, "45")
        state_file = tmp_path / "account-manager-state-v1.json"
        original_state = json.loads(state_file.read_text(encoding="utf-8"))

        def failed_delete(_account_id):
            raise OSError("simulated account-store failure")

        monkeypatch.setattr(store, "delete_account", failed_delete)
        with pytest.raises(OSError, match="simulated account-store failure"):
            manager.delete(inactive_id)

        assert manager.has_account(inactive_id)
        row = next(
            account for account in manager.snapshot()["accounts"]
            if account["id"] == inactive_id
        )
        assert row["rate_limited"] is True
        restored_state = json.loads(state_file.read_text(encoding="utf-8"))
        assert restored_state["cooldowns"][inactive_id] == original_state["cooldowns"][inactive_id]
    finally:
        manager.close()


def test_failed_settings_persistence_rolls_back_memory_and_uses_safe_error(tmp_path, monkeypatch):
    import codebuddy_proxy.account_manager as account_manager_module

    manager = AccountManager(FakeStore(tmp_path))
    try:
        manager.set_settings(True)
        state_file = tmp_path / "account-manager-state-v1.json"
        original = state_file.read_text(encoding="utf-8")

        def failed_write(_path, _payload):
            raise OSError("private path and token value")

        monkeypatch.setattr(account_manager_module, "_atomic_json", failed_write)
        with pytest.raises(AccountManagerError) as failure:
            manager.set_settings(False)
        assert failure.value.message == "无法保存账号管理状态"
        assert "private path" not in str(failure.value)
        assert manager.get_settings()["auto_switch_on_429"] is True
        assert state_file.read_text(encoding="utf-8") == original
    finally:
        manager.close()


def test_fake_store_without_root_keeps_runtime_settings_in_memory(tmp_path):
    store = FakeStore(tmp_path)
    del store.root
    manager = AccountManager(store)
    try:
        assert manager.set_settings(True)["auto_switch_on_429"] is True
        assert manager.get_settings()["auto_switch_on_429"] is True
    finally:
        manager.close()


def test_legacy_runtime_settings_migrate_on_write_without_losing_cooldowns(tmp_path):
    store = FakeStore(tmp_path)
    client = StoredClient("domestic", "legacy")
    store.import_client(client, activate=True)
    deadline = time.time() + 120
    state_path = tmp_path / "account-manager-state-v1.json"
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "settings": {
                    "auto_switch_on_429": True,
                    "default_cooldown_seconds": 60,
                },
                "cooldowns": {
                    "account-1": {
                        "deadline": deadline,
                        "last_rate_limited_at": deadline - 10,
                        "source": "retry_after",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    manager = AccountManager(store)
    try:
        assert manager.get_settings() == {
            "auto_switch_on_429": True,
            "default_cooldown_seconds": 60,
            "auto_checkin_enabled": False,
        }
        assert manager.snapshot()["accounts"][0]["cooldown_until"] == pytest.approx(deadline)

        manager.set_settings(auto_checkin_enabled=True)
        persisted = json.loads(state_path.read_text(encoding="utf-8"))
        assert persisted["settings"] == {
            "auto_switch_on_429": True,
            "default_cooldown_seconds": 60,
            "auto_checkin_enabled": True,
        }
        assert persisted["cooldowns"]["account-1"]["deadline"] == pytest.approx(deadline)
    finally:
        manager.close()


def test_checkin_setting_callback_only_follows_successful_transitions(tmp_path, monkeypatch):
    import codebuddy_proxy.account_manager as account_manager_module

    manager = AccountManager(FakeStore(tmp_path))
    callbacks = []
    manager.checkins.settings_changed = callbacks.append
    try:
        manager.set_settings(auto_switch_on_429=True)
        manager.set_settings(auto_checkin_enabled=False)
        assert callbacks == []

        manager.set_settings(auto_checkin_enabled=True)
        assert callbacks == [True]
        manager.set_settings(auto_checkin_enabled=True)
        assert callbacks == [True]

        state_path = tmp_path / "account-manager-state-v1.json"
        original = state_path.read_text(encoding="utf-8")

        def failed_write(_path, _payload):
            raise OSError("private path and token value")

        monkeypatch.setattr(account_manager_module, "_atomic_json", failed_write)
        with pytest.raises(AccountManagerError) as failure:
            manager.set_settings(auto_checkin_enabled=False)
        assert failure.value.message == "无法保存账号管理状态"
        assert manager.get_settings()["auto_checkin_enabled"] is True
        assert callbacks == [True]
        assert state_path.read_text(encoding="utf-8") == original
    finally:
        manager.close()


def test_checkin_rate_limit_records_cooldown_without_switching_active(tmp_path):
    store = FakeStore(tmp_path)
    checked_client = StoredClient("domestic", "checked-client")
    backup_client = StoredClient("domestic", "backup-client")
    original_id = store.import_client(checked_client, activate=True)
    store.import_client(backup_client)
    manager = AccountManager(store)
    try:
        manager.set_settings(True)
        assert manager.handle_rate_limit(
            checked_client, "45", allow_switch=False
        ) is None
        assert store.active_account_id == original_id
        row = next(
            account
            for account in manager.snapshot()["accounts"]
            if account["id"] == original_id
        )
        assert row["rate_limited"] is True
        assert row["cooldown_source"] == "retry_after"
    finally:
        manager.close()
