"""Offline account migration: identity, credentials, rollback and scope."""
import json

import pytest

from scripts import migrate_workbuddy_global_endpoint as migration
from codebuddy_proxy.account_manager import AccountManager
from codebuddy_proxy.account_store import AccountStore
from codebuddy_proxy.backend_profile import GLOBAL_PROFILE, DOMESTIC_PROFILE, resolve_backend
from codebuddy_proxy.codebuddy_client_demo import CodeBuddyClient, CodeBuddyError


def add(store, tmp_path, *, endpoint=migration.OLD, profile=GLOBAL_PROFILE, uid="test-user"):
    client = CodeBuddyClient(backend=resolve_backend(profile=profile, endpoint=endpoint,
        session_file=tmp_path / (uid + profile.key + endpoint.split("//")[-1] + ".json")))
    client._save_session({"auth": {"accessToken": "fake-access", "refreshToken": "fake-refresh",
                                  "domain": "www.codebuddy.ai"},
                          "account": {"uid": uid, "nickname": "test"}, "machineId": "test-machine"})
    return store.import_client(client)


def files(root):
    return {path.name: path.read_bytes() for path in root.glob("*.json")}


def test_migrates_identity_and_cooldowns_preserves_tokens_active_and_domestic(tmp_path):
    store = AccountStore(tmp_path / "accounts")
    old = add(store, tmp_path)
    domestic = add(store, tmp_path, endpoint="https://copilot.tencent.com", profile=DOMESTIC_PROFILE)
    store.set_active(old)
    manager = AccountManager(store)
    manager.handle_rate_limit(store.get_client(old), "120", model="test-model")
    manager.close()
    before = files(store.root)
    result = migration.migrate(store.root, execute=True)
    assert result["migrated"] == 1
    current = AccountStore(store.root)
    selected = current.active_client()
    assert selected.endpoint == migration.NEW
    assert current.active_account_id != old
    assert selected.session["auth"] == {"accessToken": "fake-access", "refreshToken": "fake-refresh", "domain": "www.workbuddy.ai"}
    assert selected.session["machineId"] == "test-machine"
    assert (store.root / f"account-{domestic}.json").read_bytes() == before[f"account-{domestic}.json"]
    runtime = json.loads((store.root / migration.RUNTIME).read_text())
    assert old not in runtime["cooldowns"] and current.active_account_id in runtime["cooldowns"]
    backup = __import__("pathlib").Path(result["backup"])
    assert (backup / "index.json").read_bytes() == before["index.json"]
    manager = AccountManager(current)
    assert len(manager.snapshot()["accounts"]) == 2
    manager.close()
    assert migration.migrate(store.root, execute=True)["migrated"] == 0


def test_dry_run_and_custom_endpoint_leave_data_unchanged(tmp_path):
    store = AccountStore(tmp_path / "accounts")
    add(store, tmp_path)
    custom = add(store, tmp_path, endpoint="https://custom.example.test")
    before = files(store.root)
    assert migration.migrate(store.root)["migrated"] == 1
    assert files(store.root) == before
    migration.migrate(store.root, execute=True)
    assert AccountStore(store.root).get_client(custom).endpoint == "https://custom.example.test"


def test_write_failure_rolls_back_every_account_file(tmp_path, monkeypatch):
    store = AccountStore(tmp_path / "accounts")
    add(store, tmp_path)
    before = files(store.root)
    write = migration._atomic_json
    def fail_index(path, payload):
        if path == store.index_file:
            raise OSError("test failure")
        return write(path, payload)
    monkeypatch.setattr(migration, "_atomic_json", fail_index)
    with pytest.raises(OSError):
        migration.migrate(store.root, execute=True)
    assert files(store.root) == before
    assert AccountStore(store.root).active_client().endpoint == migration.OLD


def test_existing_target_is_not_overwritten(tmp_path):
    store = AccountStore(tmp_path / "accounts")
    add(store, tmp_path)
    add(store, tmp_path, endpoint=migration.NEW)
    before = files(store.root)
    with pytest.raises(CodeBuddyError, match="already exists"):
        migration.migrate(store.root, execute=True)
    assert files(store.root) == before
