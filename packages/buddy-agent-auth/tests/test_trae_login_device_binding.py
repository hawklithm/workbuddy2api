import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from buddy_agent_auth.providers.trae.accounts import TraeAccountManager
from buddy_agent_auth.providers.trae.device_hardware import Hardware
from buddy_agent_auth.providers.trae.app_storage import bind_login_device, resolve_pair


@pytest.fixture
def hardware():
    return Hardware('profile-uuid', 'profile-serial', 'model', 'brand', 'cpu',
                    'windows', 'version', 'release', '02-11-22-33-44-55')


def test_login_profile_binds_and_checkin_reuses_it(tmp_path, monkeypatch, hardware):
    monkeypatch.setenv('TRAE2API_HOME', str(tmp_path / 'devices'))
    bind_login_device('user-1', '123', hardware)
    bind_login_device('user-1', '123', hardware)
    device, profile = resolve_pair('user-1', 'other', None, '')
    assert (device, profile) == ('123', hardware)
    with pytest.raises(ValueError, match='different device binding'):
        bind_login_device('user-1', '456', hardware)
    assert json.loads((tmp_path / 'devices' / 'device_pair.json').read_text()) == {'user-1': '123'}


def test_login_preallocates_once_before_registration(tmp_path, monkeypatch, hardware):
    from buddy_agent_auth.providers.trae import login
    import httpx
    seen = []
    monkeypatch.setattr(Hardware, 'allocate', classmethod(lambda cls: hardware))
    class DeviceService:
        def __init__(self, store, **kwargs):
            self.store, self.reader = store, kwargs['hardware_reader']
        def get_device_id(self, **kwargs):
            seen.append(self.reader())
            return SimpleNamespace(identity=self.store.load(), hardware=seen[-1])
    monkeypatch.setattr(login, 'AhaDeviceService', DeviceService)
    with httpx.Client() as client:
        flow = login.TraeLoginFlow(creds_file=tmp_path / 'credentials.json', http=client)
        flow.prepare_device()
        monkeypatch.setattr(Hardware, 'allocate', classmethod(lambda cls: pytest.fail('profile regenerated')))
        flow.prepare_device()
    assert seen == [hardware, hardware]
    assert json.loads((tmp_path / 'credentials.hardware.secret').read_text()) == asdict(hardware)


def test_account_commit_binds_only_authenticated_user(tmp_path, monkeypatch, hardware):
    from buddy_agent_auth.providers.trae import login
    monkeypatch.setenv('TRAE2API_HOME', str(tmp_path / 'devices'))
    class Flow:
        uid = 'real-user'
        def __init__(self, creds_file):
            self.path = creds_file
            self.http = SimpleNamespace(close=lambda: None)
        def start(self, **kwargs):
            self.path.write_text('{}')
            self.path.with_name('credentials.device.secret').write_text('device')
            self.path.with_name('credentials.hardware.secret').write_text(json.dumps(asdict(hardware)))
            return SimpleNamespace(creds=SimpleNamespace(user_id=self.uid, username='name'),
                                   device=SimpleNamespace(device_id='123'),
                                   http=SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(login, 'TraeLoginFlow', Flow)
    manager = TraeAccountManager(tmp_path / 'accounts')
    try:
        job = manager.start_login()
        manager.jobs[job['id']]['future'].result(timeout=5)
        assert manager.job(job['id'])['status'] == 'succeeded'
        assert json.loads((tmp_path / 'devices' / 'device_pair.json').read_text()) == {'real-user': '123'}
        assert json.loads((tmp_path / 'devices' / 'device_hardware.json').read_text()) == {'real-user': asdict(hardware)}
        account_id = manager.job(job['id'])['account_id']
        retry = manager.start_login(account_id)
        manager.jobs[retry['id']]['future'].result(timeout=5)
        assert manager.job(retry['id'])['status'] == 'succeeded'
        Flow.uid = 'different-user'
        mismatch = manager.start_login(account_id)
        manager.jobs[mismatch['id']]['future'].result(timeout=5)
        assert manager.job(mismatch['id'])['status'] == 'failed'
        assert json.loads((tmp_path / 'devices' / 'device_pair.json').read_text()) == {'real-user': '123'}
        Flow.uid = 'real-user'
        duplicate = manager.start_login()
        manager.jobs[duplicate['id']]['future'].result(timeout=5)
        assert manager.job(duplicate['id'])['status'] == 'failed'
    finally:
        manager.close()


def test_existing_device_binding_is_never_overwritten(tmp_path, monkeypatch, hardware):
    monkeypatch.setenv('TRAE2API_HOME', str(tmp_path))
    bind_login_device('user', '123', hardware)
    other = Hardware('different', *list(asdict(hardware).values())[1:])
    with pytest.raises(ValueError, match='different hardware profile'):
        bind_login_device('user', '456', other)
    assert json.loads((tmp_path / 'device_pair.json').read_text()) == {'user': '123'}
    assert json.loads((tmp_path / 'device_hardware.json').read_text()) == {'user': asdict(hardware)}
