"""Brand-specific authentication providers remain independently usable."""
import json
from pathlib import Path

import pytest

from buddy_agent_auth.providers.trae import TraeNativeProvider, NativeIdentityError
from buddy_agent_auth.providers.codebuddy import CodeBuddyProvider
from buddy_agent_auth.providers.trae.credentials import load_file


def test_trae_provider_reads_only_account_and_detects_changed_token(tmp_path, monkeypatch):
    from buddy_agent_auth.providers.trae import native_identity
    storage = tmp_path / 'User/globalStorage/storage.json'
    storage.parent.mkdir(parents=True)
    def write(token):
        data = {'userId': 'u', 'token': token, 'account': {'scope': 'marscode'}}
        storage.write_text(json.dumps({'iCubeAuthInfo://icube.cloudide': json.dumps(data)}))
    write('secret-token')
    monkeypatch.setattr(native_identity, 'decrypt_value', lambda value: value)
    identity = TraeNativeProvider(directory=tmp_path).load()
    assert identity.user_id == 'u' and identity.token == 'secret-token'
    assert identity.device_id == '' and not (tmp_path / 'logs').exists()
    assert 'secret-token' not in repr(identity)
    identity.ensure_current()
    write('changed-token')
    with pytest.raises(NativeIdentityError, match='changed'):
        identity.ensure_current()


def test_trae_file_provider_supports_legacy_and_explicit_device(tmp_path):
    path = tmp_path / 'account.json'
    path.write_text(json.dumps({'auth': {'userId': 'u', 'token': 'secret', 'deviceId': 'old'},
                                'device': {'deviceId': 'selected'}}))
    auth, device, token, device_id = load_file(path)
    assert auth['userId'] == 'u' and token == 'secret' and device_id == 'selected'
    path.write_text(json.dumps({'auth': {'token': 'secret', 'deviceId': 'legacy'}}))
    assert load_file(path)[-1] == 'legacy'


def test_codebuddy_provider_uses_selected_account_without_browser():
    class Client:
        def ensure_authenticated(self, **kwargs):
            assert kwargs == {'open_browser': False, 'allow_login': False}
    client = Client()
    class Store:
        def get_client(self, account_id):
            assert account_id == 'account'
            return client
    assert CodeBuddyProvider(Store(), 'account').load() is client


def test_trae_provider_exports_shared_token_lifecycle():
    from buddy_agent_auth.providers.trae import TraeAuth, TraeCredentials
    from buddy_agent_auth.providers.trae.auth import TraeAuth as ConcreteAuth
    assert TraeAuth is ConcreteAuth
    assert TraeCredentials().user_id == ''
