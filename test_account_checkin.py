"""Offline behavior tests for per-account scheduled daily check-in."""

import asyncio
from datetime import date, datetime, timedelta, timezone
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.responses import Response

import codebuddy_proxy.billing_proxy as billing
from codebuddy_proxy.account_checkin import (
    AccountCheckinService,
    CHECKIN_PATH,
    STATUS_PATH,
)


BEIJING = timezone(timedelta(hours=8))


class FakeClient:
    def __init__(self, account_id):
        self.account_id = account_id


class FakeStore:
    def __init__(self, account_ids=("acct-a",)):
        self.clients = {item: FakeClient(item) for item in account_ids}

    def list_accounts(self):
        return [{"id": item} for item in self.clients]

    def get_client(self, account_id):
        return self.clients[account_id]


class FakeManager:
    def __init__(self, account_ids=("acct-a",), *, enabled=False, active="acct-a"):
        self.store = FakeStore(account_ids)
        self.settings = {"auto_checkin_enabled": enabled}
        self.active_account_id = active
        self.rate_limits = []

    def get_settings(self):
        return dict(self.settings)

    def has_account(self, account_id):
        return account_id in self.store.clients

    def handle_rate_limit(self, client, retry_after, *, model=None, allow_switch=True):
        self.rate_limits.append((client.account_id, retry_after, model, allow_switch))
        return None


def response(payload, status=200, headers=None):
    if isinstance(payload, bytes):
        content = payload
    else:
        content = json.dumps(payload).encode("utf-8")
    return Response(content=content, status_code=status, headers=headers or {})


def status_body(checked, *, active=True):
    return {
        "code": 0,
        "msg": "OK",
        "data": {"active": active, "today_checked_in": checked},
    }


async def wait_event(event):
    await asyncio.wait_for(event.wait(), timeout=1.5)


def run(coro):
    return asyncio.run(coro)


class MutableClock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


async def never_sleep(_seconds):
    await asyncio.Event().wait()


def test_startup_when_enabled_checks_every_account_and_supplements_only_unchecked():
    async def scenario():
        manager = FakeManager(("acct-a", "acct-b"), enabled=True, active="acct-a")
        calls = []
        completed = asyncio.Event()
        status_counts = {}

        async def requester(client, method, path):
            calls.append((client.account_id, method, path))
            if path == STATUS_PATH:
                count = status_counts.get(client.account_id, 0) + 1
                status_counts[client.account_id] = count
                if client.account_id == "acct-a" or count > 1:
                    if client.account_id == "acct-b" and count > 1:
                        completed.set()
                    return response(status_body(True))
                return response(status_body(False))
            return response({"code": 0, "msg": "ok", "data": {}})

        service = AccountCheckinService(
            manager, requester=requester, sleep=never_sleep
        )
        await service.start()
        await wait_event(completed)
        assert status_counts == {"acct-a": 1, "acct-b": 2}
        assert sum(path == CHECKIN_PATH for _, _, path in calls) == 1
        assert service.snapshot("acct-a")["status"] == "checked"
        assert service.snapshot("acct-b")["status"] == "checked"
        assert service.snapshot("acct-a")["last_checkin_at"] is None
        assert service.snapshot("acct-b")["last_checkin_at"] is not None
        assert manager.active_account_id == "acct-a"
        await service.close()

    run(scenario())


def test_enable_transition_immediately_checks_and_supplements():
    async def scenario():
        manager = FakeManager(enabled=False)
        calls = []
        completed = asyncio.Event()

        async def requester(client, method, path):
            calls.append(path)
            if path == STATUS_PATH:
                if calls.count(STATUS_PATH) == 2:
                    completed.set()
                    return response(status_body(True))
                return response(status_body(False))
            return response({"code": 0, "data": {}})

        service = AccountCheckinService(
            manager, requester=requester, sleep=never_sleep
        )
        await service.start()
        await asyncio.sleep(0)
        assert calls == []
        manager.settings["auto_checkin_enabled"] = True
        service.settings_changed(True)
        await wait_event(completed)
        assert calls.count(CHECKIN_PATH) == 1
        await service.close()

    run(scenario())


def test_midnight_flow_checks_then_rechecks_at_absolute_random_time():
    async def scenario():
        manager = FakeManager(enabled=True)
        clock = MutableClock(datetime(2026, 10, 24, 0, 1, tzinfo=BEIJING))
        calls = []
        check_count = 0
        delays = []
        completed = asyncio.Event()

        async def requester(client, method, path):
            nonlocal check_count
            calls.append(path)
            if path == STATUS_PATH:
                check_count += 1
                if check_count < 3:
                    return response(status_body(False))
                completed.set()
                return response(status_body(True))
            return response({"code": 0, "data": {}})

        async def advancing_sleep(seconds):
            delays.append(seconds)
            clock.advance(seconds)

        service = AccountCheckinService(
            manager,
            clock=clock,
            random_delay=lambda: 120,
            sleep=advancing_sleep,
            requester=requester,
        )
        midnight = datetime(2026, 10, 24, tzinfo=BEIJING)
        await service._prepare_midnight("acct-a", "2026-10-24", midnight)
        await wait_event(completed)
        assert delays == pytest.approx([60.0])
        assert calls == [STATUS_PATH, STATUS_PATH, CHECKIN_PATH, STATUS_PATH]
        assert service.snapshot("acct-a")["status"] == "checked"
        await service.close()

    run(scenario())


def test_slow_midnight_query_does_not_push_random_window_past_600_seconds():
    async def scenario():
        manager = FakeManager(enabled=True)
        clock = MutableClock(datetime(2026, 10, 24, 0, 7, tzinfo=BEIJING))
        delays = []

        async def requester(client, method, path):
            return response(status_body(False))

        async def capture_sleep(seconds):
            delays.append(seconds)
            # Hold this task until the assertion has observed the due delay.
            await asyncio.Event().wait()

        service = AccountCheckinService(
            manager,
            clock=clock,
            random_delay=lambda: 60,
            sleep=capture_sleep,
            requester=requester,
        )
        await service._prepare_midnight(
            "acct-a", "2026-10-24", datetime(2026, 10, 24, tzinfo=BEIJING)
        )
        for _ in range(20):
            if delays:
                break
            await asyncio.sleep(0)
        assert delays == [0.0]
        assert service.snapshot("acct-a")["next_checkin_at"] == datetime(
            2026, 10, 24, 0, 1, tzinfo=BEIJING
        ).timestamp()
        await service.close()

    run(scenario())


def test_midnight_skips_already_checked_account_without_random_task():
    async def scenario():
        manager = FakeManager(enabled=True)
        random_calls = []
        paths = []

        async def requester(client, method, path):
            paths.append(path)
            return response(status_body(True))

        service = AccountCheckinService(
            manager,
            clock=lambda: datetime(2026, 10, 24, 1, tzinfo=BEIJING),
            random_delay=lambda: random_calls.append(True) or 60,
            sleep=never_sleep,
            requester=requester,
        )
        await service._prepare_midnight(
            "acct-a", "2026-10-24", datetime(2026, 10, 24, tzinfo=BEIJING)
        )
        await asyncio.sleep(0)
        assert paths == [STATUS_PATH]
        assert random_calls == []
        assert service.snapshot("acct-a")["next_checkin_at"] is None
        await service.close()

    run(scenario())


def test_disabling_cancels_pending_random_timer_and_clears_due_time():
    async def scenario():
        manager = FakeManager(enabled=False)
        sleep_started = asyncio.Event()
        requested = []

        async def requester(client, method, path):
            requested.append(path)
            return response(status_body(False))

        async def blocked_sleep(seconds):
            if seconds <= 600:
                sleep_started.set()
            await asyncio.Event().wait()

        service = AccountCheckinService(
            manager,
            clock=lambda: datetime(2026, 10, 24, 0, 1, tzinfo=BEIJING),
            random_delay=lambda: 120,
            sleep=blocked_sleep,
            requester=requester,
        )
        await service.start()
        manager.settings["auto_checkin_enabled"] = True
        await service._prepare_midnight(
            "acct-a", "2026-10-24", datetime(2026, 10, 24, tzinfo=BEIJING)
        )
        await wait_event(sleep_started)
        due = service.snapshot("acct-a")["next_checkin_at"]
        assert due is not None
        await service.check_status("acct-a")
        assert service.snapshot("acct-a")["next_checkin_at"] == due
        manager.settings["auto_checkin_enabled"] = False
        service.settings_changed(False)
        await asyncio.sleep(0)
        assert service.snapshot("acct-a")["next_checkin_at"] is None
        assert requested == [STATUS_PATH, STATUS_PATH]
        await service.close()
        assert service._random_tasks == {}
        assert service._tasks == set()

    run(scenario())


def test_stop_synchronously_signals_and_cancels_pending_random_work():
    async def scenario():
        manager = FakeManager(enabled=False)
        sleep_started = asyncio.Event()
        paths = []

        async def requester(client, method, path):
            paths.append(path)
            return response(status_body(False))

        async def blocked_sleep(seconds):
            if seconds <= 600:
                sleep_started.set()
            await asyncio.Event().wait()

        service = AccountCheckinService(
            manager,
            clock=lambda: datetime(2026, 10, 24, 0, 1, tzinfo=BEIJING),
            random_delay=lambda: 120,
            sleep=blocked_sleep,
            requester=requester,
        )
        await service.start()
        manager.settings["auto_checkin_enabled"] = True
        await service._prepare_midnight(
            "acct-a", "2026-10-24", datetime(2026, 10, 24, tzinfo=BEIJING)
        )
        await wait_event(sleep_started)
        service.stop()
        assert service._stop_event.is_set()
        await asyncio.sleep(0)
        assert paths == [STATUS_PATH]
        await service.close()
        assert service._tasks == set()
        assert service._random_tasks == {}

    run(scenario())


def test_two_concurrent_manual_checkins_send_only_one_post():
    async def scenario():
        manager = FakeManager()
        status_calls = 0
        post_calls = 0
        post_started = asyncio.Event()
        release_post = asyncio.Event()

        async def requester(client, method, path):
            nonlocal status_calls, post_calls
            if path == STATUS_PATH:
                status_calls += 1
                return response(status_body(status_calls > 1))
            post_calls += 1
            post_started.set()
            await release_post.wait()
            return response({"code": 0, "data": {}})

        service = AccountCheckinService(manager, requester=requester)
        first = asyncio.create_task(service.check_in("acct-a"))
        second = asyncio.create_task(service.check_in("acct-a"))
        await wait_event(post_started)
        release_post.set()
        results = await asyncio.gather(first, second)
        assert post_calls == 1
        assert status_calls == 3  # Before POST, post confirmation, serialized caller.
        assert all(item["status"] == "checked" for item in results)
        await service.close()

    run(scenario())


def test_explicit_rejection_confirmed_unchecked_can_be_retried_once_later():
    async def scenario():
        manager = FakeManager()
        status_calls = 0
        post_calls = 0

        async def requester(client, method, path):
            nonlocal status_calls, post_calls
            if path == STATUS_PATH:
                status_calls += 1
                return response(status_body(status_calls == 4))
            post_calls += 1
            if post_calls == 1:
                return response({"code": 429, "msg": "limited"}, status=429)
            return response({"code": 0, "data": {}})

        service = AccountCheckinService(manager, requester=requester)
        failed = await service.check_in("acct-a")
        assert failed["status"] == "error"
        assert failed["error"] == "签到请求失败"
        assert ("acct-a", "2026-10-24") not in service._post_started
        succeeded = await service.check_in("acct-a")
        assert succeeded["status"] == "checked"
        assert post_calls == 2
        assert status_calls == 4
        assert manager.rate_limits == [("acct-a", None, None, False)]
        await service.close()

    run(scenario())


def test_disable_while_post_waits_for_concurrency_slot_blocks_requester():
    async def scenario():
        manager = FakeManager(enabled=True)
        service = None
        hold_started = asyncio.Event()
        release_hold = asyncio.Event()
        post_calls = []

        async def hold_slot_after_status():
            await service._semaphore.acquire()
            hold_started.set()
            try:
                await release_hold.wait()
            finally:
                service._semaphore.release()

        async def requester(client, method, path):
            if path == STATUS_PATH:
                holder = asyncio.create_task(hold_slot_after_status())
                await asyncio.sleep(0)  # Queue the holder ahead of the next request.
                return response(status_body(False))
            post_calls.append(path)
            return response({"code": 0, "data": {}})

        service = AccountCheckinService(
            manager,
            clock=lambda: datetime(2026, 10, 24, 1, tzinfo=BEIJING),
            requester=requester,
            max_concurrent=1,
        )
        task = service._spawn(
            service._automatic_checkin("acct-a", "2026-10-24", "startup"),
            key=("acct-a", "2026-10-24"),
        )
        await wait_event(hold_started)
        await asyncio.sleep(0)
        assert ("acct-a", "2026-10-24") in service._post_started
        manager.settings["auto_checkin_enabled"] = False
        service._apply_settings_change(True)  # Stale enable callback reads persisted off.
        release_hold.set()
        await task
        assert post_calls == []
        assert ("acct-a", "2026-10-24") not in service._post_started
        assert service.snapshot("acct-a")["status"] == "unchecked"
        await service.close()

    run(scenario())


def test_stale_settings_callback_uses_current_setting_to_cancel_random_task():
    async def scenario():
        manager = FakeManager(enabled=False)
        sleep_started = asyncio.Event()
        requested = []

        async def requester(client, method, path):
            requested.append(path)
            return response(status_body(False))

        async def blocked_sleep(seconds):
            if seconds <= 600:
                sleep_started.set()
            await asyncio.Event().wait()

        service = AccountCheckinService(
            manager,
            clock=lambda: datetime(2026, 10, 24, 0, 1, tzinfo=BEIJING),
            random_delay=lambda: 120,
            sleep=blocked_sleep,
            requester=requester,
        )
        await service.start()
        manager.settings["auto_checkin_enabled"] = True
        await service._prepare_midnight(
            "acct-a", "2026-10-24", datetime(2026, 10, 24, tzinfo=BEIJING)
        )
        await wait_event(sleep_started)
        assert service.snapshot("acct-a")["next_checkin_at"] is not None
        manager.settings["auto_checkin_enabled"] = False
        service._apply_settings_change(True)  # Delayed callback from older state.
        await asyncio.sleep(0)
        assert service.snapshot("acct-a")["next_checkin_at"] is None
        assert requested == [STATUS_PATH]
        await service.close()

    run(scenario())


def test_stale_off_callback_cannot_cancel_schedule_when_saved_setting_is_on():
    async def scenario():
        manager = FakeManager(enabled=False)
        sleep_started = asyncio.Event()
        scheduled = []

        async def requester(client, method, path):
            return response(status_body(False))

        async def blocked_sleep(seconds):
            if seconds <= 600:
                sleep_started.set()
            await asyncio.Event().wait()

        service = AccountCheckinService(
            manager,
            clock=lambda: datetime(2026, 10, 24, 0, 1, tzinfo=BEIJING),
            random_delay=lambda: 120,
            sleep=blocked_sleep,
            requester=requester,
        )
        await service.start()
        service._schedule_catchup = lambda reason: scheduled.append(reason)
        manager.settings["auto_checkin_enabled"] = True
        await service._prepare_midnight(
            "acct-a", "2026-10-24", datetime(2026, 10, 24, tzinfo=BEIJING)
        )
        await wait_event(sleep_started)
        before = service.snapshot("acct-a")["next_checkin_at"]
        service._apply_settings_change(False)  # Delayed off callback from before the final on.
        assert scheduled == ["enable"]
        assert service.snapshot("acct-a")["next_checkin_at"] == before
        await service.close()

    run(scenario())


def test_safety_logging_records_query_action_schedule_and_skips_without_payloads():
    async def scenario():
        manager = FakeManager(("acct-a", "acct-b"), enabled=True)
        events = []
        status_calls = {"acct-a": 0, "acct-b": 0}

        async def requester(client, method, path):
            if path == STATUS_PATH:
                status_calls[client.account_id] += 1
                return response(
                    status_body(
                        client.account_id == "acct-a"
                        and status_calls[client.account_id] > 1
                    )
                )
            return response({"code": 0, "data": {}})

        service = AccountCheckinService(
            manager,
            clock=lambda: datetime(2026, 10, 24, 0, 1, tzinfo=BEIJING),
            random_delay=lambda: 600,
            sleep=never_sleep,
            requester=requester,
        )
        service.configure_logging(lambda event, **fields: events.append((event, fields)))
        await service._prepare_midnight(
            "acct-b",
            "2026-10-24",
            datetime(2026, 10, 24, tzinfo=BEIJING),
        )
        await service.check_in("acct-a")
        await service.check_in("acct-a")  # Checked status is logged as an action skip.
        names = [event for event, _ in events]
        assert "account_checkin_status_check_started" in names
        assert "account_checkin_scheduled" in names
        assert "account_checkin_action_started" in names
        assert "account_checkin_confirmed" in names
        assert "account_checkin_action_skipped" in names
        assert all(
            set(fields).issubset(
                {"account_id", "date", "reason", "status", "http_status", "next_checkin_at"}
            )
            for _, fields in events
        )
        await service.close()

    run(scenario())


@pytest.mark.parametrize(
    "payload,expected_error",
    [
        ({"code": 10001, "msg": "already", "data": None}, "签到状态查询失败"),
        ({"code": False, "data": {"today_checked_in": False}}, "签到状态查询失败"),
        ({"code": 0, "data": {"today_checked_in": "false"}}, "签到状态查询失败"),
        (
            {"code": 0, "data": {"today_checked_in": "false", "checked": True}},
            "签到状态查询失败",
        ),
        (
            {"code": 0, "data": {"active": False, "today_checked_in": False}},
            "签到活动未开放",
        ),
    ],
)
def test_unknown_or_inactive_status_never_posts(payload, expected_error):
    async def scenario():
        manager = FakeManager()
        paths = []

        async def requester(client, method, path):
            paths.append(path)
            return response(payload)

        service = AccountCheckinService(manager, requester=requester)
        result = await service.check_in("acct-a")
        assert result["status"] == "error"
        assert result["error"] == expected_error
        assert result["checked"] is None
        assert paths == [STATUS_PATH]
        await service.close()

    run(scenario())


def test_boolean_checked_is_strict_and_legacy_boolean_fixture_is_supported():
    async def scenario():
        manager = FakeManager()
        bodies = [
            {"code": 0, "data": {"checked": True}},
            {"code": 0, "data": {"checked": 1}},
        ]
        paths = []

        async def requester(client, method, path):
            paths.append(path)
            return response(bodies.pop(0))

        service = AccountCheckinService(manager, requester=requester)
        old_fixture = await service.check_status("acct-a")
        invalid_fixture = await service.check_status("acct-a")
        assert old_fixture["status"] == "checked"
        assert invalid_fixture["status"] == "error"
        assert paths == [STATUS_PATH, STATUS_PATH]
        await service.close()

    run(scenario())


def test_disable_during_status_get_prevents_automatic_post():
    async def scenario():
        manager = FakeManager(enabled=True)
        get_started = asyncio.Event()
        release_get = asyncio.Event()
        paths = []

        async def requester(client, method, path):
            paths.append(path)
            if path == STATUS_PATH:
                get_started.set()
                await release_get.wait()
                return response(status_body(False))
            return response({"code": 0, "data": {}})

        service = AccountCheckinService(manager, requester=requester)
        task = asyncio.create_task(
            service._automatic_checkin("acct-a", "2026-10-24", "startup")
        )
        await wait_event(get_started)
        manager.settings["auto_checkin_enabled"] = False
        release_get.set()
        await task
        assert paths == [STATUS_PATH]
        await service.close()

    run(scenario())


def test_cancelled_automatic_status_query_clears_busy_state_and_allows_manual_checkin():
    async def scenario():
        manager = FakeManager(enabled=False)
        get_started = asyncio.Event()
        status_calls = 0
        post_calls = 0

        async def requester(client, method, path):
            nonlocal status_calls, post_calls
            if path == STATUS_PATH:
                status_calls += 1
                if status_calls == 1:
                    get_started.set()
                    await asyncio.Event().wait()
                return response(status_body(status_calls > 2))
            post_calls += 1
            return response({"code": 0, "data": {}})

        service = AccountCheckinService(
            manager,
            clock=lambda: datetime(2026, 10, 24, 1, tzinfo=BEIJING),
            requester=requester,
            sleep=never_sleep,
        )
        await service.start()
        manager.settings["auto_checkin_enabled"] = True
        task = service._spawn(
            service._automatic_checkin("acct-a", "2026-10-24", "startup"),
            key=("acct-a", "2026-10-24"),
        )
        await wait_event(get_started)
        assert service.snapshot("acct-a")["status"] == "checking"
        manager.settings["auto_checkin_enabled"] = False
        service.settings_changed(False)
        await asyncio.gather(task, return_exceptions=True)
        cancelled_state = service.snapshot("acct-a")
        assert cancelled_state["status"] == "unknown"
        assert cancelled_state["next_checkin_at"] is None
        assert post_calls == 0

        manual = await service.check_in("acct-a")
        assert manual["status"] == "checked"
        assert post_calls == 1
        await service.close()

    run(scenario())


@pytest.mark.parametrize("invalidate", ["deleted", "next-day"])
def test_random_followup_rechecks_account_and_target_date(invalidate):
    async def scenario():
        manager = FakeManager(enabled=True)
        sleep_started = asyncio.Event()
        release_sleep = asyncio.Event()
        paths = []
        clock = MutableClock(datetime(2026, 10, 24, 0, 1, tzinfo=BEIJING))

        async def requester(client, method, path):
            paths.append(path)
            return response(status_body(False))

        async def gated_sleep(_seconds):
            sleep_started.set()
            await release_sleep.wait()

        service = AccountCheckinService(
            manager,
            clock=clock,
            random_delay=lambda: 120,
            sleep=gated_sleep,
            requester=requester,
        )
        await service._prepare_midnight(
            "acct-a", "2026-10-24", datetime(2026, 10, 24, tzinfo=BEIJING)
        )
        await wait_event(sleep_started)
        if invalidate == "deleted":
            manager.store.clients.pop("acct-a")
        else:
            clock.advance(24 * 60 * 60)
            release_sleep.set()
        if invalidate == "deleted":
            release_sleep.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert paths == [STATUS_PATH]
        if invalidate == "next-day":
            assert service.snapshot("acct-a")["date"] == "2026-10-25"
            assert service.snapshot("acct-a")["status"] == "unknown"
        await service.close()

    run(scenario())


def test_default_billing_requester_is_bound_to_target_and_records_429_without_switch(monkeypatch):
    manager = FakeManager(("acct-a", "acct-b"), active="acct-a")
    target = manager.store.clients["acct-b"]
    auth_options = []

    class BillingClient(FakeClient):
        endpoint = "https://billing.example"

        def build_url(self, path):
            return self.endpoint + path

        def ensure_authenticated(self, **kwargs):
            auth_options.append((self.account_id, kwargs))

        def auth_headers(self):
            return {"Authorization": f"Bearer {self.account_id}"}

    manager.store.clients["acct-b"] = BillingClient("acct-b")
    client = manager.store.clients["acct-b"]
    real_async_client = httpx.AsyncClient
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            429,
            content=b'{"code":429,"msg":"slow down"}',
            headers={"content-type": "application/json", "Retry-After": "37"},
        )

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        billing.httpx,
        "AsyncClient",
        lambda **kwargs: real_async_client(transport=transport, **kwargs),
    )

    async def scenario():
        service = AccountCheckinService(manager)
        result = await service.check_status("acct-b")
        assert result["status"] == "error"
        assert result["error"] == "签到状态查询失败"
        await service.close()

    run(scenario())
    assert len(requests) == 1
    assert requests[0].url.path == STATUS_PATH
    assert requests[0].headers["authorization"] == "Bearer acct-b"
    assert auth_options == [
        ("acct-b", {"open_browser": False, "allow_login": False})
    ]
    assert manager.rate_limits == [("acct-b", "37", None, False)]
    assert manager.active_account_id == "acct-a"
