"""Per-account daily check-in automation with fail-closed status handling."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import date, datetime, time, timedelta, timezone
import json
import logging
import random
import threading
import time as time_module
from typing import Any

from fastapi.responses import Response

from codebuddy_proxy.billing_proxy import forward_billing


BEIJING = timezone(timedelta(hours=8), name="Asia/Shanghai")
# The legacy checkin-status endpoint can return an empty, inactive activity.
STATUS_PATH = "/v2/billing/meter/checkin-activity-status"
CHECKIN_PATH = "/v2/billing/meter/daily-checkin"
_SAFE_ERRORS = frozenset(
    {
        "签到活动未开放",
        "签到状态查询失败",
        "签到请求失败",
        "签到结果未确认",
        "账号不存在",
    }
)
_REASONS = frozenset({"manual", "startup", "enable", "midnight", "timer"})


class _BillingAdapterState:
    """Bind billing_proxy to one saved client without its global failover hook."""

    managed_auth = True
    account_runtime = None
    logger = None
    json_logger = None

    def __init__(self, client: Any, diagnostic: Callable[..., Any]) -> None:
        self.client = client
        self._diagnostic = diagnostic

    def ensure_auth(self) -> None:
        self.client.ensure_authenticated(open_browser=False, allow_login=False)


Requester = Callable[[Any, str, str], Awaitable[Response]]
Clock = Callable[[], datetime | float | int]
Sleep = Callable[[float], Awaitable[Any]]


class AccountCheckinService:
    """Check and optionally sign each account without changing the active one.

    ``clock`` returns a datetime or Unix timestamp. ``random_delay`` returns a
    delay in seconds in the midnight window. Both hooks, plus ``sleep`` and
    ``requester``, exist so timer behavior can be tested without waiting.
    """

    def __init__(
        self,
        manager: Any,
        *,
        clock: Clock | None = None,
        random_delay: Callable[[], float] | None = None,
        sleep: Sleep | None = None,
        requester: Requester | None = None,
        max_concurrent: int = 4,
    ) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be positive")
        self.manager = manager
        self._clock = clock or self._system_clock
        self._random_delay = random_delay or (lambda: random.uniform(0.0, 600.0))
        self._sleep = sleep or asyncio.sleep
        self._requester = requester or self._forward_request
        self._semaphore = asyncio.Semaphore(max_concurrent)

        self._state_guard = threading.RLock()
        self._states: dict[str, dict[str, Any]] = {}
        self._account_locks: dict[str, asyncio.Lock] = {}
        self._post_started: set[tuple[str, str]] = set()
        self._logging_callback: Callable[..., Any] | None = None

        self._loop: asyncio.AbstractEventLoop | None = None
        self._started = False
        self._closed = False
        self._stop_event = threading.Event()
        self._scheduler_task: asyncio.Task[Any] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._task_keys: dict[asyncio.Task[Any], tuple[str, str] | None] = {}
        self._random_tasks: dict[tuple[str, str], asyncio.Task[Any]] = {}

    @staticmethod
    def _system_clock() -> datetime:
        return datetime.now(timezone.utc).astimezone(BEIJING)

    def _now(self) -> datetime:
        value = self._clock()
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=BEIJING)
            return value.astimezone(BEIJING)
        return datetime.fromtimestamp(float(value), BEIJING)

    def _epoch(self) -> float:
        return self._now().timestamp()

    def _today(self) -> str:
        return self._now().date().isoformat()

    def configure_logging(self, callback: Callable[..., Any] | None) -> None:
        self._logging_callback = callback

    def _log(self, event: str, **fields: Any) -> None:
        safe = {
            key: value
            for key, value in fields.items()
            if key in {
                "account_id",
                "date",
                "reason",
                "status",
                "http_status",
                "next_checkin_at",
            }
            and (value is None or isinstance(value, (str, int, float, bool)))
        }
        try:
            if self._logging_callback is not None:
                self._logging_callback(event, **safe)
            else:
                logging.getLogger(__name__).info("%s %s", event, safe)
        except Exception:
            # Diagnostics must never change authentication or check-in behavior.
            logging.getLogger(__name__).debug("check-in diagnostic callback failed")

    async def _forward_request(self, client: Any, method: str, path: str) -> Response:
        state = _BillingAdapterState(client, self._log)
        return await forward_billing(state, method, path, self._log)

    def snapshot(self, account_id: str) -> dict[str, Any]:
        """Return the cached state only; never make an upstream request here."""

        today = self._today()
        with self._state_guard:
            current = self._states.get(account_id)
            if current is None or current["date"] != today:
                return self._empty_state(today)
            return dict(current)

    @staticmethod
    def _empty_state(target_date: str) -> dict[str, Any]:
        return {
            "date": target_date,
            "status": "unknown",
            "checked": None,
            "last_checked_at": None,
            "last_checkin_at": None,
            "error": None,
            "next_checkin_at": None,
        }

    def _state(self, account_id: str) -> dict[str, Any]:
        today = self._today()
        with self._state_guard:
            current = self._states.get(account_id)
            if current is None or current["date"] != today:
                current = self._empty_state(today)
                self._states[account_id] = current
            return current

    def _account_lock(self, account_id: str) -> asyncio.Lock:
        with self._state_guard:
            lock = self._account_locks.get(account_id)
            if lock is None:
                lock = asyncio.Lock()
                self._account_locks[account_id] = lock
            return lock

    def _has_account(self, account_id: str) -> bool:
        has_account = getattr(self.manager, "has_account", None)
        if callable(has_account):
            try:
                return bool(has_account(account_id))
            except Exception:
                return False
        try:
            return any(
                isinstance(record, dict) and record.get("id") == account_id
                for record in self.manager.store.list_accounts()
            )
        except Exception:
            return False

    def _account_ids(self) -> list[str]:
        try:
            records = self.manager.store.list_accounts()
        except Exception:
            return []
        result = []
        for record in records:
            if isinstance(record, dict):
                account_id = record.get("id")
                if isinstance(account_id, str) and account_id:
                    result.append(account_id)
        return result

    def _get_client(self, account_id: str) -> Any | None:
        if not self._has_account(account_id):
            return None
        try:
            return self.manager.store.get_client(account_id)
        except Exception:
            return None

    def _enabled(self) -> bool:
        try:
            return self.manager.get_settings().get("auto_checkin_enabled") is True
        except Exception:
            return False

    @staticmethod
    def _reason(reason: str) -> str:
        return reason if isinstance(reason, str) and reason in _REASONS else "manual"

    async def check_status(self, account_id: str, reason: str = "manual") -> dict[str, Any]:
        async with self._account_lock(account_id):
            return await self._check_status_locked(account_id, self._reason(reason))

    async def _check_status_locked(self, account_id: str, reason: str) -> dict[str, Any]:
        state = self._state(account_id)
        client = self._get_client(account_id)
        if client is None:
            self._set_error(state, "账号不存在")
            state["next_checkin_at"] = None
            self._cancel_random_task(account_id, state["date"])
            self._log(
                "account_checkin_status_skipped",
                account_id=account_id,
                date=state["date"],
                reason=reason,
                status="account_missing",
            )
            return dict(state)

        previous = dict(state)
        state.update(status="checking", checked=None, error=None)
        self._log(
            "account_checkin_status_check_started",
            account_id=account_id,
            date=state["date"],
            reason=reason,
            status="checking",
        )
        try:
            response = await self._request(
                client, "POST", STATUS_PATH, account_id, reason
            )
        except asyncio.CancelledError:
            if state["date"] == self._today():
                stable = previous
                if stable.get("status") in {"checking", "signing"}:
                    stable = self._empty_state(state["date"])
                state.clear()
                state.update(stable)
                state["next_checkin_at"] = None
            raise
        now = self._epoch()
        state["last_checked_at"] = now
        status_result = self._parse_status(response)
        if status_result[0] == "checked":
            state.update(
                status="checked", checked=True, error=None, next_checkin_at=None
            )
            self._cancel_random_task(account_id, state["date"])
        elif status_result[0] == "unchecked":
            state.update(status="unchecked", checked=False, error=None)
        elif status_result[0] == "inactive":
            self._set_error(state, "签到活动未开放")
            state["next_checkin_at"] = None
            self._cancel_random_task(account_id, state["date"])
        else:
            self._set_error(state, "签到状态查询失败")
        self._log(
            "account_checkin_status_checked",
            account_id=account_id,
            date=state["date"],
            reason=reason,
            status=state["status"],
            http_status=response.status_code,
        )
        return dict(state)

    async def check_in(self, account_id: str, reason: str = "manual") -> dict[str, Any]:
        async with self._account_lock(account_id):
            return await self._check_in_locked(
                account_id,
                self._reason(reason),
                target_date=self._today(),
                require_enabled=False,
            )

    async def _check_in_locked(
        self,
        account_id: str,
        reason: str,
        *,
        target_date: str,
        require_enabled: bool,
    ) -> dict[str, Any]:
        state = self._state(account_id)
        if self._today() != target_date:
            self._log(
                "account_checkin_action_skipped",
                account_id=account_id,
                date=target_date,
                reason=reason,
                status="date_expired",
            )
            return dict(state)
        status = await self._check_status_locked(account_id, reason)
        state = self._state(account_id)
        if status["status"] != "unchecked":
            self._log(
                "account_checkin_action_skipped",
                account_id=account_id,
                date=target_date,
                reason=reason,
                status=status["status"],
            )
            return dict(state)
        if not self._before_post_allowed(account_id, target_date, require_enabled):
            self._log(
                "account_checkin_action_skipped",
                account_id=account_id,
                date=target_date,
                reason=reason,
                status="disabled_or_expired",
            )
            return dict(state)
        marker = (account_id, target_date)
        with self._state_guard:
            if marker in self._post_started:
                self._set_error(state, "签到结果未确认")
                self._log(
                    "account_checkin_action_skipped",
                    account_id=account_id,
                    date=target_date,
                    reason=reason,
                    status="already_attempted",
                )
                return dict(state)
            # Mark before yielding to the requester. A timeout or cancellation
            # after this point must not send the same daily action twice.
            self._post_started.add(marker)

        client = self._get_client(account_id)
        if client is None:
            with self._state_guard:
                self._post_started.discard(marker)
            self._set_error(state, "账号不存在")
            return dict(state)

        state.update(status="signing", checked=False, error=None, next_checkin_at=None)
        try:
            response = await self._request(
                client,
                "POST",
                CHECKIN_PATH,
                account_id,
                reason,
                before_request=lambda: self._before_post_allowed(
                    account_id, target_date, require_enabled
                ),
                on_start=lambda: self._log(
                    "account_checkin_action_started",
                    account_id=account_id,
                    date=target_date,
                    reason=reason,
                    status="signing",
                ),
            )
        except asyncio.CancelledError:
            if state["date"] == self._today():
                state.update(
                    status="unknown",
                    checked=None,
                    error=None,
                    next_checkin_at=None,
                )
            raise
        if response is None:
            with self._state_guard:
                self._post_started.discard(marker)
            state.update(status="unchecked", checked=False, error=None)
            self._log(
                "account_checkin_action_skipped",
                account_id=account_id,
                date=target_date,
                reason=reason,
                status="disabled_or_expired",
            )
            return dict(state)
        post_kind, _ = self._parse_business_response(response)
        if post_kind not in {"ok", "already"}:
            if self._explicit_post_rejection(response):
                confirmed = await self._check_status_locked(account_id, reason)
                state = self._state(account_id)
                if confirmed["status"] == "checked":
                    return dict(state)
                if confirmed["status"] == "unchecked":
                    with self._state_guard:
                        self._post_started.discard(marker)
            self._set_error(state, "签到请求失败")
            self._log(
                "account_checkin_failed",
                account_id=account_id,
                date=target_date,
                reason=reason,
                status="error",
                http_status=response.status_code,
            )
            return dict(state)

        # Even a successful POST must be confirmed by today's status endpoint;
        # code 10001 is only an already-signed hint, never proof by itself.
        confirmed = await self._check_status_locked(account_id, reason)
        state = self._state(account_id)
        if confirmed["status"] == "checked":
            state["last_checkin_at"] = self._epoch()
            self._log(
                "account_checkin_confirmed",
                account_id=account_id,
                date=target_date,
                reason=reason,
                status="checked",
                http_status=response.status_code,
            )
        elif confirmed["status"] != "error":
            self._set_error(state, "签到结果未确认")
        return dict(state)

    def _before_post_allowed(
        self, account_id: str, target_date: str, require_enabled: bool
    ) -> bool:
        return (
            not self._stop_event.is_set()
            and self._today() == target_date
            and self._has_account(account_id)
            and (not require_enabled or self._enabled())
        )

    @staticmethod
    def _set_error(state: dict[str, Any], error: str) -> None:
        state.update(
            status="error",
            checked=None,
            error=error if error in _SAFE_ERRORS else "签到请求失败",
        )

    def _cancel_random_task(self, account_id: str, target_date: str) -> None:
        key = (account_id, target_date)
        task = self._random_tasks.pop(key, None)
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    @classmethod
    def _explicit_post_rejection(cls, response: Response) -> bool:
        if response.status_code == 429 or 400 <= response.status_code < 500:
            return True
        value = cls._response_json(response)
        if value is None:
            return False
        code = value.get("code")
        return type(code) is int and code not in {0, 10001}

    async def _request(
        self,
        client: Any,
        method: str,
        path: str,
        account_id: str,
        reason: str,
        *,
        before_request: Callable[[], bool] | None = None,
        on_start: Callable[[], Any] | None = None,
    ) -> Response | None:
        async with self._semaphore:
            if before_request is not None and not before_request():
                return None
            if on_start is not None:
                on_start()
            try:
                response = await self._requester(client, method, path)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._log(
                    "account_checkin_upstream_failed",
                    account_id=account_id,
                    date=self._today(),
                    reason=reason,
                    status="error",
                )
                response = Response(status_code=502, content=b"")
        if response.status_code == 429:
            await self._record_rate_limit(client, response.headers.get("Retry-After"))
        return response

    async def _record_rate_limit(self, client: Any, retry_after: str | None) -> None:
        handler = getattr(self.manager, "handle_rate_limit", None)
        if not callable(handler):
            return
        try:
            result = await asyncio.to_thread(
                handler, client, retry_after, model=None, allow_switch=False
            )
            if hasattr(result, "__await__"):
                await result
        except Exception:
            self._log("account_checkin_rate_limit_record_failed")

    @staticmethod
    def _response_json(response: Response) -> dict[str, Any] | None:
        if not 200 <= int(response.status_code) < 300:
            return None
        try:
            value = json.loads(response.body)
        except (TypeError, ValueError, UnicodeDecodeError):
            return None
        if not isinstance(value, dict):
            return None
        # Some client wrappers retain the upstream envelope under this key;
        # still validate its own business code and data strictly.
        upstream = value.get("upstream")
        if isinstance(upstream, dict):
            value = upstream
        return value

    @classmethod
    def _parse_business_response(cls, response: Response) -> tuple[str, dict[str, Any] | None]:
        value = cls._response_json(response)
        if value is None:
            return "failure", None
        code = value.get("code")
        if type(code) is int and code == 0:
            data = value.get("data")
            return ("ok", data if isinstance(data, dict) else None)
        if type(code) is int and code == 10001:
            return "already", None
        return "failure", None

    @classmethod
    def _parse_status(cls, response: Response) -> tuple[str, bool | None]:
        value = cls._response_json(response)
        if value is None or type(value.get("code")) is not int or value["code"] != 0:
            return "unknown", None
        data = value.get("data")
        if not isinstance(data, dict):
            return "unknown", None
        if data.get("active") is False:
            return "inactive", None
        if "today_checked_in" in data:
            result = data.get("today_checked_in")
        else:
            # ``checked`` is retained only for older, explicit boolean fixtures.
            result = data.get("checked")
        if type(result) is not bool:
            return "unknown", None
        return ("checked" if result else "unchecked"), result

    async def start(self) -> None:
        """Register timers and return immediately; network work runs in tasks."""

        if self._started or self._closed:
            return
        self._loop = asyncio.get_running_loop()
        self._started = True
        if self._enabled():
            self._schedule_catchup("startup")
        self._scheduler_task = self._loop.create_task(self._scheduler_loop())

    def settings_changed(self, enabled: bool) -> None:
        """Apply a setting transition safely from either loop or worker thread."""

        loop = self._loop
        if loop is None or loop.is_closed() or not self._started or self._closed:
            return
        loop.call_soon_threadsafe(self._apply_settings_change, bool(enabled))

    def _apply_settings_change(self, enabled: bool) -> None:
        if self._closed or self._stop_event.is_set():
            return
        # Callbacks can arrive out of order from concurrent settings writers.
        # The persisted setting at execution time is authoritative.
        enabled = self._enabled()
        if not enabled:
            for key, task in list(self._random_tasks.items()):
                if not task.done():
                    task.cancel()
                self._random_tasks.pop(key, None)
                account_id, target_date = key
                state = self._state(account_id)
                if state["date"] == target_date and state["status"] == "unchecked":
                    state["next_checkin_at"] = None
            # Cancel other automatic work before its POST gate. A task whose
            # POST has already begun is allowed to finish its single request.
            for task in list(self._tasks):
                key = self._task_keys.get(task)
                if key is not None and key in self._post_started:
                    continue
                if task is not asyncio.current_task() and not task.done():
                    task.cancel()
            return
        self._schedule_catchup("enable")

    def _schedule_catchup(self, reason: str) -> None:
        if not self._enabled() or self._stop_event.is_set() or self._loop is None:
            return
        target_date = self._today()
        for account_id in self._account_ids():
            self._spawn(
                self._automatic_checkin(account_id, target_date, reason),
                key=(account_id, target_date),
            )

    async def _automatic_checkin(
        self, account_id: str, target_date: str, reason: str
    ) -> None:
        async with self._account_lock(account_id):
            if not self._before_post_allowed(account_id, target_date, True):
                self._log(
                    "account_checkin_action_skipped",
                    account_id=account_id,
                    date=target_date,
                    reason=reason,
                    status="disabled_or_expired",
                )
                return
            await self._check_in_locked(
                account_id,
                self._reason(reason),
                target_date=target_date,
                require_enabled=True,
            )

    async def _prepare_midnight(
        self, account_id: str, target_date: str, midnight: datetime
    ) -> None:
        if not self._before_post_allowed(account_id, target_date, True):
            self._log(
                "account_checkin_midnight_skipped",
                account_id=account_id,
                date=target_date,
                reason="midnight",
                status="disabled_or_expired",
            )
            return
        result = await self.check_status(account_id, reason="midnight")
        if result["status"] != "unchecked":
            self._log(
                "account_checkin_midnight_skipped",
                account_id=account_id,
                date=target_date,
                reason="midnight",
                status=result["status"],
            )
            return
        if not self._before_post_allowed(account_id, target_date, True):
            self._log(
                "account_checkin_midnight_skipped",
                account_id=account_id,
                date=target_date,
                reason="midnight",
                status="disabled_or_expired",
            )
            return
        try:
            offset = float(self._random_delay())
        except (TypeError, ValueError, OverflowError):
            offset = 0.0
        if not (offset >= 0.0 and offset <= 600.0):
            offset = min(600.0, max(0.0, offset)) if offset == offset else 0.0
        due = midnight + timedelta(seconds=offset)
        due_epoch = due.timestamp()
        state = self._state(account_id)
        state["next_checkin_at"] = due_epoch
        self._log(
            "account_checkin_scheduled",
            account_id=account_id,
            date=target_date,
            reason="midnight",
            status="unchecked",
            next_checkin_at=due_epoch,
        )
        key = (account_id, target_date)
        old = self._random_tasks.get(key)
        if old is not None and not old.done():
            old.cancel()
        task = self._spawn(
            self._random_followup(account_id, target_date, due), key=key
        )
        self._random_tasks[key] = task

    async def _random_followup(
        self, account_id: str, target_date: str, due: datetime
    ) -> None:
        delay = max(0.0, due.timestamp() - self._epoch())
        await self._sleep(delay)
        key = (account_id, target_date)
        self._random_tasks.pop(key, None)
        state = self._state(account_id)
        if not self._before_post_allowed(account_id, target_date, True):
            if state["date"] == target_date and state["status"] == "unchecked":
                state["next_checkin_at"] = None
            if self._today() != target_date:
                skip_status = "date_expired"
            elif not self._has_account(account_id):
                skip_status = "account_missing"
            else:
                skip_status = "disabled_or_expired"
            self._log(
                "account_checkin_action_skipped",
                account_id=account_id,
                date=target_date,
                reason="timer",
                status=skip_status,
            )
            return
        state["next_checkin_at"] = None
        await self._automatic_checkin(account_id, target_date, "timer")

    async def _run_midnight(self, target_date: date) -> None:
        target = target_date.isoformat()
        midnight = datetime.combine(target_date, time.min, tzinfo=BEIJING)
        for account_id in self._account_ids():
            self._spawn(
                self._prepare_midnight(account_id, target, midnight),
                key=(account_id, target),
            )

    async def _scheduler_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                now = self._now()
                next_day = now.date() + timedelta(days=1)
                midnight = datetime.combine(next_day, time.min, tzinfo=BEIJING)
                await self._sleep(max(0.0, (midnight - now).total_seconds()))
                if self._stop_event.is_set():
                    break
                if self._enabled():
                    await self._run_midnight(next_day)
        except asyncio.CancelledError:
            raise

    def _spawn(
        self,
        coroutine: Awaitable[Any],
        *,
        key: tuple[str, str] | None = None,
    ) -> asyncio.Task[Any]:
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        task = self._loop.create_task(coroutine)
        self._tasks.add(task)
        self._task_keys[task] = key

        def done(completed: asyncio.Task[Any]) -> None:
            self._tasks.discard(completed)
            task_key = self._task_keys.pop(completed, None)
            if task_key is not None and self._random_tasks.get(task_key) is completed:
                self._random_tasks.pop(task_key, None)
            try:
                completed.exception()
            except (asyncio.CancelledError, Exception):
                pass

        task.add_done_callback(done)
        return task

    def stop(self) -> None:
        """Synchronously signal shutdown and schedule cancellation on the loop."""

        self._stop_event.set()
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._cancel_all_on_loop)

    def _cancel_all_on_loop(self) -> None:
        if self._scheduler_task is not None and not self._scheduler_task.done():
            self._scheduler_task.cancel()
        for task in list(self._tasks):
            if not task.done():
                task.cancel()
        self._random_tasks.clear()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.stop()
        current = asyncio.current_task()
        tasks = [task for task in self._tasks if task is not current]
        if self._scheduler_task is not None and self._scheduler_task is not current:
            tasks.append(self._scheduler_task)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
