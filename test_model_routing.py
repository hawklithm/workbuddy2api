import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from codebuddy_proxy.model_routing import ModelRouting, RoutingError, list_candidates, automatic_target


def test_persistent_rules_and_corruption(tmp_path):
    store = ModelRouting(tmp_path)
    assert store.read() == {}
    store.set('shared', {'provider': 'trae', 'account_id': 'a'})
    assert ModelRouting(tmp_path).read()['shared']['provider'] == 'trae'
    store.set('shared', None)
    assert store.read() == {}
    (tmp_path / 'model-routing.json').write_text('{', encoding='utf-8')
    with pytest.raises(RoutingError):
        store.read()


def test_candidates_match_exact_model_and_exclude_static(tmp_path, monkeypatch):
    import codebuddy_proxy.trae_models as trae_models
    class Store:
        root = tmp_path
        def list_accounts(self):
            return [{'id':'w','nickname':'WB','backend':'domestic'}, {'id':'offline','backend':'global'}]
        def get_client(self, account_id): return account_id
    manager = SimpleNamespace(store=Store(), trae_accounts=SimpleNamespace(snapshot=lambda: {'accounts':[{'id':'t','nickname':'Trae'}]}))
    async def wb(client, refresh):
        return {'models':[{'id':'shared'},{'id':'wb-only'}], 'catalog':{'source':'builtin' if client == 'offline' else 'live'}}
    async def trae(app, accounts, account_id, refresh):
        return {'data':[{'id':'shared'},{'id':'trae-only'}], 'source':'upstream'}
    monkeypatch.setattr(trae_models, 'models_for_account', trae)
    models, failed = asyncio.run(list_candidates(SimpleNamespace(state=SimpleNamespace(admin_workbuddy_models=wb)), manager))
    assert [option['provider'] for option in models['shared']] == ['workbuddy', 'trae']
    assert [option['provider'] for option in models['trae-only']] == ['trae']
    assert all(option['account_id'] != 'offline' for options in models.values() for option in options)
    assert failed == 1
    defaults, failed = asyncio.run(list_candidates(SimpleNamespace(state=SimpleNamespace(admin_workbuddy_models=wb)), manager,
                                                   defaults={'workbuddy':'w','trae':'t'}))
    assert automatic_target('shared', defaults) == {'provider':'workbuddy','account_id':'w'}
    assert automatic_target('trae-only', defaults) == {'provider':'trae','account_id':'t'}
    assert automatic_target('not-offered', defaults) is None
    assert failed == 0


def test_unconfigured_model_uses_provider_default_offering_it(tmp_path, monkeypatch):
    from fastapi import HTTPException
    from fastapi.responses import JSONResponse
    import codebuddy_proxy.__main__ as proxy
    import codebuddy_proxy.model_routing as routing
    import codebuddy_proxy.trae_gateway as gateway
    accounts = SimpleNamespace(lock=threading.RLock(), _records=lambda: [{'id':'t'}])
    manager = SimpleNamespace(store=SimpleNamespace(root=tmp_path, get_client=lambda value: 'wb-client'), trae_accounts=accounts)
    state = SimpleNamespace(account_runtime=manager, client='original')
    monkeypatch.setattr(routing, 'provider_defaults', lambda manager, client: {'workbuddy':'w', 'trae':'t'})
    async def candidates(app, manager, **kwargs):
        assert kwargs['defaults'] == {'workbuddy':'w', 'trae':'t'}
        return {'shared':[{'provider':'trae','account_id':'t'}, {'provider':'workbuddy','account_id':'w'}],
                'trae-only':[{'provider':'trae','account_id':'t'}]}, 0
    monkeypatch.setattr(routing, 'list_candidates', candidates)
    async def wb(*args, **kwargs): return JSONResponse({'provider':'workbuddy'})
    async def trae(body, accounts, account_id): return JSONResponse({'provider':'trae', 'account_id':account_id})
    monkeypatch.setattr(proxy, 'forward_chat', wb)
    monkeypatch.setattr(gateway, 'forward_trae', trae)
    async def run():
        token = proxy._request_state.set(state)
        try:
            shared = await proxy.route_model_chat({'model':'shared'}, 'openai')
            assert json.loads(shared.body)['provider'] == 'workbuddy'
            only = await proxy.route_model_chat({'model':'trae-only'}, 'openai')
            assert json.loads(only.body) == {'provider':'trae','account_id':'t'}
            async def rejected(*args, **kwargs): return JSONResponse({'error':'limited'}, status_code=429)
            monkeypatch.setattr(proxy, 'forward_chat', rejected)
            limited = await proxy.route_model_chat({'model':'shared'}, 'openai')
            assert limited.status_code == 429
            with pytest.raises(HTTPException) as error:
                await proxy.route_model_chat({'model':'missing'}, 'openai')
            assert error.value.status_code == 404
        finally:
            proxy._request_state.reset(token)
    asyncio.run(run())


def test_unified_models_include_defaults_from_both_providers(tmp_path, monkeypatch):
    import codebuddy_proxy.__main__ as proxy
    import codebuddy_proxy.trae_models as trae_models
    class Store:
        root = tmp_path
        active_account_id = 'w'
        def list_accounts(self): return [{'id':'w', 'backend':'domestic', 'uid':'wb'}]
        def get_client(self, account_id): return SimpleNamespace(profile=SimpleNamespace(key='domestic'))
    class Trae:
        lock = threading.RLock()
        def _active_id_locked(self): return 't'
        def snapshot(self): return {'accounts':[{'id':'t', 'uid':'trae'}]}
    manager = SimpleNamespace(store=Store(), trae_accounts=Trae(), account_id_for_client=lambda client: 'w')
    state = SimpleNamespace(account_runtime=manager, client=SimpleNamespace(profile=SimpleNamespace(key='domestic', models_resource='fallback')))
    async def wb(client, refresh=False): return {'models':[{'id':'shared','owned_by':'workbuddy'}], 'catalog':{'source':'live'}}
    async def tr(app, accounts, account_id, refresh=False): return {'data':[{'id':'shared'},{'id':'trae-only'}], 'source':'upstream'}
    monkeypatch.setattr(proxy.app.state, 'admin_workbuddy_models', wb)
    monkeypatch.setattr(trae_models, 'models_for_account', tr)
    monkeypatch.setattr(proxy, 'diagnostic', lambda *args, **kwargs: None)
    async def run():
        token = proxy._request_state.set(state)
        try:
            result = await proxy.list_models()
            by_id = {m['id']:m for m in result['models']}
            assert set(by_id) == {'shared','trae-only'}
            assert by_id['shared']['owned_by'] == 'workbuddy'
            assert by_id['trae-only']['owned_by'] == 'trae'
        finally:
            proxy._request_state.reset(token)
    asyncio.run(run())


def test_admin_only_accepts_offered_account_model_pair(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from codebuddy_proxy.admin_api import create_admin_router
    import codebuddy_proxy.model_routing as routing
    async def candidates(app, manager, refresh=False):
        return {'shared':[{'provider':'trae','account_id':'t'}]}, 0
    monkeypatch.setattr(routing, 'list_candidates', candidates)
    manager = SimpleNamespace(store=SimpleNamespace(root=tmp_path))
    app = FastAPI(); app.include_router(create_admin_router(lambda: manager))
    headers = {'X-Account-Manager':'1', 'Origin':'http://localhost'}
    with TestClient(app, base_url='http://localhost', client=('127.0.0.1', 1234)) as client:
        assert client.put('/admin/api/model-routing', json={'model':'shared','target':None}).status_code == 403
        assert client.put('/admin/api/model-routing', headers=headers, json={'model':'shared','target':{'provider':'trae','account_id':'wrong'}}).status_code == 409
        assert client.put('/admin/api/model-routing', headers=headers, json={'model':'shared','target':{'provider':'trae','account_id':'t'}}).status_code == 200
        assert client.get('/admin/api/model-routing').json()['routes']['shared']['account_id'] == 't'


@pytest.mark.parametrize('protocol,finish_event', [('responses', 'response.completed'), ('anthropic', 'message_stop')])
def test_trae_routed_stream_preserves_protocol_events(tmp_path, monkeypatch, protocol, finish_event):
    from fastapi.responses import StreamingResponse
    import codebuddy_proxy.__main__ as proxy
    import codebuddy_proxy.trae_gateway as gateway
    routes = ModelRouting(tmp_path)
    routes.set('shared', {'provider':'trae', 'account_id':'t'})
    accounts = SimpleNamespace(lock=threading.RLock(), _records=lambda: [{'id':'t'}])
    manager = SimpleNamespace(store=SimpleNamespace(root=tmp_path), trae_accounts=accounts)
    state = SimpleNamespace(account_runtime=manager)
    async def fake(*args):
        async def generate():
            yield 'data: {"choices":[{"index":0,"delta":{"role":"assistant","content":"hello"},"finish_reason":null}]}\n\n'
            yield 'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
            yield 'data: [DONE]\n\n'
        return StreamingResponse(generate(), media_type='text/event-stream')
    monkeypatch.setattr(gateway, 'forward_trae', fake)
    async def run():
        token = proxy._request_state.set(state)
        try:
            response = await proxy.route_model_chat({'model':'shared','messages':[],'stream':True}, protocol, original={'model':'shared'})
            data = ''.join([part async for part in response.body_iterator])
            assert 'hello' in data
            assert f'event: {finish_event}' in data
        finally:
            proxy._request_state.reset(token)
    asyncio.run(run())


def test_trae_stream_error_is_not_completed(tmp_path, monkeypatch):
    from fastapi.responses import StreamingResponse
    import codebuddy_proxy.__main__ as proxy
    import codebuddy_proxy.trae_gateway as gateway
    ModelRouting(tmp_path).set('m', {'provider':'trae','account_id':'t'})
    manager = SimpleNamespace(store=SimpleNamespace(root=tmp_path), trae_accounts=SimpleNamespace(lock=threading.RLock(), _records=lambda: [{'id':'t'}]))
    async def fake(*args):
        async def generate():
            yield 'data: {"error":{"message":"failed"}}\n\n'
            yield 'data: [DONE]\n\n'
        return StreamingResponse(generate())
    monkeypatch.setattr(gateway, 'forward_trae', fake)
    async def run():
        token = proxy._request_state.set(SimpleNamespace(account_runtime=manager))
        try:
            response = await proxy.route_model_chat({'model':'m','messages':[],'stream':True}, 'responses', original={'model':'m'})
            data = ''.join([part async for part in response.body_iterator])
            assert 'event: response.failed' in data
            assert 'event: response.completed' not in data
        finally:
            proxy._request_state.reset(token)
    asyncio.run(run())


def test_routed_trae_request_reaches_gateway_with_selected_account(tmp_path, monkeypatch):
    import httpx
    import codebuddy_proxy.__main__ as proxy
    import codebuddy_proxy.trae_gateway as gateway
    ModelRouting(tmp_path).set('shared', {'provider':'trae','account_id':'t'})
    accounts = SimpleNamespace(lock=threading.RLock(), _records=lambda: [{'id':'t'}])
    manager = SimpleNamespace(store=SimpleNamespace(root=tmp_path), trae_accounts=accounts)
    picked, upstream = [], []
    def load(accounts_arg, account_id):
        assert accounts_arg is accounts
        picked.append(account_id)
        return 'user', 'token', 'device', 'machine'
    monkeypatch.setattr(gateway, 'load_account', load)
    real_client = httpx.AsyncClient
    def handler(request):
        upstream.append(request)
        return httpx.Response(200, content=b'event: output\ndata: {"response":"hello"}\n\nevent: done\ndata: {}\n\n')
    monkeypatch.setattr(gateway.httpx, 'AsyncClient', lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    async def run():
        token = proxy._request_state.set(SimpleNamespace(account_runtime=manager))
        try:
            result = await proxy.route_model_chat({'model':'shared','messages':[{'role':'user','content':'hello'}]}, 'openai')
            assert json.loads(result.body)['choices'][0]['message']['content'] == 'hello'
        finally:
            proxy._request_state.reset(token)
    asyncio.run(run())
    assert picked == ['t']
    assert upstream[0].url.path.endswith('/llm_utils_chat')
    assert json.loads(upstream[0].content)['model_name'] == 'shared__dev'


@pytest.mark.parametrize('protocol', ['responses', 'anthropic'])
def test_trae_routed_nonstream_converts_protocol(tmp_path, monkeypatch, protocol):
    from fastapi.responses import JSONResponse
    import codebuddy_proxy.__main__ as proxy
    import codebuddy_proxy.trae_gateway as gateway
    ModelRouting(tmp_path).set('m', {'provider':'trae','account_id':'t'})
    manager = SimpleNamespace(store=SimpleNamespace(root=tmp_path), trae_accounts=SimpleNamespace(lock=threading.RLock(), _records=lambda: [{'id':'t'}]))
    async def fake(*args):
        return JSONResponse({'id':'x','model':'m','choices':[{'message':{'role':'assistant','content':'hello'},'finish_reason':'stop'}]})
    monkeypatch.setattr(gateway, 'forward_trae', fake)
    async def run():
        token = proxy._request_state.set(SimpleNamespace(account_runtime=manager))
        try:
            response = await proxy.route_model_chat({'model':'m','messages':[]}, protocol, original={'model':'m'})
            data = json.loads(response.body)
            assert ('hello' in json.dumps(data))
            assert data['object'] == 'response' if protocol == 'responses' else data['type'] == 'message'
        finally:
            proxy._request_state.reset(token)
    asyncio.run(run())
