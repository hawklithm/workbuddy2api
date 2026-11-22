import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from codebuddy_proxy.admin_api import create_admin_router
from codebuddy_proxy.trae_accounts import TraeAccounts
from codebuddy_proxy.account_manager import AccountManagerError


def wait_job(manager, job):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = manager.job(job['id'])
        if result['status'] != 'pending':
            return result
        time.sleep(.01)
    raise AssertionError('login timed out')


class Flow:
    uid = 'user-1'
    entered = None
    release = None

    def __init__(self, creds_file):
        self.file = creds_file
        self.http = SimpleNamespace(close=lambda: None)
        self._done = threading.Event()

    def cancel(self):
        self._done.set()

    def start(self, open_browser, timeout_sec, log):
        self.on_event('auth_url', 'https://example.test/login')
        if self.entered:
            self.entered.set()
            self.release.wait(2)
        self.file.write_text(json.dumps({'auth': {'userId': self.uid, 'token': 'private'}}), encoding='utf-8')
        identity = self.file.with_name('credentials.device.secret')
        if not identity.exists():
            identity.write_text('device-secret', encoding='utf-8')
        return SimpleNamespace(creds=SimpleNamespace(user_id=self.uid, username='Name'), http=SimpleNamespace(close=lambda: None))


@pytest.fixture
def manager(tmp_path, monkeypatch):
    import buddy_agent_auth.providers.trae.login as login
    monkeypatch.setattr(login, 'TraeLoginFlow', Flow)
    Flow.uid = 'user-1'
    Flow.entered = Flow.release = None
    instance = TraeAccounts(tmp_path)
    yield instance
    asyncio.run(instance.close())


def test_login_duplicate_relogin_and_private_snapshot(manager):
    result = wait_job(manager, manager.start_login())
    assert result['status'] == 'succeeded'
    account_id = result['account_id']
    assert 'private' not in json.dumps(manager.snapshot())
    assert wait_job(manager, manager.start_login())['status'] == 'failed'
    assert len(manager.snapshot()['accounts']) == 1
    assert wait_job(manager, manager.start_login(account_id))['status'] == 'succeeded'
    before = (manager._path(account_id) / 'credentials.json').read_bytes()
    Flow.uid = 'other'
    assert wait_job(manager, manager.start_login(account_id))['status'] == 'failed'
    assert (manager._path(account_id) / 'credentials.json').read_bytes() == before
    asyncio.run(manager.delete(account_id))
    assert manager.snapshot()['accounts'] == []


def test_cancelled_login_never_commits(manager):
    Flow.entered, Flow.release = threading.Event(), threading.Event()
    job = manager.start_login()
    assert Flow.entered.wait(2)
    assert manager.job(job['id'], cancel=True)['status'] == 'cancelled'
    Flow.release.set()
    manager.jobs[job['id']]['future'].result(timeout=3)
    assert manager.snapshot()['accounts'] == []


def test_settings_and_checkin_are_isolated(manager, monkeypatch):
    import daily_checkin.trae_scheduler as scheduler
    result = wait_job(manager, manager.start_login())
    account_id = result['account_id']
    events = []
    class Service:
        def __init__(self):
            self.manager = SimpleNamespace(store=SimpleNamespace(account_id='user-1'))
        async def start(self): events.append('start')
        async def close(self): events.append('close')
        async def check_status(self, uid, reason): return {'status': 'unchecked', 'uid': uid}
        async def check_in(self, uid, reason): return {'status': 'checked', 'uid': uid}
        def snapshot(self, uid): return {'status': 'checked'}
    monkeypatch.setattr(scheduler, 'create_service', lambda **kwargs: Service())
    async def run():
        await manager.start()
        assert (await manager.checkin(account_id))['status'] == 'unchecked'
        assert (await manager.checkin(account_id, claim=True))['status'] == 'checked'
        await manager.set_auto(True)
        assert manager.snapshot()['auto_checkin_enabled'] is True
        await manager.set_auto(False)
    asyncio.run(run())
    assert events == ['close', 'start', 'close']
    assert manager.snapshot()['auto_checkin_enabled'] is False


def test_trae_routes_require_local_write_header(manager):
    owner = SimpleNamespace(_lock=threading.RLock(), trae_accounts=manager)
    app = FastAPI()
    app.include_router(create_admin_router(lambda: owner))
    with TestClient(app, base_url='http://localhost', client=('127.0.0.1', 1234)) as client:
        assert client.get('/admin/api/trae/accounts').status_code == 200
        assert client.post('/admin/api/trae/logins', json={}).status_code == 403
        headers = {'X-Account-Manager': '1', 'Origin': 'http://localhost'}
        assert client.patch('/admin/api/trae/settings', headers=headers, json={'auto_checkin_enabled': 1}).status_code == 400
        assert client.patch('/admin/api/trae/settings', headers=headers, json={'auto_checkin_enabled': True}).status_code == 200
        assert client.post('/admin/api/trae/logins', headers={'X-Account-Manager': '1', 'Origin': 'http://evil.test'}, json={}).status_code == 403


def test_active_trae_account_persists_and_explicit_selection_wins(manager, tmp_path, monkeypatch):
    from codebuddy_proxy.trae_accounts import selected_record
    from codebuddy_proxy.trae_models import selected_record as selected_model_record
    from codebuddy_proxy.trae_gateway import load_account
    first = wait_job(manager, manager.start_login())['account_id']
    assert manager.snapshot()['active_account_id'] == first
    Flow.uid = 'user-2'
    second = wait_job(manager, manager.start_login())['account_id']
    assert manager.snapshot()['active_account_id'] == first
    manager.activate(second)
    assert manager.snapshot()['active_account_id'] == second
    with manager.lock:
        assert selected_record(manager)['id'] == second
        assert selected_record(manager, first)['id'] == first
    assert selected_model_record(manager, None)[0]['id'] == second
    assert selected_model_record(manager, first)[0]['id'] == first
    class FakeAuth:
        def __init__(self, creds_file):
            uid = json.loads(creds_file.read_text(encoding='utf-8'))['auth']['userId']
            self.creds = SimpleNamespace(user_id=uid)
            self.device = SimpleNamespace(device_id='device', machine_id='machine')
            self.http = SimpleNamespace(close=lambda: None)
        def load(self): pass
        def token_valid(self): return True
        def get_jwt(self): return 'jwt'
    monkeypatch.setattr('buddy_agent_auth.providers.trae.auth.TraeAuth', FakeAuth)
    assert load_account(manager, None)[0] == 'user-2'
    assert load_account(manager, first)[0] == 'user-1'
    other = TraeAccounts(tmp_path)
    try:
        assert other.snapshot()['active_account_id'] == second
    finally:
        asyncio.run(other.close())
    asyncio.run(manager.delete(second))
    assert manager.snapshot()['active_account_id'] == first


def test_trae_activation_requires_marker_and_rejects_unknown(manager):
    account_id = wait_job(manager, manager.start_login())['account_id']
    owner = SimpleNamespace(_lock=threading.RLock(), trae_accounts=manager)
    app = FastAPI()
    app.include_router(create_admin_router(lambda: owner))
    with TestClient(app, base_url='http://localhost', client=('127.0.0.1', 1234)) as client:
        path = f'/admin/api/trae/accounts/{account_id}/activate'
        assert client.post(path).status_code == 403
        headers = {'X-Account-Manager': '1', 'Origin': 'http://localhost'}
        assert client.post(path, headers=headers).json()['active_account_id'] == account_id
        assert client.post('/admin/api/trae/accounts/missing/activate', headers=headers).status_code == 404
        assert client.get('/admin/api/trae/accounts').json()['active_account_id'] == account_id


def test_trae_credits_are_for_requested_account(manager, monkeypatch):
    import codebuddy_proxy.account_credits as credits
    account_id = wait_job(manager, manager.start_login())['account_id']
    async def fake_credits(selected_manager, selected_id):
        assert selected_manager is manager and selected_id == account_id
        return {'remaining': 10, 'packages': []}
    monkeypatch.setattr(credits, 'trae_credits', fake_credits)
    owner = SimpleNamespace(_lock=threading.RLock(), trae_accounts=manager)
    app = FastAPI()
    app.include_router(create_admin_router(lambda: owner))
    with TestClient(app, base_url='http://localhost', client=('127.0.0.1', 1234)) as client:
        assert client.get(f'/admin/api/trae/accounts/{account_id}/credits').json()['remaining'] == 10
        assert client.get('/admin/api/trae/accounts/unknown/credits').status_code == 404
        async def unknown(*args):
            raise credits.CreditsError('unknown_balance')
        monkeypatch.setattr(credits, 'trae_credits', unknown)
        response = client.get(f'/admin/api/trae/accounts/{account_id}/credits')
        assert response.status_code == 502 and response.json() == {'error': 'unknown_balance'}
        async def private_failure(*args):
            raise ValueError('private-token')
        monkeypatch.setattr(credits, 'trae_credits', private_failure)
        response = client.get(f'/admin/api/trae/accounts/{account_id}/credits')
        assert response.status_code == 502 and 'private-token' not in response.text

def test_installed_scheduler_loads_independent_account_without_network(manager, tmp_path, monkeypatch):
    monkeypatch.setenv('TRAE2API_HOME', str(tmp_path / 'device-data'))
    account_id = 'a' * 32
    directory = manager._path(account_id)
    directory.mkdir()
    (directory / 'account.json').write_text(json.dumps({'uid': 'user-1', 'nickname': 'Name'}), encoding='utf-8')
    (directory / 'credentials.device.secret').write_text('test identity', encoding='utf-8')
    (directory / 'credentials.json').write_text(json.dumps({
        'auth': {'userId': 'user-1', 'token': 'test-token'},
        'device': {'deviceId': '123', 'deviceModel': 'test'},
    }), encoding='utf-8')
    async def run():
        service = await manager._service(manager.snapshot()['accounts'][0])
        assert service.manager.store.account_id == 'user-1'
        assert service.snapshot('user-1')['status'] == 'unknown'
    asyncio.run(run())

def test_prepare_failure_is_specific_and_redacted(tmp_path, monkeypatch, caplog):
    import buddy_agent_auth.providers.trae.login as login
    class FailingFlow(Flow):
        def start(self, **kwargs):
            raise ValueError('secret-token https://private.example/ sensitive response')
    monkeypatch.setattr(login, 'TraeLoginFlow', FailingFlow)
    instance = TraeAccounts(tmp_path)
    try:
        result = wait_job(instance, instance.start_login())
        assert result['status'] == 'failed'
        assert result['error'] == '准备 Trae 登录地址失败，请检查设备准备及网络后重试'
        assert 'stage=preparing' in caplog.text
        assert 'secret-token' not in caplog.text
        assert 'private.example' not in caplog.text
        assert 'secret-token' not in json.dumps(result)
    finally:
        asyncio.run(instance.close())


def test_real_login_flow_with_local_callback(tmp_path, monkeypatch):
    from urllib.parse import parse_qs, urlsplit
    import requests
    from buddy_agent_auth.providers.trae.login import TraeLoginFlow
    monkeypatch.setattr(TraeLoginFlow, 'prepare_device', lambda self: None)
    monkeypatch.setattr(TraeLoginFlow, 'exchange_auth_code', lambda self, code: {
        'Token': 'synthetic-token', 'RefreshToken': 'synthetic-refresh'})
    monkeypatch.setattr(TraeLoginFlow, 'fetch_user_info', lambda self, token: {
        'UserID': 'user-real-flow', 'ScreenName': 'Test User'})
    instance = TraeAccounts(tmp_path)
    try:
        job = instance.start_login()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = instance.job(job['id'])
            if state['auth_url'] or state['status'] != 'pending':
                break
            time.sleep(.01)
        assert state['auth_url']
        callback = parse_qs(urlsplit(state['auth_url']).query)['auth_callback_url'][0]
        response = requests.get(callback, params={'authCodeInfo': json.dumps({'AuthCode': 'fake-code'})}, timeout=5)
        assert response.status_code == 200
        result = wait_job(instance, job)
        assert result['status'] == 'succeeded'
        accounts = instance.snapshot()['accounts']
        assert len(accounts) == 1 and accounts[0]['uid'] == 'user-real-flow'
        assert 'synthetic-token' not in json.dumps(accounts)
        # The real provider must also preserve the saved identity during re-login.
        old_identity = (instance._path(result['account_id']) / 'credentials.device.secret').read_bytes()
        job = instance.start_login(result['account_id'])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = instance.job(job['id'])
            if state['auth_url'] or state['status'] != 'pending':
                break
            time.sleep(.01)
        assert state['auth_url']
        callback = parse_qs(urlsplit(state['auth_url']).query)['auth_callback_url'][0]
        requests.get(callback, params={'authCodeInfo': json.dumps({'AuthCode': 'fake-code'})}, timeout=5).raise_for_status()
        assert wait_job(instance, job)['status'] == 'succeeded'
        assert (instance._path(result['account_id']) / 'credentials.device.secret').read_bytes() == old_identity
    finally:
        asyncio.run(instance.close())
