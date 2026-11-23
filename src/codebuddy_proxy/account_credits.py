"""Read-only, account-scoped WorkBuddy and Trae credit queries."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import math
import time
import uuid
from urllib.parse import urlsplit

import httpx

from codebuddy_proxy.trae_gateway import load_account

WORKBUDDY_PATH = '/v2/billing/meter/get-user-resource'
TRAE_URL = 'https://api.trae.cn/trae/api/v2/pay/ide_user_ent_usage'
SUMMARY_PATH = '/billing/meter/get-user-resource-summary'
PAID_PATH = '/billing/meter/get-user-resource-paid-packages'
FREE_PATH = '/billing/meter/get-user-resource-free-packages'
PAID_CODES = ['TCACA_code_002_AkiJS3ZHF5', 'TCACA_code_023_4xbGhMrE6q', 'TCACA_code_026_BaESVICNoi',
              'TCACA_code_027_0FCGVA6vSa', 'TCACA_code_009_0XmEQc2xOf', 'TCACA_code_038_OhvqZtiPKr']
FREE_CODES = ['TCACA_code_008_cfWoLwvjU4', 'TCACA_code_007_nzdH5h4Nl0', 'TCACA_code_028_NtpWi0jzXs',
              'TCACA_code_029_6wCGEWquYy', 'TCACA_code_030_BjSt89qTvr']


class CreditsError(Exception):
    """A safe, user-facing classification; never includes upstream bodies."""


def _error_code(document):
    if not isinstance(document, dict):
        return None
    nested = document.get('data')
    code = document.get('code', nested.get('code') if isinstance(nested, dict) else None)
    return str(code) if code is not None else None


def _number(value):
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return None
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    return float(value)


def _items(document, key):
    if not isinstance(document, dict):
        return None
    nested = document.get('data')
    code = document.get('code', nested.get('code', 0) if isinstance(nested, dict) else 0)
    if code not in (0, 200, '0', '200') or document.get('ok') is False or document.get('success') is False:
        return None
    for path in (('data',), ('data', 'data'), ('data', 'Response', 'Data'), ('data', 'data', 'Response', 'Data')):
        node = document
        for part in path:
            node = node.get(part) if isinstance(node, dict) else None
        if isinstance(node, dict):
            found = node.get(key, node.get(key.lower()))
            if isinstance(found, list):
                return found
    return None


def _expiry(value):
    numeric = _number(value)
    if numeric is not None:
        return numeric / 1000 if abs(numeric) >= 10_000_000_000 else numeric
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        text = value.strip()
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
        if len(text) == 10:
            parsed = parsed.replace(hour=23, minute=59, second=59)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
        return parsed.timestamp()
    except (ValueError, OverflowError):
        return None


def _resource(item):
    if not isinstance(item, dict):
        raise CreditsError('unknown_balance')
    slices = item.get('SlicePeriodUsageDetails') or item.get('slicePeriodUsageDetails') or []
    extra = slices[0] if isinstance(slices, list) and slices and isinstance(slices[0], dict) else {}
    def first(keys):
        for source in (item, extra):
            for key in keys:
                number = _number(source.get(key))
                if number is not None:
                    return number
        return None
    total = first(('CycleCapacitySizePrecise', 'CycleCapacitySize', 'CycleTotalCapacity', 'CapacitySizePrecise', 'CapacitySize', 'SlicePeriodCapacitySizePrecise', 'SlicePeriodCapacitySize'))
    remaining = first(('CycleCapacityRemainPrecise', 'CycleCapacityRemain', 'CycleRemainCapacity', 'CapacityRemainPrecise', 'CapacityRemain', 'SlicePeriodCapacityRemainPrecise', 'SlicePeriodCapacityRemain'))
    used = first(('CycleCapacityUsedPrecise', 'CycleCapacityUsed', 'CycleUsedCapacity', 'CapacityUsedPrecise', 'CapacityUsed', 'SlicePeriodCapacityUsedPrecise', 'SlicePeriodCapacityUsed'))
    if remaining is None:
        if total is None or used is None:
            raise CreditsError('unknown_balance')
        remaining = total - used
    if total is None and used is not None:
        total = remaining + used
    timestamps = [_expiry(item.get(key)) for key in ('DeductionEndTime', 'deductionEndTime', 'ExpiredTime', 'expiredTime', 'CycleEndTime', 'cycleEndTime')]
    expiry = min((stamp for stamp in timestamps if stamp is not None), default=None)
    name = item.get('PackageName') or item.get('packageName')
    code = item.get('PackageCode') or item.get('packageCode')
    return {'name': name[:200] if isinstance(name, str) else None,
            'code': code if isinstance(code, str) else None,
            'remaining': max(0, remaining), 'total': total, 'expire_at': expiry,
            'expired': expiry is not None and expiry <= time.time()}


def parse_workbuddy_resources(summary, paid, free):
    groups = [_items(summary, 'Packages'), _items(paid, 'Accounts'), _items(free, 'Accounts')]
    if all(group is None for group in groups):
        raise CreditsError('unknown_balance')
    details = [_resource(item) for group in groups[1:] if group is not None for item in group]
    detail_codes = {item['code'] for item in details if item['code']}
    summaries = [_resource(item) for item in (groups[0] or [])]
    packages = details + [item for item in summaries if not item['code'] or item['code'] not in detail_codes]
    return {'remaining': round(sum(item['remaining'] for item in packages if not item['expired']), 2),
            'packages': packages, 'partial': any(group is None for group in groups)}


def parse_workbuddy(document):
    if not isinstance(document, dict) or document.get('code', 0) != 0:
        raise CreditsError('upstream_error')
    try:
        data = document['data']['Response']['Data']
        items = data['Accounts']
    except (KeyError, TypeError):
        raise CreditsError('unknown_balance') from None
    if not isinstance(items, list):
        raise CreditsError('unknown_balance')
    packages = []
    for item in items:
        if not isinstance(item, dict):
            raise CreditsError('unknown_balance')
        size = _number(item.get('CycleCapacitySize')) or 0
        cycle_remain = _number(item.get('CycleCapacityRemain'))
        cycle_used = _number(item.get('CycleCapacityUsed')) or 0
        capacity_remain = _number(item.get('CapacityRemain'))
        if size > 0 or (cycle_remain is not None and cycle_remain > 0) or cycle_used > 0:
            remaining = cycle_remain
            total = size or None
        else:
            remaining = capacity_remain
            total = _number(item.get('CapacitySize'))
        if remaining is None:
            raise CreditsError('unknown_balance')
        name = item.get('PackageName')
        packages.append({'name': name[:200] if isinstance(name, str) and name else None,
                         'remaining': max(0, remaining), 'total': total})
    count = data.get('TotalCount')
    if type(count) is int and count > len(items):
        raise CreditsError('incomplete_balance')
    return {'remaining': round(sum(p['remaining'] for p in packages), 2), 'packages': packages}


def parse_trae(document):
    if not isinstance(document, dict) or document.get('code', 0) != 0 or document.get('error_code', 0) != 0:
        raise CreditsError('upstream_error')
    packs = document.get('user_entitlement_pack_list')
    if not isinstance(packs, list):
        raise CreditsError('unknown_balance')
    packages = []
    for pack in packs:
        if not isinstance(pack, dict):
            continue
        base = pack.get('entitlement_base_info') or {}
        quota = base.get('quota') or {} if isinstance(base, dict) else {}
        limit = _number(quota.get('credits_limit')) if isinstance(quota, dict) else None
        if limit is None:
            continue
        usage = pack.get('usage') or {}
        used = _number(usage.get('credits_amount')) if isinstance(usage, dict) else None
        if used is None:
            used = 0
        name = pack.get('group_name') or pack.get('display_desc') or base.get('package_name') or base.get('name')
        expire = pack.get('expire_time')
        packages.append({'name': name[:200] if isinstance(name, str) else None,
                         'total': round(limit, 2), 'used': round(used, 2),
                         'remaining': round(max(0, limit - used), 2),
                         'expire_at': expire if type(expire) is int and expire > 0 else None})
    if packs and not packages:
        raise CreditsError('unknown_balance')
    return {'remaining': round(sum(p['remaining'] for p in packages), 2), 'packages': packages}


async def workbuddy_credits(client):
    ensure = getattr(client, 'ensure_authenticated', None)
    if callable(ensure):
        try:
            await asyncio.to_thread(ensure, open_browser=False, allow_login=False)
        except Exception:
            raise CreditsError('authentication_error') from None
    session, _ = client._snapshot_session()
    auth_headers = client._auth_headers_for_session(session)
    if not auth_headers.get('Authorization'):
        raise CreditsError('authentication_error')
    headers = {key: value for key, value in auth_headers.items()
               if key.lower() in {'authorization', 'x-user-id', 'x-enterprise-id', 'x-tenant-id', 'x-domain'}}
    headers.update({'Accept': 'application/json', 'Content-Type': 'application/json'})
    endpoint = client.endpoint.rstrip('/')
    legacy_endpoint = endpoint
    parts = urlsplit(endpoint)
    # The official CN chat host and billing host differ; explicitly configured
    # alternative hosts retain their own origin and credentials stay scoped there.
    if client.profile.key == 'domestic' and parts.scheme == 'https' and parts.netloc.lower() == 'copilot.tencent.com':
        domain = str((session.get('auth') or {}).get('domain') or '').lower().strip()
        endpoint = 'https://www.workbuddy.cn' if domain in ('workbuddy.cn', 'www.workbuddy.cn') else 'https://www.codebuddy.cn'
        legacy_endpoint = 'https://www.codebuddy.cn'
    headers.update({'X-Client-Platform': 'web', 'Accept': 'application/json, text/plain, */*',
                    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36',
                    'Origin': endpoint, 'Referer': endpoint + '/profile/plans-usage'})
    now = datetime.now()
    body = {'PageNumber': 1, 'PageSize': 100, 'ProductCode': 'p_tcaca', 'Status': [0, 3],
            'PackageEndTimeRangeBegin': now.strftime('%Y-%m-%d %H:%M:%S'),
            'PackageEndTimeRangeEnd': (now + timedelta(days=365*101)).strftime('%Y-%m-%d %H:%M:%S')}
    async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as http:
        today = now.strftime('%Y-%m-%d')
        requests = [(SUMMARY_PATH, {}),
                    (PAID_PATH, {'PageNumber': 1, 'PageSize': 200, 'Status': [0, 3], 'PackageCodes': PAID_CODES, 'NeedRenewInfo': True}),
                    (FREE_PATH, {'PageNumber': 1, 'PageSize': 200, 'Status': [0, 3], 'SlicePeriodStartTime': today + ' 00:00:00', 'SlicePeriodEndTime': today + ' 23:59:59', 'PackageCodes': FREE_CODES})]
        responses = []
        for path, payload in requests:
            try:
                response = await http.post(endpoint + path, headers=headers, json=payload)
                try:
                    document = response.json()
                except ValueError:
                    document = None
                responses.append(document if response.is_success else None)
            except httpx.HTTPError:
                responses.append(None)
        try:
            result = parse_workbuddy_resources(*responses)
            result['source'] = 'user_center'
        except CreditsError:
            legacy_headers = {**headers, 'Origin': legacy_endpoint, 'Referer': legacy_endpoint + '/profile/plans-usage'}
            response = await http.post(legacy_endpoint + WORKBUDDY_PATH, headers=legacy_headers, json=body)
            try:
                document = response.json()
            except ValueError:
                document = None
            if _error_code(document) == '10085':
                raise CreditsError('upstream_rejected')
            response.raise_for_status()
            result = parse_workbuddy(document)
            result['source'] = 'legacy'
            result['partial'] = False
    return {**result, 'provider': 'workbuddy', 'checked_at': time.time()}


async def trae_credits(accounts, account_id):
    uid, jwt, device, _ = await asyncio.to_thread(load_account, accounts, account_id)
    jwt = jwt.removeprefix('Cloud-IDE-JWT ')
    headers = {'Authorization': 'Cloud-IDE-JWT ' + jwt, 'Content-Type': 'application/json',
               'Accept': '*/*', 'Accept-Language': 'zh-CN', 'User-Agent': 'VSCode 1.107.1 (TRAE SOLO CN)',
               'x-market-client-id': 'VSCode 1.107.1', 'x-user-region': 'CN', 'x-device-id': device,
               'x-lgw-req-sdk-type': '3', 'package-type': 'stable_cn', 'x-request-id': str(uuid.uuid4()),
               'x-lscbd-aid': '787976', 'x-lscbd-platform': 'windows', 'app-version': '0.1.45'}
    async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False) as http:
        response = await http.post(TRAE_URL, headers=headers, json={})
        response.raise_for_status()
        result = parse_trae(response.json())
    return {**result, 'provider': 'trae', 'checked_at': time.time()}
