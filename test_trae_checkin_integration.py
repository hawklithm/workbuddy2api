"""Trae scheduler integration is opt-in and follows application lifecycle."""
import asyncio
import importlib
from types import SimpleNamespace

import daily_checkin.trae_scheduler as scheduler

proxy = importlib.import_module('codebuddy_proxy.__main__')


def test_trae_automation_disabled_by_default(monkeypatch):
    monkeypatch.delenv('DAILYCHECKIN_TRAE_NATIVE', raising=False)
    monkeypatch.delenv('DAILYCHECKIN_TRAE_CREDS', raising=False)
    monkeypatch.setattr(scheduler, 'create_service', lambda **kwargs: (_ for _ in ()).throw(AssertionError()))
    asyncio.run(proxy.start_trae_checkin())
    assert proxy._trae_checkin_service is None


def test_trae_automation_starts_and_closes(monkeypatch):
    calls = []
    class Service:
        async def start(self):
            calls.append('start')
        async def close(self):
            calls.append('close')
    service = Service()
    def create_service(**kwargs):
        calls.append(kwargs)
        return service
    monkeypatch.setenv('DAILYCHECKIN_TRAE_NATIVE', '1')
    monkeypatch.delenv('DAILYCHECKIN_TRAE_CREDS', raising=False)
    monkeypatch.setattr(proxy, 'proxy_state', None)
    monkeypatch.setattr(scheduler, 'create_service', create_service)
    async def run():
        await proxy.start_trae_checkin()
        assert proxy._trae_checkin_service is service
        await proxy.close_trae_checkin()
    asyncio.run(run())
    assert calls == [{'native': True, 'creds': None, 'device_id_source': 'get-device-id'}, 'start', 'close']


def test_trae_setup_failure_does_not_prevent_proxy_start(monkeypatch):
    monkeypatch.setenv('DAILYCHECKIN_TRAE_NATIVE', '1')
    monkeypatch.delenv('DAILYCHECKIN_TRAE_CREDS', raising=False)
    monkeypatch.setattr(scheduler, 'create_service', lambda **kwargs: (_ for _ in ()).throw(ValueError('secret')))
    asyncio.run(proxy.start_trae_checkin())
    assert proxy._trae_checkin_service is None


def test_host_attaches_checkin_logger_before_starting_trae_accounts(monkeypatch):
    events = []
    class Checkins:
        async def start(self):
            events.append('workbuddy_started')
    class Accounts:
        def configure_logging(self, callback):
            self.callback = callback
            events.append('logger_attached')
        async def start(self):
            self.callback('trae_event', brand='trae_cn', stage='service_init')
            events.append('trae_started')
    state = object.__new__(proxy.ProxyState)
    state.account_manager = SimpleNamespace(checkins=Checkins(), trae_accounts=Accounts())
    state.log_checkin = lambda event, **fields: events.append((event, fields))
    monkeypatch.setattr(proxy, 'proxy_state', state)
    asyncio.run(proxy.start_account_checkin())
    assert events == ['workbuddy_started', 'logger_attached',
                      ('trae_event', {'brand': 'trae_cn', 'stage': 'service_init'}), 'trae_started']
