"""Persistent, credential-isolated storage for CodeBuddy accounts."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import tempfile
import threading
from contextlib import nullcontext
from typing import Any
from urllib.parse import urlsplit

from codebuddy_proxy.backend_profile import (
    normalize_endpoint,
    profile_for_key,
    resolve_backend,
)
from codebuddy_proxy.codebuddy_client_demo import CodeBuddyClient, CodeBuddyError


_INDEX_VERSION = 1
_ACCOUNT_ID_RE = re.compile(r"^acct_[0-9a-f]{64}$")
_INDEX_FIELDS = {"id", "backend", "endpoint", "uid", "nickname"}
_ROOT_LOCKS: dict[str, Any] = {}
_ROOT_LOCKS_GUARD = threading.Lock()


def _root_lock(root: pathlib.Path):
    key = os.fspath(root)
    with _ROOT_LOCKS_GUARD:
        lock = _ROOT_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _ROOT_LOCKS[key] = lock
        return lock


def _safe_endpoint(value: str) -> str:
    try:
        endpoint = normalize_endpoint(value)
        parsed = urlsplit(endpoint)
    except (TypeError, ValueError) as exc:
        raise CodeBuddyError("账号 endpoint 无效") from exc
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise CodeBuddyError("账号 endpoint 不得在地址中包含凭据或查询参数")
    return endpoint


def _account_id(backend: str, endpoint: str, uid: str) -> str:
    identity = json.dumps(
        [backend, endpoint, uid], ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return f"acct_{hashlib.sha256(identity).hexdigest()}"


def _has_auth_token(auth: Any) -> bool:
    return isinstance(auth, dict) and any(
        isinstance(auth.get(name), str) and bool(auth[name].strip())
        for name in ("accessToken", "refreshToken")
    )


def _atomic_json(path: pathlib.Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    temp_name: str | None = None
    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
        temp_name = None
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError as exc:
        raise CodeBuddyError(f"无法原子写入账号文件: {path.name}") from exc
    finally:
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except OSError:
                pass


def _capture_file(path: pathlib.Path) -> bytes | None:
    if path.is_symlink():
        raise CodeBuddyError("账号文件路径无效")
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CodeBuddyError("无法读取账号文件进行安全更新") from exc


def _restore_file(path: pathlib.Path, contents: bytes | None) -> None:
    if path.is_symlink():
        raise CodeBuddyError("账号文件路径无效")
    if contents is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise CodeBuddyError("无法清理账号事务文件") from exc
        return
    temp_name: str | None = None
    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".rollback.tmp", dir=path.parent
        )
        with os.fdopen(fd, "wb") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
        temp_name = None
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError as exc:
        raise CodeBuddyError("无法恢复账号文件") from exc
    finally:
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except OSError:
                pass


class AccountStore:
    """Store account metadata separately from each credential-bearing session."""

    def __init__(self, root: pathlib.Path | str | None = None) -> None:
        self.root = pathlib.Path(root or (pathlib.Path.home() / ".workbuddy2api" / "accounts"))
        self.root = self.root.expanduser().resolve()
        self.index_file = self.root / "index.json"
        self._lock = _root_lock(self.root)
        self._clients: dict[str, CodeBuddyClient] = {}
        with self._lock:
            try:
                self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.chmod(self.root, 0o700)
            except OSError as exc:
                raise CodeBuddyError("无法创建私有账号目录") from exc
            if self.index_file.is_symlink():
                raise CodeBuddyError("账号索引文件路径无效")
            self._read_index_locked()

    def _session_file(self, account_id: str) -> pathlib.Path:
        if not _ACCOUNT_ID_RE.fullmatch(account_id):
            raise CodeBuddyError("账号 id 无效")
        return self.root / f"account-{account_id}.json"

    def _read_index_locked(self) -> dict[str, Any]:
        try:
            raw = self.index_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"version": _INDEX_VERSION, "active_account_id": None, "accounts": []}
        except OSError as exc:
            raise CodeBuddyError("无法读取账号索引文件") from exc
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CodeBuddyError("账号索引文件损坏，未修改任何账号数据") from exc
        if (
            not isinstance(value, dict)
            or set(value) != {"version", "active_account_id", "accounts"}
            or value.get("version") != _INDEX_VERSION
        ):
            raise CodeBuddyError("账号索引文件格式或版本无效，未修改任何账号数据")
        accounts = value.get("accounts")
        if not isinstance(accounts, list):
            raise CodeBuddyError("账号索引中的 accounts 字段无效")
        normalized_accounts: list[dict[str, Any]] = []
        seen: set[str] = set()
        for record in accounts:
            if not isinstance(record, dict) or set(record) != _INDEX_FIELDS:
                raise CodeBuddyError("账号索引包含无效字段，未修改任何账号数据")
            backend = record.get("backend")
            endpoint = record.get("endpoint")
            uid = record.get("uid")
            account_id = record.get("id")
            nickname = record.get("nickname")
            if not all(isinstance(item, str) and item for item in (backend, endpoint, uid)):
                raise CodeBuddyError("账号索引包含不完整的身份信息")
            try:
                profile_for_key(backend)
                canonical_endpoint = _safe_endpoint(endpoint)
            except (ValueError, CodeBuddyError) as exc:
                raise CodeBuddyError("账号索引包含无效的 backend 或 endpoint") from exc
            if canonical_endpoint != endpoint or _account_id(backend, endpoint, uid) != account_id:
                raise CodeBuddyError("账号索引身份校验失败，未修改任何账号数据")
            if nickname is not None and not isinstance(nickname, str):
                raise CodeBuddyError("账号索引包含无效的 nickname")
            if account_id in seen:
                raise CodeBuddyError("账号索引包含重复账号 id")
            seen.add(account_id)
            normalized_accounts.append(
                {
                    "id": account_id,
                    "backend": backend,
                    "endpoint": endpoint,
                    "uid": uid,
                    "nickname": nickname,
                }
            )
        active_id = value.get("active_account_id")
        if active_id is not None and active_id not in seen:
            raise CodeBuddyError("账号索引的 active_account_id 不存在")
        return {
            "version": _INDEX_VERSION,
            "active_account_id": active_id,
            "accounts": normalized_accounts,
        }

    def _write_index_locked(self, index: dict[str, Any]) -> None:
        metadata = [
            {key: record[key] for key in ("id", "backend", "endpoint", "uid", "nickname")}
            for record in index["accounts"]
        ]
        _atomic_json(
            self.index_file,
            {
                "version": _INDEX_VERSION,
                "active_account_id": index["active_account_id"],
                "accounts": metadata,
            },
        )

    @staticmethod
    def _find_account(index: dict[str, Any], account_id: str) -> dict[str, Any]:
        if not isinstance(account_id, str) or not _ACCOUNT_ID_RE.fullmatch(account_id):
            raise CodeBuddyError("账号 id 无效")
        for record in index["accounts"]:
            if record["id"] == account_id:
                return record
        raise CodeBuddyError("账号不存在")

    @staticmethod
    def _validated_client_session(
        client: CodeBuddyClient,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if not isinstance(client, CodeBuddyClient):
            raise CodeBuddyError("只能导入 CodeBuddyClient 会话")
        session, _ = client._snapshot_session()
        account = session.get("account")
        auth = session.get("auth")
        if (
            not isinstance(account, dict)
            or account.get("uid") is None
            or not str(account.get("uid")).strip()
        ):
            raise CodeBuddyError("账号 session 缺少 uid，不能导入")
        if not _has_auth_token(auth):
            raise CodeBuddyError("账号 session 缺少认证 token，不能导入")
        try:
            client._validate_session_metadata(session)
            backend = client.profile.key
            profile_for_key(backend)
            endpoint = _safe_endpoint(client.endpoint)
        except (ValueError, CodeBuddyError) as exc:
            raise CodeBuddyError("账号 session 的 backend 或 endpoint 无效") from exc
        uid = str(account["uid"]).strip()
        if not uid:
            raise CodeBuddyError("账号 session 缺少 uid，不能导入")
        session["backend"] = backend
        session["endpoint"] = endpoint
        session_account = dict(account)
        session_account["uid"] = uid
        session["account"] = session_account
        metadata = {
            "id": _account_id(backend, endpoint, uid),
            "backend": backend,
            "endpoint": endpoint,
            "uid": uid,
            "nickname": (
                str(account.get("nickname"))[:200]
                if account.get("nickname") is not None
                else None
            ),
        }
        return session, metadata

    def _new_client(self, record: dict[str, Any]) -> CodeBuddyClient:
        session_file = self._session_file(record["id"])
        if session_file.is_symlink():
            raise CodeBuddyError("账号 session 路径无效")
        profile = profile_for_key(record["backend"])
        client = CodeBuddyClient(
            backend=resolve_backend(
                profile=profile,
                endpoint=record["endpoint"],
                session_file=session_file,
                environment={},
            )
        )
        session, _ = client._snapshot_session()
        identity = self._identity_from_session(client, session)
        if identity != record["id"]:
            raise CodeBuddyError("账号 session 身份与索引不一致")
        if not _has_auth_token(session.get("auth")):
            raise CodeBuddyError("账号 session 缺少认证 token")
        return client

    @staticmethod
    def _identity_from_session(client: CodeBuddyClient, session: dict[str, Any]) -> str:
        account = session.get("account") or {}
        uid = account.get("uid") if isinstance(account, dict) else None
        if uid is None or not str(uid).strip():
            raise CodeBuddyError("账号 session 缺少 uid")
        endpoint = _safe_endpoint(client.endpoint)
        return _account_id(client.profile.key, endpoint, str(uid).strip())

    def _get_client_locked(self, record: dict[str, Any]) -> CodeBuddyClient:
        client = self._clients.get(record["id"])
        if client is None:
            client = self._new_client(record)
            self._clients[record["id"]] = client
        return client

    def list_accounts(self) -> list[dict[str, Any]]:
        with self._lock:
            index = self._read_index_locked()
            active_id = index["active_account_id"]
            return [
                {**record, "active": record["id"] == active_id}
                for record in index["accounts"]
            ]

    @property
    def active_account_id(self) -> str | None:
        with self._lock:
            return self._read_index_locked()["active_account_id"]

    def import_client(self, client: CodeBuddyClient, activate: bool = False) -> str:
        session, metadata = self._validated_client_session(client)
        with self._lock:
            index = self._read_index_locked()
            existing = next(
                (record for record in index["accounts"] if record["id"] == metadata["id"]),
                None,
            )
            record = metadata
            session_file = self._session_file(metadata["id"])
            if session_file.is_symlink():
                raise CodeBuddyError("账号 session 路径无效")
            cached = self._clients.get(metadata["id"])
            # Keep cached-client session writers outside the full disk/cache
            # transaction. This covers the rollback interval too, so no writer
            # can persist a newer session between restoring the file and cache.
            session_guard = cached._session_lock if cached is not None else nullcontext()
            with session_guard:
                index_backup = _capture_file(self.index_file)
                session_backup: bytes | None = None
                cached_snapshot: dict[str, Any] | None = None
                session_written = False
                index_write_attempted = False
                try:
                    if cached is None:
                        session_backup = _capture_file(session_file)
                        _atomic_json(session_file, session)
                        session_written = True
                        persistent_client = self._new_client(record)
                    else:
                        persistent_client = cached
                        for _ in range(5):
                            previous_session, generation = cached._snapshot_session()
                            candidate_backup = _capture_file(session_file)
                            if cached._save_session(
                                session, expected_generation=generation
                            ):
                                session_backup = candidate_backup
                                cached_snapshot = previous_session
                                session_written = True
                                break
                        else:
                            raise CodeBuddyError(
                                "账号 session 正在更新，请稍后重试关联"
                            )

                    if existing is None:
                        index["accounts"].append(record)
                    else:
                        position = index["accounts"].index(existing)
                        index["accounts"][position] = record
                    if activate or (existing is None and len(index["accounts"]) == 1):
                        index["active_account_id"] = metadata["id"]
                    index_write_attempted = True
                    self._write_index_locked(index)
                except Exception as original_error:
                    rollback_errors: list[Exception] = []
                    if session_written:
                        try:
                            _restore_file(session_file, session_backup)
                        except Exception as exc:
                            rollback_errors.append(exc)
                    if cached is not None and cached_snapshot is not None:
                        try:
                            cached._restore_session_snapshot(cached_snapshot)
                        except Exception as exc:
                            rollback_errors.append(exc)
                    if index_write_attempted:
                        try:
                            _restore_file(self.index_file, index_backup)
                        except Exception as exc:
                            rollback_errors.append(exc)
                    if rollback_errors:
                        raise CodeBuddyError(
                            "账号关联失败，且事务回滚未能完整恢复"
                        ) from rollback_errors[0]
                    raise original_error
                self._clients[metadata["id"]] = persistent_client
                return metadata["id"]

    def get_client(self, account_id: str) -> CodeBuddyClient:
        with self._lock:
            index = self._read_index_locked()
            record = self._find_account(index, account_id)
            return self._get_client_locked(record)

    def set_active(self, account_id: str) -> CodeBuddyClient:
        with self._lock:
            index = self._read_index_locked()
            record = self._find_account(index, account_id)
            client = self._get_client_locked(record)
            index["active_account_id"] = account_id
            self._write_index_locked(index)
            return client

    def clear_active(self) -> None:
        """Persist an empty active selection while retaining stored accounts."""
        with self._lock:
            index = self._read_index_locked()
            index["active_account_id"] = None
            self._write_index_locked(index)

    def active_client(self) -> CodeBuddyClient | None:
        with self._lock:
            index = self._read_index_locked()
            active_id = index["active_account_id"]
            if active_id is None:
                return None
            record = self._find_account(index, active_id)
            return self._get_client_locked(record)

    def delete_account(self, account_id: str) -> None:
        with self._lock:
            index = self._read_index_locked()
            record = self._find_account(index, account_id)
            if index["active_account_id"] == account_id:
                raise CodeBuddyError("不能删除当前生效账号")
            session_file = self._session_file(record["id"])
            if session_file.is_symlink():
                raise CodeBuddyError("账号 session 路径无效")
            cached = self._clients.get(account_id)
            if cached is not None:
                cached._invalidate_persistence()
            saved_bytes: bytes | None = None
            try:
                if session_file.exists():
                    saved_bytes = session_file.read_bytes()
                    session_file.unlink()
            except OSError as exc:
                if cached is not None:
                    cached._enable_persistence()
                raise CodeBuddyError("无法删除账号 session 文件") from exc
            index["accounts"] = [
                item for item in index["accounts"] if item["id"] != account_id
            ]
            try:
                self._write_index_locked(index)
            except Exception:
                if saved_bytes is not None:
                    temp_name: str | None = None
                    try:
                        fd, temp_name = tempfile.mkstemp(
                            prefix=f".{session_file.name}.",
                            suffix=".restore.tmp",
                            dir=self.root,
                        )
                        with os.fdopen(fd, "wb") as stream:
                            stream.write(saved_bytes)
                            stream.flush()
                            os.fsync(stream.fileno())
                        os.replace(temp_name, session_file)
                        temp_name = None
                    except OSError:
                        pass
                    finally:
                        if temp_name is not None:
                            try:
                                os.unlink(temp_name)
                            except OSError:
                                pass
                if cached is not None:
                    cached._enable_persistence()
                raise
            self._clients.pop(account_id, None)
