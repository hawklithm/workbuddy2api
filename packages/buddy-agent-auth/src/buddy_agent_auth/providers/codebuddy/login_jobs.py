"""Reusable, bounded WorkBuddy background authentication jobs."""
from __future__ import annotations
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
import pathlib
import tempfile
import threading
import time
import uuid
from typing import Any
from urllib.parse import urlsplit
from .backend_profile import profile_for_key, resolve_backend
from .client import CodeBuddyClient, CodeBuddyError

class AccountManagerError(CodeBuddyError):
    """Expected account error safe for a local management interface."""
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code

_TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
_PUBLIC_JOB_FIELDS = ("id", "backend", "status", "auth_url", "error", "account_id")

@dataclass(slots=True)
class _LoginJob:
    id: str
    backend: str
    created_at: float
    target_account_id: str | None = None
    target_endpoint: str | None = None
    status: str = "pending"
    auth_url: str | None = None
    error: str | None = None
    account_id: str | None = None
    updated_at: float = field(default_factory=time.monotonic)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    lock: threading.RLock = field(default_factory=threading.RLock)
    future: Future[Any] | None = None


class WorkBuddyLoginJobs:
    def _init_login_jobs(self, *, max_concurrent_logins=2, retained_job_limit=100,
                         job_ttl_seconds=15 * 60, login_timeout=300, client_factory=None):
        if max_concurrent_logins < 1:
            raise ValueError("max_concurrent_logins must be positive")
        if retained_job_limit < 1:
            raise ValueError("retained_job_limit must be positive")
        self._max_concurrent_logins = max_concurrent_logins
        self._retained_job_limit = retained_job_limit
        self._job_ttl_seconds = max(0.0, job_ttl_seconds)
        self._login_timeout = max(1, int(login_timeout))
        self._client_factory = client_factory or self._create_login_client
        self._closed = False
        self._jobs = {}
        self._executor = ThreadPoolExecutor(max_workers=max_concurrent_logins,
                                            thread_name_prefix="codebuddy-login")

    def start_login(self, backend: str, account_id: str | None = None) -> dict[str, Any]:
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

            target = None
            if account_id is not None:
                with self._store_lock:
                    target = next((a for a in self.store.list_accounts() if a["id"] == account_id), None)
                if target is None:
                    raise AccountManagerError("未找到该账号", status_code=404)
                if target["backend"] != backend:
                    raise AccountManagerError("账号区域不匹配", status_code=400)
            job = _LoginJob(id=uuid.uuid4().hex, backend=backend, created_at=now,
                            target_account_id=account_id, target_endpoint=target["endpoint"] if target else None)
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

    def _close_logins(self) -> None:
        """Cancel outstanding jobs and stop the bounded worker pool."""

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

    def _create_login_client(self, backend: str, session_file: pathlib.Path) -> CodeBuddyClient:
        profile = profile_for_key(backend)
        resolved = resolve_backend(
            profile=profile,
            session_file=session_file,
            environment={},
        )
        return getattr(self, '_login_client_class', CodeBuddyClient)(backend=resolved)

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
                if job.target_endpoint is not None:
                    client.endpoint = job.target_endpoint
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
                    accounts = self.store.list_accounts()
                    if job.target_account_id is not None:
                        target = next((a for a in accounts if a["id"] == job.target_account_id), None)
                        uid = str(client.session["account"]["uid"]).strip()
                        if target is None or target["backend"] != job.backend or target["endpoint"] != client.endpoint or target["uid"] != uid:
                            job.status = "failed"
                            job.error = "登录账号与所选账号不一致，或所选账号已删除，请重试"
                            job.updated_at = time.monotonic()
                            return
                    should_activate = not bool(accounts)
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


class WorkBuddyLoginManager(WorkBuddyLoginJobs):
    """Standalone login coordinator for an authentication AccountStore."""
    def __init__(self, store, **options):
        self.store = store
        self._lock = threading.RLock()
        self._store_lock = threading.RLock()
        self._init_login_jobs(**options)

    def close(self):
        self._close_logins()


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
