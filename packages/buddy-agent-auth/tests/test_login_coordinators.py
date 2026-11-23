"""Authentication coordinators work without a host application's modules."""
import json
import threading
import time
from types import SimpleNamespace

from buddy_agent_auth.providers.codebuddy.login_jobs import WorkBuddyLoginManager
from buddy_agent_auth.providers.trae.accounts import TraeAccountManager


def test_workbuddy_login_jobs_standalone(tmp_path):
    class Store:
        root = tmp_path
        def __init__(self): self.rows = []
        def list_accounts(self): return list(self.rows)
        def import_client(self, client, activate=False):
            self.rows.append({'id':'id', 'backend':client.backend, 'endpoint':client.endpoint,
                              'uid':client.session['account']['uid']})
            return 'id'
    class Client:
        backend = 'domestic'
        endpoint = 'https://example.test'
        session = {'account': {'uid': 'u'}}
        def begin_login(self): return {'auth_url':'https://example.test/login', 'state':'state'}
        def complete_login(self, challenge, timeout, cancel_event): pass
    manager = WorkBuddyLoginManager(Store(), client_factory=lambda backend, path: Client())
    try:
        job = manager.start_login('domestic')
        manager._jobs[job['id']].future.result(timeout=5)
        assert manager.get_login(job['id'])['account_id'] == 'id'
    finally:
        manager.close()


def test_trae_login_jobs_standalone(tmp_path, monkeypatch):
    import buddy_agent_auth.providers.trae.login as login
    class Flow:
        def __init__(self, creds_file):
            self.creds_file = creds_file
            self.http = SimpleNamespace(close=lambda: None)
            self.done = threading.Event()
            self.on_event = None
        def cancel(self): self.done.set()
        def start(self, open_browser, timeout_sec, log):
            self.on_event('auth_url', 'https://example.test/login')
            self.creds_file.write_text(json.dumps({'auth': {'userId':'u', 'token':'private'}}), encoding='utf-8')
            self.creds_file.with_name('credentials.device.secret').write_text('device', encoding='utf-8')
            return SimpleNamespace(creds=SimpleNamespace(user_id='u', username='name'), http=SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(login, 'TraeLoginFlow', Flow)
    manager = TraeAccountManager(tmp_path)
    try:
        job = manager.start_login()
        manager.jobs[job['id']]['future'].result(timeout=5)
        result = manager.job(job['id'])
        assert result['status'] == 'succeeded'
        assert manager.list_accounts()[0]['uid'] == 'u'
        assert manager.credentials_file(result['account_id']).is_file()
        manager.delete(result['account_id'])
        assert manager.list_accounts() == []
    finally:
        manager.close()
