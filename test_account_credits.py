import asyncio
from types import SimpleNamespace

import httpx
import pytest

from codebuddy_proxy import account_credits as credits


def test_workbuddy_credit_packages_and_unknown_shape():
    body = {'code': 0, 'data': {'Response': {'Data': {'Accounts': [
        {'PackageName': 'monthly', 'CycleCapacitySize': 100, 'CycleCapacityRemain': 42, 'CapacityRemain': 90},
        {'PackageName': 'other', 'CapacitySize': 50, 'CapacityRemain': 8},
    ]}}}}
    result = credits.parse_workbuddy(body)
    assert result['remaining'] == 50
    assert result['packages'][0]['remaining'] == 42
    assert result['packages'][1]['total'] == 50
    with pytest.raises(credits.CreditsError, match='unknown_balance'):
        credits.parse_workbuddy({'code': 0, 'data': {}})
    body['data']['Response']['Data']['TotalCount'] = 3
    with pytest.raises(credits.CreditsError, match='incomplete_balance'):
        credits.parse_workbuddy(body)


def test_trae_credits_and_missing_quota():
    body = {'code': 0, 'user_entitlement_pack_list': [
        {'group_name': 'monthly', 'entitlement_base_info': {'quota': {'credits_limit': 100}},
         'usage': {'credits_amount': 15}, 'expire_time': 2000000000},
        {'entitlement_base_info': {'quota': {'credits_limit': 10}}, 'usage': {'credits_amount': 20}},
        {'entitlement_base_info': {'quota': {'fast_request_limit': 20}}},
    ]}
    result = credits.parse_trae(body)
    assert result['remaining'] == 85
    assert result['packages'][0]['name'] == 'monthly'
    assert result['packages'][0]['expire_at'] == 2000000000
    assert result['packages'][1]['remaining'] == 0
    with pytest.raises(credits.CreditsError, match='unknown_balance'):
        credits.parse_trae({'user_entitlement_pack_list': [{'entitlement_base_info': {'quota': {}}}]})
    with pytest.raises(credits.CreditsError, match='upstream_error'):
        credits.parse_trae({'code': 1001, 'user_entitlement_pack_list': []})


def test_upstream_requests_are_scoped_to_account(monkeypatch):
    real_client = httpx.AsyncClient
    captured = []
    def handler(request):
        captured.append(request)
        if 'api.trae.cn' in str(request.url):
            return httpx.Response(200, json={'user_entitlement_pack_list': [{'entitlement_base_info': {'quota': {'credits_limit': 5}}}]})
        if request.url.path != credits.WORKBUDDY_PATH:
            return httpx.Response(404, json={'code':404})
        return httpx.Response(200, json={'code': 0, 'data': {'Response': {'Data': {'Accounts': [{'CapacityRemain': 4}]}}}})
    monkeypatch.setattr(credits.httpx, 'AsyncClient', lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(credits, 'load_account', lambda accounts, account_id: ('uid', 'trae-token', 'device-1', 'machine'))
    class Client:
        endpoint = 'https://copilot.tencent.com'
        profile = SimpleNamespace(key='domestic')
        def _snapshot_session(self): return ({}, None)
        def _auth_headers_for_session(self, session): return {'Authorization': 'Bearer wb-token', 'X-User-Id': 'u1', 'X-Product-Code': 'ignored'}
    async def run():
        assert (await credits.workbuddy_credits(Client()))['remaining'] == 4
        assert (await credits.trae_credits(object(), 'selected'))['remaining'] == 5
        assert captured[0].url.host == 'www.codebuddy.cn'
        assert captured[0].headers['authorization'] == 'Bearer wb-token'
        assert 'x-product-code' not in captured[0].headers
        assert captured[-1].headers['authorization'] == 'Cloud-IDE-JWT trae-token'
        assert captured[-1].headers['x-device-id'] == 'device-1'
        assert captured[-1].content == b'{}'
    asyncio.run(run())


def test_user_center_packages_merge_precise_values_and_expired():
    summary = {'code': 0, 'data': {'Packages': [
        {'PackageCode':'a', 'PackageName':'summary duplicate', 'CapacityRemain':99},
        {'PackageCode':'b', 'PackageName':'expired', 'CapacityRemain':9, 'ExpiredTime':'2020-01-01 00:00:00'},
    ]}}
    paid = {'code': 0, 'data': {'Accounts': [
        {'PackageCode':'a', 'PackageName':'paid', 'CycleCapacitySizePrecise':'100.5', 'CycleCapacityRemainPrecise':'42.25'}
    ]}}
    free = {'code': 0, 'data': {'Accounts': []}}
    result = credits.parse_workbuddy_resources(summary, paid, free)
    assert result['remaining'] == 42.25
    assert len(result['packages']) == 2
    assert result['packages'][1]['expired'] is True
    assert result['partial'] is False
    assert credits.parse_workbuddy_resources(summary, None, free)['partial'] is True


def test_waf_error_does_not_trigger_more_requests_or_refresh(monkeypatch):
    real_client = httpx.AsyncClient
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(403, json={'code':10085, 'msg':'private upstream detail'})
    monkeypatch.setattr(credits.httpx, 'AsyncClient', lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    class Client:
        endpoint = 'https://copilot.tencent.com'
        profile = SimpleNamespace(key='domestic')
        def _snapshot_session(self): return ({'auth': {'domain': 'www.workbuddy.cn'}}, None)
        def _auth_headers_for_session(self, session): return {'Authorization':'Bearer test'}
    with pytest.raises(credits.CreditsError, match='upstream_rejected'):
        asyncio.run(credits.workbuddy_credits(Client()))
    assert len(requests) == 1
    assert requests[0].url.host == 'www.workbuddy.cn'
    assert requests[0].url.path == credits.SUMMARY_PATH
    assert requests[0].headers['x-client-platform'] == 'web'
