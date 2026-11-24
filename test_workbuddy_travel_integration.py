"""Host integration for the WorkBuddy CN travel activity."""
import httpx
import builtins
import threading
from fastapi.testclient import TestClient

import codebuddy_proxy.__main__ as proxy
from codebuddy_proxy.account_manager import AccountManager
from codebuddy_proxy.account_store import AccountStore
from codebuddy_proxy.backend_profile import DOMESTIC_PROFILE, GLOBAL_PROFILE
from test_account_integration import make_client


def test_travel_routes_support_explicit_account_and_opt_in(tmp_path, monkeypatch):
    store = AccountStore(tmp_path / 'accounts')
    domestic = store.import_client(make_client(tmp_path, DOMESTIC_PROFILE, 'first', token='PRIVATE-ONE'))
    global_id = store.import_client(make_client(tmp_path, GLOBAL_PROFILE, 'second', token='PRIVATE-TWO'))
    active_before = store.active_account_id
    manager = AccountManager(store)
    seen = []
    async def handler(request):
        seen.append(request)
        assert request.headers['authorization'] == 'Bearer PRIVATE-ONE'
        if request.url.path.endswith('/status'):
            return httpx.Response(200, json={'code': 0, 'data': {'state': 'idle'}})
        if request.url.path.endswith('/config'):
            return httpx.Response(200, json={'code': 0, 'data': {'locations': [{'id': 7}]}})
        return httpx.Response(200, json={'code': 0, 'data': {}})
    real = httpx.AsyncClient
    manager.travel._client_factory = lambda: real(transport=httpx.MockTransport(handler))
    state = proxy.ProxyState(manager.client, None, None, account_manager=manager)
    monkeypatch.setattr(proxy, 'proxy_state', state)
    try:
        with TestClient(proxy.app, base_url='http://127.0.0.1', client=('127.0.0.1', 50500)) as app:
            assert app.get('/admin/api/travel/settings').json() == {'enabled': False}
            assert app.patch('/admin/api/travel/settings', json={'enabled': True}).status_code == 403
            assert app.post(f'/admin/api/accounts/{domestic}/travel').status_code == 403
            assert app.get(f'/admin/api/accounts/{global_id}/travel').json()['error'] == 'unsupported_region'
            response = app.post(f'/admin/api/accounts/{domestic}/travel', headers={'X-Account-Manager': '1'})
            assert response.json()['action'] == 'departed'
            assert store.active_account_id == active_before
            assert sum(r.method == 'POST' for r in seen) == 1
    finally:
        manager.close()


def test_auto_travel_setting_persists_and_starts_from_host(tmp_path, monkeypatch):
    store = AccountStore(tmp_path / 'accounts')
    account_id = store.import_client(make_client(tmp_path, DOMESTIC_PROFILE, 'first', token='PRIVATE-ONE'))
    manager = AccountManager(store)
    checked = threading.Event()
    async def handler(request):
        return httpx.Response(200, json={'code': 0, 'data': {'state': 'traveling'}})
    real = httpx.AsyncClient
    manager.travel._client_factory = lambda: real(transport=httpx.MockTransport(handler))
    state = proxy.ProxyState(manager.client, None, None, account_manager=manager)
    manager.travel.configure_logging(lambda event, **fields: checked.set() if event == 'workbuddy_travel_result' else None)
    monkeypatch.setattr(proxy, 'proxy_state', state)
    try:
        with TestClient(proxy.app, base_url='http://127.0.0.1', client=('127.0.0.1', 50500)) as app:
            assert not checked.is_set()
            assert app.patch('/admin/api/travel/settings', headers={'X-Account-Manager': '1'},
                             json={'enabled': 'true'}).status_code == 400
            response = app.patch('/admin/api/travel/settings', headers={'X-Account-Manager': '1'},
                                 json={'enabled': True})
            assert response.json() == {'enabled': True}
            assert checked.wait(5)
            assert app.get('/admin/api/travel/settings').json() == {'enabled': True}
            # The account list projects the background service state safely.
            snapshot = app.get('/admin/api/accounts').json()
            row = next(a for a in snapshot['accounts'] if a['id'] == account_id)
            assert row['travel']['action'] == 'traveling'
    finally:
        manager.close()
    reopened = AccountManager(store)
    try:
        assert reopened.travel.enabled() is True
    finally:
        reopened.close()


def test_older_dailycheckin_does_not_break_account_manager(tmp_path, monkeypatch):
    original = builtins.__import__
    def import_without_travel(name, *args, **kwargs):
        if name == 'daily_checkin.workbuddy_travel':
            raise ModuleNotFoundError('old installed package', name=name)
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', import_without_travel)
    manager = AccountManager(AccountStore(tmp_path / 'accounts'))
    try:
        assert manager.travel is None
        assert manager.snapshot()['accounts'] == []
    finally:
        manager.close()
