"""Account switching and bounded background login management.

Login credentials are first written to a private temporary session file.  The
temporary client is imported into the account store only after authentication
has completed and the job still owns the commit boundary.
"""

from __future__ import annotations

from datetime import timezone
from email.utils import parsedate_to_datetime
import ipaddress
import json
import math
import pathlib
import re
import threading
import time
import unicodedata
from typing import Any, Callable

from codebuddy_proxy.account_store import _atomic_json
from codebuddy_proxy.codebuddy_client_demo import CodeBuddyClient


from buddy_agent_auth.providers.codebuddy.login_jobs import (
    AccountManagerError, WorkBuddyLoginJobs, _client_identity, _client_has_usable_auth,
)


_PUBLIC_ACCOUNT_FIELDS = (
    "id",
    "backend",
    "endpoint",
    "uid",
    "nickname",
    "active",
    "model_cooldowns",
    "checkin",
)
_RUNTIME_STATE_VERSION = 2
_LEGACY_RUNTIME_STATE_VERSION = 1
_RUNTIME_STATE_FILENAME = "account-manager-state-v1.json"
_DEFAULT_COOLDOWN_SECONDS = 60
_RETRY_AFTER_SECONDS_RE = re.compile(r"^[0-9]+$")


class AccountManager(WorkBuddyLoginJobs):
    """Coordinate the account store and bounded, cancellable login jobs."""

    def __init__(
        self,
        store: Any,
        *,
        max_concurrent_logins: int = 2,
        retained_job_limit: int = 100,
        job_ttl_seconds: float = 15 * 60,
        login_timeout: int = 300,
        client_factory: Callable[[str, pathlib.Path], Any] | None = None,
    ) -> None:
        self.store = store
        self._login_client_class = CodeBuddyClient
        self._lock = threading.RLock()
        self._store_lock = threading.RLock()
        self._runtime_lock = threading.RLock()
        self._runtime_state_path = self._find_runtime_state_path()
        self._settings = {
            "auto_switch_on_429": False,
            "default_cooldown_seconds": _DEFAULT_COOLDOWN_SECONDS,
            "auto_checkin_enabled": False,
        }
        self._cooldowns: dict[str, dict[str, dict[str, Any]]] = {}
        self._load_runtime_state()
        self._init_login_jobs(max_concurrent_logins=max_concurrent_logins,
                              retained_job_limit=retained_job_limit,
                              job_ttl_seconds=job_ttl_seconds,
                              login_timeout=login_timeout, client_factory=client_factory)
        from daily_checkin.workbuddy import AccountCheckinService

        self.checkins = AccountCheckinService(self)

    @property
    def client(self) -> Any | None:
        """Return the currently active client, if the store has one."""

        with self._store_lock:
            return self.store.active_client()

    def snapshot(self) -> dict[str, Any]:
        """Return safe account metadata, cooldowns, settings, and server time."""

        with self._store_lock:
            accounts = self.store.list_accounts()
            active_id = self.store.active_account_id
            server_time = time.time()
            with self._runtime_lock:
                safe_accounts = []
                for account in accounts:
                    if not isinstance(account, dict):
                        continue
                    safe = {
                        key: account[key]
                        for key in _PUBLIC_ACCOUNT_FIELDS[:6]
                        if key in account
                    }
                    account_id = account.get("id")
                    if not isinstance(account_id, str):
                        account_id = None
                    cooldowns = self._cooldowns.get(account_id, {})
                    model_cooldowns = []
                    for model, cooldown in sorted(cooldowns.items()):
                        deadline = cooldown.get("deadline")
                        active_cooldown = (
                            isinstance(deadline, (int, float))
                            and deadline > server_time
                        )
                        model_cooldowns.append(
                            {
                                "model": model,
                                "rate_limited": active_cooldown,
                                # Keep the original deadline and source as
                                # history after expiry; only the active flag
                                # and remaining duration change.
                                "cooldown_until": deadline,
                                "cooldown_remaining_seconds": (
                                    max(0, math.ceil(deadline - server_time))
                                    if active_cooldown
                                    else 0
                                ),
                                "last_rate_limited_at": cooldown.get(
                                    "last_rate_limited_at"
                                ),
                                "cooldown_source": cooldown.get("source"),
                            }
                        )
                    safe.update(
                        {
                            "model_cooldowns": model_cooldowns,
                        }
                    )
                    safe_accounts.append(safe)
                settings = dict(self._settings)
        for account in safe_accounts:
            account_id = account.get("id")
            account["checkin"] = self.checkins.snapshot(
                account_id if isinstance(account_id, str) else ""
            )
        return {
            "accounts": safe_accounts,
            "active_account_id": active_id,
            "settings": settings,
            "server_time": server_time,
        }

    def activate(self, account_id: str) -> None:
        """Make an existing account active and persist the choice in the store."""

        with self._store_lock:
            if not self._has_account_locked(account_id):
                raise AccountManagerError("未找到该账号", status_code=404)
            try:
                self.store.set_active(account_id)
            except (KeyError, FileNotFoundError):
                raise AccountManagerError("未找到该账号", status_code=404) from None

    def has_account(self, account_id: str) -> bool:
        """Return whether an account id is present in the persistent store."""

        with self._store_lock:
            return self._has_account_locked(account_id)

    def account_id_for_client(self, client: Any) -> str | None:
        """Find a stored account by its backend, endpoint, and uid identity."""

        identity = _client_identity(client)
        if identity is None:
            return None
        backend, endpoint, uid = identity
        with self._store_lock:
            return self._account_id_for_identity_locked(backend, endpoint, uid)

    def handle_rate_limit(
        self,
        client: Any,
        retry_after: Any,
        *,
        model: str | None = None,
        excluded_account_ids: set[str] | None = None,
        allow_switch: bool = True,
    ) -> Any | None:
        """Record a model-scoped 429 and optionally switch within the same region."""

        model_key = _valid_model_name(model)
        if model_key is None:
            # Billing and other model-less endpoints must not affect chat
            # failover or impose a cooldown on every model for this account.
            return None
        identity = _client_identity(client)
        if identity is None:
            return None
        backend, endpoint, uid = identity
        with self._store_lock:
            account_id = self._account_id_for_identity_locked(backend, endpoint, uid)
            if account_id is None:
                return None
            now = time.time()
            delay, source = _cooldown_delay(retry_after, now)
            deadline = now + delay
            with self._runtime_lock:
                account_cooldowns = self._cooldowns.get(account_id)
                had_account_cooldowns = account_cooldowns is not None
                if account_cooldowns is None:
                    account_cooldowns = {}
                    self._cooldowns[account_id] = account_cooldowns
                had_previous = model_key in account_cooldowns
                previous = account_cooldowns.get(model_key)
                if previous is not None and previous.get("deadline", 0) > now:
                    if previous["deadline"] > deadline:
                        deadline = previous["deadline"]
                        source = previous["source"]
                account_cooldowns[model_key] = {
                    "deadline": deadline,
                    "last_rate_limited_at": now,
                    "source": source,
                }
                try:
                    self._save_runtime_state_locked()
                except Exception:
                    if had_previous:
                        account_cooldowns[model_key] = previous
                    else:
                        account_cooldowns.pop(model_key, None)
                        if not account_cooldowns and not had_account_cooldowns:
                            self._cooldowns.pop(account_id, None)
                    raise

        excluded = {
            item for item in (excluded_account_ids or set()) if isinstance(item, str)
        }
        excluded.add(account_id)
        with self._store_lock:
            with self._runtime_lock:
                if (
                    not allow_switch
                    or not self._settings["auto_switch_on_429"]
                    or self.store.active_account_id != account_id
                ):
                    return None
                accounts = self.store.list_accounts()
                for candidate in accounts:
                    if not isinstance(candidate, dict):
                        continue
                    candidate_id = candidate.get("id")
                    if (
                        not isinstance(candidate_id, str)
                        or candidate_id in excluded
                        or candidate.get("backend") != backend
                        or candidate.get("endpoint") != endpoint
                    ):
                        continue
                    cooldown = self._cooldowns.get(candidate_id, {}).get(model_key)
                    if (
                        cooldown is not None
                        and cooldown.get("deadline", 0) > time.time()
                    ):
                        continue
                    try:
                        candidate_client = self.store.get_client(candidate_id)
                    except Exception:
                        # A broken/missing session is not an eligible failover.
                        continue
                    if not _client_has_usable_auth(candidate_client):
                        continue
                    # This is serialized with explicit activate() calls under
                    # _store_lock; a manual selection can never be displaced
                    # by a delayed response from a previously active client.
                    return self.store.set_active(candidate_id)
        return None

    def get_settings(self) -> dict[str, Any]:
        with self._runtime_lock:
            return dict(self._settings)

    def set_settings(
        self,
        auto_switch_on_429: bool | None = None,
        *,
        auto_checkin_enabled: bool | None = None,
    ) -> dict[str, Any]:
        if auto_switch_on_429 is None and auto_checkin_enabled is None:
            raise AccountManagerError("至少需要设置一个账号管理开关", status_code=400)
        if auto_switch_on_429 is not None and type(auto_switch_on_429) is not bool:
            raise AccountManagerError("auto_switch_on_429 必须为布尔值", status_code=400)
        if auto_checkin_enabled is not None and type(auto_checkin_enabled) is not bool:
            raise AccountManagerError("auto_checkin_enabled 必须为布尔值", status_code=400)
        with self._runtime_lock:
            previous = dict(self._settings)
            if auto_switch_on_429 is not None:
                self._settings["auto_switch_on_429"] = auto_switch_on_429
            if auto_checkin_enabled is not None:
                self._settings["auto_checkin_enabled"] = auto_checkin_enabled
            try:
                self._save_runtime_state_locked()
            except Exception:
                self._settings = previous
                raise
            updated = dict(self._settings)
            checkin_transition = (
                previous["auto_checkin_enabled"]
                != updated["auto_checkin_enabled"]
            )
        if checkin_transition:
            self.checkins.settings_changed(updated["auto_checkin_enabled"])
        return updated

    def delete(self, account_id: str) -> None:
        """Delete an inactive account."""

        with self._store_lock:
            if not self._has_account_locked(account_id):
                raise AccountManagerError("未找到该账号", status_code=404)
            if self.store.active_account_id == account_id:
                raise AccountManagerError("请先切换到其他账号，再删除此账号", status_code=409)

            # Remove persisted runtime state first.  If this write fails, the
            # account remains untouched; otherwise a deleted identity could
            # retain a cooldown on disk and inherit it if re-imported later.
            removed_cooldown: dict[str, Any] | None = None
            with self._runtime_lock:
                removed_cooldown = self._cooldowns.pop(account_id, None)
                if removed_cooldown is not None:
                    try:
                        self._save_runtime_state_locked()
                    except Exception:
                        self._cooldowns[account_id] = removed_cooldown
                        raise

            try:
                self.store.delete_account(account_id)
            except (KeyError, FileNotFoundError):
                self._restore_deleted_account_cooldown(account_id, removed_cooldown)
                raise AccountManagerError("未找到该账号", status_code=404) from None
            except Exception:
                self._restore_deleted_account_cooldown(account_id, removed_cooldown)
                raise

    def _restore_deleted_account_cooldown(
        self, account_id: str, cooldown: dict[str, Any] | None
    ) -> None:
        if cooldown is None:
            return
        with self._runtime_lock:
            self._cooldowns[account_id] = cooldown
            self._save_runtime_state_locked()

    def _account_id_for_identity_locked(
        self, backend: str, endpoint: str, uid: str
    ) -> str | None:
        for account in self.store.list_accounts():
            if (
                isinstance(account, dict)
                and account.get("backend") == backend
                and account.get("endpoint") == endpoint
                and str(account.get("uid", "")).strip() == uid
            ):
                account_id = account.get("id")
                if isinstance(account_id, str):
                    return account_id
        return None

    def _find_runtime_state_path(self) -> pathlib.Path | None:
        root = getattr(self.store, "root", None)
        if root is None:
            return None
        try:
            return pathlib.Path(root).expanduser() / _RUNTIME_STATE_FILENAME
        except (TypeError, ValueError, OSError) as exc:
            raise AccountManagerError("账号管理状态目录无效，未修改任何数据", status_code=503) from exc

    def _load_runtime_state(self) -> None:
        path = self._runtime_state_path
        if path is None:
            return
        if path.is_symlink():
            raise AccountManagerError("账号管理状态文件无效，未修改任何数据", status_code=503)
        try:
            value = json.loads(
                path.read_text(encoding="utf-8"),
                object_pairs_hook=_unique_runtime_object,
            )
        except FileNotFoundError:
            return
        except (
            OSError,
            json.JSONDecodeError,
            UnicodeDecodeError,
            _DuplicateRuntimeStateKey,
            RecursionError,
        ):
            raise AccountManagerError("账号管理状态文件损坏，未修改任何数据", status_code=503) from None
        if not isinstance(value, dict) or set(value) != {"version", "settings", "cooldowns"}:
            raise AccountManagerError("账号管理状态文件损坏，未修改任何数据", status_code=503)
        version = value.get("version")
        if type(version) is not int or version not in (
            _LEGACY_RUNTIME_STATE_VERSION,
            _RUNTIME_STATE_VERSION,
        ):
            raise AccountManagerError("账号管理状态文件版本不受支持，未修改任何数据", status_code=503)
        settings = value.get("settings")
        setting_fields = set(settings) if isinstance(settings, dict) else set()
        legacy_fields = {"auto_switch_on_429", "default_cooldown_seconds"}
        current_fields = legacy_fields | {"auto_checkin_enabled"}
        if (
            not isinstance(settings, dict)
            or setting_fields not in (
                (legacy_fields, current_fields)
                if version == _LEGACY_RUNTIME_STATE_VERSION
                else (current_fields,)
            )
            or type(settings.get("auto_switch_on_429")) is not bool
            or type(settings.get("default_cooldown_seconds")) is not int
            or settings.get("default_cooldown_seconds") != _DEFAULT_COOLDOWN_SECONDS
            or (
                "auto_checkin_enabled" in settings
                and type(settings.get("auto_checkin_enabled")) is not bool
            )
        ):
            raise AccountManagerError("账号管理状态文件损坏，未修改任何数据", status_code=503)
        cooldowns = value.get("cooldowns")
        if not isinstance(cooldowns, dict):
            raise AccountManagerError("账号管理状态文件损坏，未修改任何数据", status_code=503)
        loaded: dict[str, dict[str, dict[str, Any]]] = {}
        if version == _LEGACY_RUNTIME_STATE_VERSION:
            # A legacy account-wide cooldown cannot safely be assigned to a
            # particular model.  Validate its full shape, then discard it.
            for account_id, record in cooldowns.items():
                if (
                    not isinstance(account_id, str)
                    or not account_id
                    or len(account_id) > 256
                    or not isinstance(record, dict)
                    or set(record) != {"deadline", "last_rate_limited_at", "source"}
                    or not _finite_number(record.get("deadline"))
                    or not _finite_number(record.get("last_rate_limited_at"))
                    or record.get("source") not in ("retry_after", "default")
                ):
                    raise AccountManagerError(
                        "账号管理状态文件损坏，未修改任何数据", status_code=503
                    )
        else:
            for account_id, model_records in cooldowns.items():
                if (
                    not isinstance(account_id, str)
                    or not account_id
                    or len(account_id) > 256
                    or not isinstance(model_records, dict)
                ):
                    raise AccountManagerError(
                        "账号管理状态文件损坏，未修改任何数据", status_code=503
                    )
                loaded_records: dict[str, dict[str, Any]] = {}
                for model, record in model_records.items():
                    if (
                        _valid_model_name(model) != model
                        or not isinstance(record, dict)
                        or set(record)
                        != {"deadline", "last_rate_limited_at", "source"}
                        or not _finite_number(record.get("deadline"))
                        or not _finite_number(record.get("last_rate_limited_at"))
                        or record.get("source") not in ("retry_after", "default")
                    ):
                        raise AccountManagerError(
                            "账号管理状态文件损坏，未修改任何数据", status_code=503
                        )
                    loaded_records[model] = {
                        "deadline": float(record["deadline"]),
                        "last_rate_limited_at": float(record["last_rate_limited_at"]),
                        "source": record["source"],
                    }
                if loaded_records:
                    loaded[account_id] = loaded_records
        self._settings = {
            "auto_switch_on_429": settings["auto_switch_on_429"],
            "default_cooldown_seconds": _DEFAULT_COOLDOWN_SECONDS,
            "auto_checkin_enabled": settings.get("auto_checkin_enabled", False),
        }
        self._cooldowns = loaded

    def _save_runtime_state_locked(self) -> None:
        path = self._runtime_state_path
        if path is None:
            return
        payload = {
            "version": _RUNTIME_STATE_VERSION,
            "settings": dict(self._settings),
            "cooldowns": {
                account_id: {
                    model: dict(record)
                    for model, record in model_records.items()
                }
                for account_id, model_records in self._cooldowns.items()
                if model_records
            },
        }
        try:
            _atomic_json(path, payload)
        except Exception:
            # Keep private paths and low-level storage details out of manager
            # responses. The caller sees only a fixed error message.
            raise AccountManagerError("无法保存账号管理状态", status_code=500) from None


    def _has_account_locked(self, account_id: str) -> bool:
        return any(
            isinstance(account, dict) and account.get("id") == account_id
            for account in self.store.list_accounts()
        )

    def close(self) -> None:
        self.checkins.stop()
        self._close_logins()


class _DuplicateRuntimeStateKey(ValueError):
    pass


def _unique_runtime_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateRuntimeStateKey("duplicate runtime state key")
        value[key] = item
    return value


def _finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        return False


def _valid_model_name(model: Any) -> str | None:
    """Return a model identifier only when it is safe to key and persist."""

    if not isinstance(model, str) or not model or len(model) > 256:
        return None
    if not model.strip() or any(
        unicodedata.category(char) in {"Cc", "Cs"} for char in model
    ):
        return None
    return model


def _cooldown_delay(retry_after: Any, now: float) -> tuple[float, str]:
    seconds: float | None = None
    if isinstance(retry_after, int) and not isinstance(retry_after, bool) and retry_after >= 0:
        try:
            seconds = float(retry_after)
        except OverflowError:
            seconds = None
    elif isinstance(retry_after, str):
        value = retry_after.strip()
        if _RETRY_AFTER_SECONDS_RE.fullmatch(value):
            try:
                seconds = float(int(value))
            except (OverflowError, ValueError):
                seconds = None
        else:
            try:
                parsed = parsedate_to_datetime(value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                target = parsed.astimezone(timezone.utc).timestamp()
                if math.isfinite(target):
                    seconds = max(0.0, target - now)
            except (AttributeError, TypeError, ValueError, OverflowError, OSError):
                seconds = None
    if seconds is None or not math.isfinite(seconds):
        return float(_DEFAULT_COOLDOWN_SECONDS), "default"
    return seconds, "retry_after"


def is_loopback_address(value: str | None) -> bool:
    """Return whether a peer address is a literal loopback IP address."""

    if not value:
        return False
    try:
        address = ipaddress.ip_address(value)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            return address.ipv4_mapped.is_loopback
        return address.is_loopback
    except ValueError:
        return False
