"""Account switching and bounded background login management.

Login credentials are first written to a private temporary session file.  The
temporary client is imported into the account store only after authentication
has completed and the job still owns the commit boundary.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import timezone
from email.utils import parsedate_to_datetime
import ipaddress
import json
import math
import pathlib
import re
import tempfile
import threading
import time
import uuid
from typing import Any, Callable
from urllib.parse import urlsplit

from codebuddy_proxy.account_store import _atomic_json
from codebuddy_proxy.backend_profile import profile_for_key, resolve_backend
from codebuddy_proxy.codebuddy_client_demo import CodeBuddyClient, CodeBuddyError


class AccountManagerError(CodeBuddyError):
    """An expected account-management error safe to show to a local user."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


_TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
_PUBLIC_JOB_FIELDS = ("id", "backend", "status", "auth_url", "error", "account_id")
_PUBLIC_ACCOUNT_FIELDS = (
    "id",
    "backend",
    "endpoint",
    "uid",
    "nickname",
    "active",
    "rate_limited",
    "cooldown_until",
    "cooldown_remaining_seconds",
    "last_rate_limited_at",
    "cooldown_source",
    "checkin",
)
_RUNTIME_STATE_VERSION = 1
_RUNTIME_STATE_FILENAME = "account-manager-state-v1.json"
_DEFAULT_COOLDOWN_SECONDS = 60
_RETRY_AFTER_SECONDS_RE = re.compile(r"^[0-9]+$")


@dataclass(slots=True)
class _LoginJob:
    id: str
    backend: str
    created_at: float
    status: str = "pending"
    auth_url: str | None = None
    error: str | None = None
    account_id: str | None = None
    updated_at: float = field(default_factory=time.monotonic)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    lock: threading.RLock = field(default_factory=threading.RLock)
    future: Future[Any] | None = None


class AccountManager:
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
        if max_concurrent_logins < 1:
            raise ValueError("max_concurrent_logins must be positive")
        if retained_job_limit < 1:
            raise ValueError("retained_job_limit must be positive")
        self.store = store
        self._max_concurrent_logins = max_concurrent_logins
        self._retained_job_limit = retained_job_limit
        self._job_ttl_seconds = max(0.0, job_ttl_seconds)
        self._login_timeout = max(1, int(login_timeout))
        self._client_factory = client_factory or self._create_login_client
        self._lock = threading.RLock()
        self._store_lock = threading.RLock()
        self._runtime_lock = threading.RLock()
        self._runtime_state_path = self._find_runtime_state_path()
        self._settings = {
            "auto_switch_on_429": False,
            "default_cooldown_seconds": _DEFAULT_COOLDOWN_SECONDS,
            "auto_checkin_enabled": False,
        }
        self._cooldowns: dict[str, dict[str, Any]] = {}
        self._load_runtime_state()
        self._closed = False
        self._jobs: dict[str, _LoginJob] = {}
        self._executor = ThreadPoolExecutor(
            max_workers=max_concurrent_logins,
            thread_name_prefix="codebuddy-login",
        )
        from codebuddy_proxy.account_checkin import AccountCheckinService

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
                    cooldown = self._cooldowns.get(account_id)
                    deadline = cooldown.get("deadline") if cooldown else None
                    active_cooldown = (
                        cooldown is not None
                        and isinstance(deadline, (int, float))
                        and deadline > server_time
                    )
                    safe.update(
                        {
                            "rate_limited": active_cooldown,
                            "cooldown_until": deadline if active_cooldown else None,
                            "cooldown_remaining_seconds": (
                                max(0, math.ceil(deadline - server_time))
                                if active_cooldown
                                else 0
                            ),
                            "last_rate_limited_at": (
                                cooldown.get("last_rate_limited_at") if cooldown else None
                            ),
                            "cooldown_source": (
                                cooldown.get("source") if active_cooldown else None
                            ),
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
        excluded_account_ids: set[str] | None = None,
        allow_switch: bool = True,
    ) -> Any | None:
        """Record a 429 cooldown and optionally switch within the same region."""

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
                previous = self._cooldowns.get(account_id)
                if previous is not None and previous.get("deadline", 0) > now:
                    if previous["deadline"] > deadline:
                        deadline = previous["deadline"]
                        source = previous["source"]
                self._cooldowns[account_id] = {
                    "deadline": deadline,
                    "last_rate_limited_at": now,
                    "source": source,
                }
                self._save_runtime_state_locked()

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
                    cooldown = self._cooldowns.get(candidate_id)
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
        if (
            type(value.get("version")) is not int
            or value.get("version") != _RUNTIME_STATE_VERSION
        ):
            raise AccountManagerError("账号管理状态文件版本不受支持，未修改任何数据", status_code=503)
        settings = value.get("settings")
        if (
            not isinstance(settings, dict)
            or set(settings)
            not in (
                {"auto_switch_on_429", "default_cooldown_seconds"},
                {
                    "auto_switch_on_429",
                    "default_cooldown_seconds",
                    "auto_checkin_enabled",
                },
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
        loaded: dict[str, dict[str, Any]] = {}
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
                raise AccountManagerError("账号管理状态文件损坏，未修改任何数据", status_code=503)
            loaded[account_id] = {
                "deadline": float(record["deadline"]),
                "last_rate_limited_at": float(record["last_rate_limited_at"]),
                "source": record["source"],
            }
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
                account_id: dict(record)
                for account_id, record in self._cooldowns.items()
            },
        }
        try:
            _atomic_json(path, payload)
        except Exception:
            # Keep private paths and low-level storage details out of manager
            # responses. The caller sees only a fixed error message.
            raise AccountManagerError("无法保存账号管理状态", status_code=500) from None


    def start_login(self, backend: str) -> dict[str, Any]:
        """Start a login flow and return its opaque job identifier."""

        if backend not in {"domestic", "global"}:
            raise AccountManagerError("backend 必须为 domestic 或 global", status_code=400)

        now = time.monotonic()
        with self._lock:
            if self._closed:
                raise AccountManagerError("账号管理器已关闭", status_code=503)
            self._reap_jobs_locked(now)
            active_workers = sum(
                job.future is None or not job.future.done()
                for job in self._jobs.values()
            )
            if active_workers >= self._max_concurrent_logins:
                raise AccountManagerError("当前登录任务较多，请稍后重试", status_code=429)

            job = _LoginJob(id=uuid.uuid4().hex, backend=backend, created_at=now)
            self._jobs[job.id] = job
            try:
                job.future = self._executor.submit(self._run_login, job)
            except RuntimeError:
                self._jobs.pop(job.id, None)
                raise AccountManagerError("账号管理器已关闭", status_code=503) from None
        return self._public_job(job)

    def get_login(self, job_id: str) -> dict[str, Any]:
        job = self._find_job(job_id)
        return self._public_job(job)

    def cancel_login(self, job_id: str) -> dict[str, Any]:
        job = self._find_job(job_id)
        with job.lock:
            if job.status == "pending":
                # This transition is the cancellation linearization point.  A
                # worker that finishes after it cannot import into the store.
                job.cancel_event.set()
                job.status = "cancelled"
                job.error = None
                job.updated_at = time.monotonic()
            return self._public_job_unlocked(job)

    def close(self) -> None:
        """Cancel outstanding jobs and stop the bounded worker pool."""

        checkins = getattr(self, "checkins", None)
        if checkins is not None:
            checkins.stop()
        with self._lock:
            if self._closed:
                return
            # The manager lock also guards the commit boundary.  If a worker
            # already owns it, its commit finishes first; otherwise close wins.
            self._closed = True
            jobs = list(self._jobs.values())

        for job in jobs:
            with job.lock:
                if job.status == "pending":
                    job.cancel_event.set()
                    job.status = "cancelled"
                    job.error = None
                    job.updated_at = time.monotonic()

        self._executor.shutdown(wait=True, cancel_futures=True)

    def _has_account_locked(self, account_id: str) -> bool:
        return any(
            isinstance(account, dict) and account.get("id") == account_id
            for account in self.store.list_accounts()
        )

    def _create_login_client(self, backend: str, session_file: pathlib.Path) -> CodeBuddyClient:
        profile = profile_for_key(backend)
        resolved = resolve_backend(
            profile=profile,
            session_file=session_file,
            environment={},
        )
        return CodeBuddyClient(backend=resolved)

    def _temporary_directory(self) -> tempfile.TemporaryDirectory[str]:
        root = getattr(self.store, "root", None)
        temp_parent: str | None = None
        if root is not None:
            try:
                root_path = pathlib.Path(root).expanduser()
                if root_path.is_dir():
                    temp_parent = str(root_path)
            except (OSError, TypeError, ValueError):
                temp_parent = None
        return tempfile.TemporaryDirectory(prefix="workbuddy-login-", dir=temp_parent)

    def _run_login(self, job: _LoginJob) -> None:
        with job.lock:
            if job.status != "pending" or job.cancel_event.is_set():
                return

        try:
            with self._temporary_directory() as temp_directory:
                session_file = pathlib.Path(temp_directory) / "session.json"
                client = self._client_factory(job.backend, session_file)
                with job.lock:
                    if job.status != "pending" or job.cancel_event.is_set():
                        return
                challenge = client.begin_login()
                auth_url, state = self._read_challenge(challenge)

                with job.lock:
                    if job.status != "pending" or job.cancel_event.is_set():
                        return
                    job.auth_url = auth_url
                    job.updated_at = time.monotonic()

                client.complete_login(
                    {"auth_url": auth_url, "state": state},
                    timeout=self._login_timeout,
                    cancel_event=job.cancel_event,
                )

                session = getattr(client, "session", None)
                account = session.get("account") if isinstance(session, dict) else None
                if not isinstance(account, dict) or not account.get("uid"):
                    raise ValueError("login did not produce a valid account")

                self._commit_login(job, client)
        except Exception as exc:
            self._finish_failure(job, exc)

    @staticmethod
    def _read_challenge(challenge: Any) -> tuple[str, str]:
        if not isinstance(challenge, dict):
            raise ValueError("invalid login challenge")
        auth_url = challenge.get("auth_url")
        state = challenge.get("state")
        if (
            not isinstance(auth_url, str)
            or len(auth_url) > 4096
            or any(ord(char) < 0x21 or ord(char) == 0x7F for char in auth_url)
        ):
            raise ValueError("invalid login URL")
        parsed = urlsplit(auth_url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise ValueError("invalid login URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("invalid login URL")
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("invalid login URL")
        if not isinstance(state, str) or not state or len(state) > 4096:
            raise ValueError("invalid login state")
        return auth_url, state

    def _commit_login(self, job: _LoginJob, client: Any) -> None:
        with job.lock:
            if job.status != "pending" or job.cancel_event.is_set():
                return
            # Holding the manager lock through import gives close() and
            # cancellation a deterministic boundary relative to persistence.
            with self._lock:
                if self._closed or job.cancel_event.is_set():
                    self._mark_cancelled_unlocked(job)
                    return
                with self._store_lock:
                    should_activate = not bool(self.store.list_accounts())
                    account_id = self.store.import_client(client, activate=should_activate)
                if not isinstance(account_id, str) or not account_id:
                    raise ValueError("account store returned an invalid identifier")
                job.account_id = account_id
                job.status = "succeeded"
                job.error = None
                job.updated_at = time.monotonic()

    def _finish_failure(self, job: _LoginJob, exc: Exception) -> None:
        with job.lock:
            if job.status != "pending":
                return
            if job.cancel_event.is_set():
                self._mark_cancelled_unlocked(job)
                return
            if isinstance(exc, TimeoutError):
                message = "登录超时，请重新尝试"
            elif isinstance(exc, (ValueError, TypeError)):
                message = "登录信息无效，请重新尝试"
            else:
                # Do not expose provider text, paths, response bodies, or
                # exception representations through the local management API.
                message = "登录失败，请稍后重新尝试"
            job.status = "failed"
            job.error = message
            job.updated_at = time.monotonic()

    @staticmethod
    def _mark_cancelled_unlocked(job: _LoginJob) -> None:
        job.cancel_event.set()
        job.status = "cancelled"
        job.error = None
        job.updated_at = time.monotonic()

    def _find_job(self, job_id: str) -> _LoginJob:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise AccountManagerError("未找到该登录任务", status_code=404)
        return job

    def _reap_jobs_locked(self, now: float) -> None:
        # Job state is monotonic: pending can only become terminal.  Read-only
        # status/timestamp inspection here is safe while the registry lock is
        # held and avoids inverting the per-job -> registry lock order.
        for job_id, job in list(self._jobs.items()):
            future_done = job.future is not None and job.future.done()
            if (
                job.status in _TERMINAL
                and future_done
                and now - job.updated_at >= self._job_ttl_seconds
            ):
                self._jobs.pop(job_id, None)

        terminal = sorted(
            (
                job
                for job in self._jobs.values()
                if job.status in _TERMINAL
                and job.future is not None
                and job.future.done()
            ),
            key=lambda job: job.updated_at,
        )
        while len(terminal) > self._retained_job_limit:
            self._jobs.pop(terminal.pop(0).id, None)

        max_jobs = self._retained_job_limit + self._max_concurrent_logins
        while len(self._jobs) >= max_jobs:
            oldest = next((job for job in terminal if job.id in self._jobs), None)
            if oldest is None:
                break
            self._jobs.pop(oldest.id, None)
            terminal.remove(oldest)

    def _public_job(self, job: _LoginJob) -> dict[str, Any]:
        with job.lock:
            return self._public_job_unlocked(job)

    @staticmethod
    def _public_job_unlocked(job: _LoginJob) -> dict[str, Any]:
        values = {
            "id": job.id,
            "backend": job.backend,
            "status": job.status,
            "auth_url": job.auth_url,
            "error": job.error,
            "account_id": job.account_id,
        }
        return {key: values[key] for key in _PUBLIC_JOB_FIELDS}


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


def _client_identity(client: Any) -> tuple[str, str, str] | None:
    if client is None:
        return None
    profile = getattr(client, "profile", None)
    backend = getattr(profile, "key", None)
    if not isinstance(backend, str):
        backend_value = getattr(client, "backend", None)
        backend = (
            backend_value
            if isinstance(backend_value, str)
            else getattr(getattr(backend_value, "profile", None), "key", None)
        )
    endpoint = getattr(client, "endpoint", None)
    try:
        session = _client_session(client)
    except Exception:
        return None
    account = session.get("account") if isinstance(session, dict) else None
    uid = account.get("uid") if isinstance(account, dict) else None
    if (
        not isinstance(backend, str)
        or not backend
        or not isinstance(endpoint, str)
        or not endpoint
        or uid is None
    ):
        return None
    uid_text = str(uid).strip()
    if not uid_text:
        return None
    return backend, endpoint, uid_text


def _client_session(client: Any) -> dict[str, Any]:
    snapshot = getattr(client, "_snapshot_session", None)
    if callable(snapshot):
        value = snapshot()
        if isinstance(value, tuple) and value:
            value = value[0]
    else:
        value = getattr(client, "session", None)
    return value if isinstance(value, dict) else {}


def _client_has_usable_auth(client: Any) -> bool:
    try:
        session = _client_session(client)
    except Exception:
        return False
    auth = session.get("auth")
    if not isinstance(auth, dict):
        return False
    access_token = auth.get("accessToken")
    if isinstance(access_token, str) and access_token.strip():
        expires_at = auth.get("expiresAt")
        try:
            expires_ms = int(expires_at or 0)
        except (TypeError, ValueError, OverflowError):
            expires_ms = 1
        if not expires_ms or expires_ms > int(time.time() * 1000) + 60_000:
            return True
    refresh_token = auth.get("refreshToken")
    return isinstance(refresh_token, str) and bool(refresh_token.strip())


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
