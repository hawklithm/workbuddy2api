"""Explicit offline migration of stored Global accounts to WorkBuddy.

Stop the proxy first. Default is a dry run; --execute writes a private backup,
rekeys account/session/cooldown identities and preserves the active selection.
No tokens are printed, refreshed, or sent over the network.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import uuid

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codebuddy_proxy.account_store import AccountStore, _account_id, _atomic_json, _capture_file, _restore_file
from codebuddy_proxy.codebuddy_client_demo import CodeBuddyError

OLD = "https://www.codebuddy.ai"
NEW = "https://www.workbuddy.ai"
RUNTIME = "account-manager-state-v1.json"


def migrate(root: Path, *, execute: bool = False) -> dict:
    store = AccountStore(root)
    with store._lock:
        index = store._read_index_locked()
        selected = [row for row in index["accounts"] if row["backend"] == "global" and row["endpoint"] == OLD]
        if not selected:
            return {"migrated": 0, "execute": execute}
        identities = {row["id"]: _account_id("global", NEW, row["uid"]) for row in selected}
        if set(identities.values()) & {row["id"] for row in index["accounts"]}:
            raise CodeBuddyError("WorkBuddy target account already exists; migration refused")
        writes = {}
        old_sessions = []
        for row in selected:
            old_path = store._session_file(row["id"])
            session, _ = store.get_client(row["id"])._snapshot_session()
            session = deepcopy(session)
            session["endpoint"] = NEW
            auth = session.get("auth") or {}
            if auth.get("domain") in ("codebuddy.ai", "www.codebuddy.ai"):
                auth["domain"] = "www.workbuddy.ai"
            target = store._session_file(identities[row["id"]])
            if target.exists() or target.is_symlink():
                raise CodeBuddyError("Target session already exists; migration refused")
            writes[target] = session
            old_sessions.append(old_path)
        updated = deepcopy(index)
        for row in updated["accounts"]:
            if row["id"] in identities:
                row["id"] = identities[row["id"]]
                row["endpoint"] = NEW
        if updated["active_account_id"] in identities:
            updated["active_account_id"] = identities[updated["active_account_id"]]
        runtime_path = store.root / RUNTIME
        runtime_bytes = _capture_file(runtime_path)
        if runtime_bytes is not None:
            runtime = json.loads(runtime_bytes)
            cooldowns = runtime.get("cooldowns")
            if not isinstance(cooldowns, dict):
                raise CodeBuddyError("Invalid cooldown state; migration refused")
            for old, new in identities.items():
                if new in cooldowns:
                    raise CodeBuddyError("Target cooldown already exists; migration refused")
                if old in cooldowns:
                    cooldowns[new] = cooldowns.pop(old)
            writes[runtime_path] = runtime
        # Publish the new index only after all referenced sessions exist.
        writes[store.index_file] = updated
        snapshots = {path: _capture_file(path) for path in [*writes, *old_sessions]}
        report = {"migrated": len(selected), "execute": execute,
                  "from": OLD, "to": NEW, "active_selection_preserved": True}
        if not execute:
            return report
        backup = store.root / "endpoint-backups" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
        backup.mkdir(parents=True, mode=0o700)
        os.chmod(backup.parent, 0o700)
        for path, contents in snapshots.items():
            if contents is not None:
                (backup / path.name).write_bytes(contents)
                os.chmod(backup / path.name, 0o600)
        _atomic_json(backup / "migration.json", {**report, "new_sessions": [path.name for path in writes if snapshots[path] is None]})
        # Refuse changes made by another process since the plan was built.
        if any(_capture_file(path) != contents for path, contents in snapshots.items()):
            raise CodeBuddyError("Account data changed concurrently; migration refused")
        try:
            for path, payload in writes.items():
                _atomic_json(path, payload)
            verify = AccountStore(store.root)
            for row in verify.list_accounts():
                verify.get_client(row["id"])
            for path in old_sessions:
                path.unlink()
        except BaseException:
            for path, contents in snapshots.items():
                _restore_file(path, contents)
            raise
        return {**report, "backup": str(backup)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.home() / ".workbuddy2api" / "accounts")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(migrate(args.root, execute=args.execute), ensure_ascii=False))
    except (CodeBuddyError, OSError, ValueError) as exc:
        print(f"Migration failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
