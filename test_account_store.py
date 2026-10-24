import json
import threading

import pytest

import codebuddy_proxy.account_store as account_store_module
from codebuddy_proxy.account_store import AccountStore
from codebuddy_proxy.backend_profile import DOMESTIC_PROFILE, GLOBAL_PROFILE, resolve_backend
from codebuddy_proxy.codebuddy_client_demo import CodeBuddyClient, CodeBuddyError


def _client(path, *, profile=DOMESTIC_PROFILE, endpoint=None, uid="user-1", access="access-1"):
    client = CodeBuddyClient(
        backend=resolve_backend(
            profile=profile,
            endpoint=endpoint,
            session_file=path,
            environment={},
        )
    )
    client._save_session(
        {
            "auth": {"accessToken": access, "refreshToken": f"refresh-{access}"},
            "account": {"uid": uid, "nickname": f"Name {uid}"},
            "machineId": "machine-1",
        }
    )
    return client


def test_import_isolated_and_reassociation_updates_cached_client(tmp_path):
    store = AccountStore(tmp_path / "accounts")
    source_file = tmp_path / "source.json"
    source = _client(source_file)
    source_before = source_file.read_bytes()

    account_id = store.import_client(source)
    assert store.active_account_id == account_id
    cached = store.get_client(account_id)
    assert cached.session_file != source_file
    assert source_file.read_bytes() == source_before

    replacement = _client(tmp_path / "replacement.json", access="access-2")
    replacement_before = replacement.session_file.read_bytes()
    assert store.import_client(replacement) == account_id
    assert replacement.session_file.read_bytes() == replacement_before
    assert store.get_client(account_id) is cached
    assert cached.session["auth"]["accessToken"] == "access-2"

    index_text = (store.root / "index.json").read_text(encoding="utf-8")
    assert "access-1" not in index_text
    assert "access-2" not in index_text
    assert "refresh-access-2" not in index_text
    assert set(json.loads(index_text)["accounts"][0]) == {
        "id", "backend", "endpoint", "uid", "nickname"
    }
    assert store.list_accounts() == [
        {
            "id": account_id,
            "backend": "domestic",
            "endpoint": DOMESTIC_PROFILE.default_endpoint,
            "uid": "user-1",
            "nickname": "Name user-1",
            "active": True,
        }
    ]


def test_identity_includes_backend_and_endpoint_and_clear_is_persistent(tmp_path):
    root = tmp_path / "accounts"
    store = AccountStore(root)
    domestic = _client(tmp_path / "domestic.json")
    global_client = _client(
        tmp_path / "global.json", profile=GLOBAL_PROFILE, uid="user-1", access="global-access"
    )
    custom = _client(
        tmp_path / "custom.json",
        endpoint="https://staging.example.test",
        uid="user-1",
        access="custom-access",
    )

    domestic_id = store.import_client(domestic)
    global_id = store.import_client(global_client)
    custom_id = store.import_client(custom)
    assert len({domestic_id, global_id, custom_id}) == 3
    assert store.active_account_id == domestic_id

    active_global = store.set_active(global_id)
    assert active_global.session["auth"]["accessToken"] == "global-access"
    with pytest.raises(CodeBuddyError, match="不能删除当前生效账号"):
        store.delete_account(global_id)

    store.clear_active()
    reopened = AccountStore(root)
    assert reopened.active_account_id is None
    assert reopened.active_client() is None
    # Re-importing an existing account after an explicit clear keeps the store empty.
    assert reopened.import_client(global_client) == global_id
    assert reopened.active_account_id is None
    reopened.delete_account(global_id)
    assert all(item["id"] != global_id for item in reopened.list_accounts())


def test_corrupt_index_is_rejected_without_overwrite(tmp_path):
    root = tmp_path / "accounts"
    root.mkdir()
    index = root / "index.json"
    index.write_text("{broken", encoding="utf-8")

    with pytest.raises(CodeBuddyError, match="账号索引文件损坏"):
        AccountStore(root)
    assert index.read_text(encoding="utf-8") == "{broken"


@pytest.mark.parametrize(
    "session",
    [
        {"auth": {"accessToken": "token"}, "account": {"nickname": "No uid"}},
        {"auth": {}, "account": {"uid": "user-1"}},
    ],
)
def test_import_rejects_legacy_session_without_uid_or_token(tmp_path, session):
    client = CodeBuddyClient(session_file=tmp_path / "legacy.json")
    client._save_session(session)
    with pytest.raises(CodeBuddyError, match="缺少"):
        AccountStore(tmp_path / "accounts").import_client(client)
    assert not (tmp_path / "accounts" / "index.json").exists()


def test_inflight_old_refresh_cannot_overwrite_reassociated_token(tmp_path, monkeypatch):
    store = AccountStore(tmp_path / "accounts")
    account_id = store.import_client(_client(tmp_path / "first.json", access="first-access"))
    cached = store.get_client(account_id)
    request_started = threading.Event()
    release_request = threading.Event()
    refresh_result = []

    def delayed_refresh(method, path, *, headers=None, body=None, timeout=30):
        request_started.set()
        assert release_request.wait(5)
        return {"accessToken": "stale-refreshed-access", "refreshToken": "stale-refresh"}

    monkeypatch.setattr(cached, "_request", delayed_refresh)
    thread = threading.Thread(target=lambda: refresh_result.append(cached.refresh()))
    thread.start()
    assert request_started.wait(5)

    assert store.import_client(
        _client(tmp_path / "new-login.json", access="new-login-access")
    ) == account_id
    release_request.set()
    thread.join(5)

    assert not thread.is_alive()
    assert refresh_result == [True]
    assert cached.session["auth"]["accessToken"] == "new-login-access"
    saved = json.loads(cached.session_file.read_text(encoding="utf-8"))
    assert saved["auth"]["accessToken"] == "new-login-access"


def test_failed_first_import_rolls_back_orphan_session_and_index(tmp_path, monkeypatch):
    store = AccountStore(tmp_path / "accounts")
    source = _client(tmp_path / "first-import.json", access="new-token")
    _, metadata = store._validated_client_session(source)
    session_file = store._session_file(metadata["id"])
    write_index = store._write_index_locked

    def fail_index_write(index):
        write_index(index)
        raise OSError("injected index write failure")

    monkeypatch.setattr(store, "_write_index_locked", fail_index_write)
    with pytest.raises(OSError, match="injected index write failure"):
        store.import_client(source)

    assert not session_file.exists()
    assert not (store.root / "index.json").exists()
    assert store.list_accounts() == []


def test_failed_reassociation_restores_session_file_and_cached_state(tmp_path, monkeypatch):
    store = AccountStore(tmp_path / "accounts")
    account_id = store.import_client(
        _client(tmp_path / "old-login.json", access="old-token")
    )
    cached = store.get_client(account_id)
    previous_session, _ = cached._snapshot_session()
    session_file = cached.session_file
    previous_file = session_file.read_bytes()
    index_file = store.index_file
    previous_index = index_file.read_bytes()
    replacement = _client(tmp_path / "new-login.json", access="new-token")
    write_index = store._write_index_locked

    def fail_index_write(index):
        write_index(index)
        raise OSError("injected index write failure")

    monkeypatch.setattr(store, "_write_index_locked", fail_index_write)
    with pytest.raises(OSError, match="injected index write failure"):
        store.import_client(replacement)

    assert session_file.read_bytes() == previous_file
    assert index_file.read_bytes() == previous_index
    assert cached.session == previous_session
    assert store.get_client(account_id) is cached
    assert store.list_accounts()[0]["nickname"] == "Name user-1"
    assert "old-token" in previous_file.decode("utf-8")
    assert "new-token" not in session_file.read_text(encoding="utf-8")


def test_import_rollback_holds_session_lock_against_concurrent_writer(
    tmp_path, monkeypatch
):
    store = AccountStore(tmp_path / "accounts")
    account_id = store.import_client(_client(tmp_path / "first.json", access="old"))
    cached = store.get_client(account_id)
    session_file = cached.session_file
    original_restore = account_store_module._restore_file
    writer_session, _ = cached._snapshot_session()
    writer_session["auth"]["accessToken"] = "writer-access"
    writer_session["auth"]["refreshToken"] = "writer-refresh"
    rollback_paused = threading.Event()
    finish_rollback = threading.Event()
    writer_call_entered = threading.Event()
    writer_finished = threading.Event()
    import_errors = []
    writer_results = []

    def pause_after_session_restore(path, contents):
        original_restore(path, contents)
        if path == session_file:
            rollback_paused.set()
            assert finish_rollback.wait(5)

    monkeypatch.setattr(account_store_module, "_restore_file", pause_after_session_restore)
    original_save = cached._save_session

    def observe_writer_call(session, *, expected_generation=None):
        if threading.current_thread().name == "session-writer":
            writer_call_entered.set()
        return original_save(session, expected_generation=expected_generation)

    monkeypatch.setattr(cached, "_save_session", observe_writer_call)
    original_index_write = store._write_index_locked

    def fail_after_index_write(index):
        original_index_write(index)
        raise OSError("injected index write failure")

    monkeypatch.setattr(store, "_write_index_locked", fail_after_index_write)

    def import_replacement():
        try:
            store.import_client(_client(tmp_path / "replacement.json", access="new"))
        except Exception as exc:  # captured for assertions in the test thread
            import_errors.append(exc)

    def write_concurrently():
        try:
            writer_results.append(cached._save_session(writer_session))
        finally:
            writer_finished.set()

    importer = threading.Thread(target=import_replacement)
    importer.start()
    assert rollback_paused.wait(5)

    writer = threading.Thread(target=write_concurrently, name="session-writer")
    writer.start()
    assert writer_call_entered.wait(5)
    assert not writer_finished.wait(0.2)

    finish_rollback.set()
    importer.join(5)
    writer.join(5)

    assert not importer.is_alive()
    assert not writer.is_alive()
    assert len(import_errors) == 1
    assert isinstance(import_errors[0], OSError)
    assert writer_results == [True]
    persisted = json.loads(session_file.read_text(encoding="utf-8"))
    assert persisted == cached.session
    assert persisted["auth"]["accessToken"] == "writer-access"


def test_deleting_inactive_account_prevents_inflight_refresh_from_recreating_file(
    tmp_path, monkeypatch
):
    store = AccountStore(tmp_path / "accounts")
    removed_id = store.import_client(_client(tmp_path / "removed.json", access="removed"))
    keeper_id = store.import_client(
        _client(tmp_path / "keeper.json", uid="user-2", access="keeper")
    )
    store.set_active(keeper_id)
    cached = store.get_client(removed_id)
    session_file = cached.session_file
    request_started = threading.Event()
    release_request = threading.Event()
    results = []

    def delayed_refresh(method, path, *, headers=None, body=None, timeout=30):
        request_started.set()
        assert release_request.wait(5)
        return {"accessToken": "late-refresh", "refreshToken": "late-refresh-token"}

    monkeypatch.setattr(cached, "_request", delayed_refresh)
    thread = threading.Thread(target=lambda: results.append(cached.refresh()))
    thread.start()
    assert request_started.wait(5)
    store.delete_account(removed_id)
    release_request.set()
    thread.join(5)

    assert not thread.is_alive()
    assert results == [True]
    assert not session_file.exists()
    assert all(item["id"] != removed_id for item in store.list_accounts())
