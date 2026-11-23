"""Persistent model-to-provider/account routing for the host proxy."""
from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from buddy_agent_auth.providers.codebuddy.account_store import _atomic_json, _root_lock


class RoutingError(ValueError):
    pass


class ModelRouting:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / 'model-routing.json'
        self.lock = _root_lock(self.root)

    def read(self):
        with self.lock:
            if not self.path.exists():
                return {}
            try:
                value = json.loads(self.path.read_text(encoding='utf-8'))
                if not isinstance(value, dict) or value.get('version') != 1 or not isinstance(value.get('routes'), dict):
                    raise ValueError()
                routes = value['routes']
                for model, target in routes.items():
                    if (not isinstance(model, str) or not model.strip() or not isinstance(target, dict)
                            or target.get('provider') not in ('workbuddy', 'trae')
                            or not isinstance(target.get('account_id'), str) or not target['account_id']):
                        raise ValueError()
                return routes
            except (OSError, ValueError, TypeError):
                raise RoutingError('模型路由配置损坏') from None

    def set(self, model, target):
        if not isinstance(model, str) or not model.strip() or len(model) > 256:
            raise RoutingError('模型名称无效')
        with self.lock:
            routes = self.read()
            if target is None:
                routes.pop(model, None)
            else:
                routes[model] = {'provider': target['provider'], 'account_id': target['account_id']}
            _atomic_json(self.path, {'version': 1, 'routes': routes, 'auto_switch': self.read_auto_switch(), 'auto_assign': self.read_auto_assign()})
        return routes

    def read_auto_switch(self):
        with self.lock:
            self.read()
            if not self.path.exists():
                return {}
            try:
                value = json.loads(self.path.read_text(encoding='utf-8')).get('auto_switch', {})
                if not isinstance(value, dict) or any(not isinstance(k, str) or not k.strip() or type(v) is not bool for k, v in value.items()):
                    raise ValueError()
                return value
            except (OSError, ValueError, TypeError):
                raise RoutingError('模型路由配置损坏') from None

    def set_auto_switch(self, model, enabled):
        if not isinstance(model, str) or not model.strip() or len(model) > 256 or type(enabled) is not bool:
            raise RoutingError('模型自动切换配置无效')
        with self.lock:
            routes, flags = self.read(), self.read_auto_switch()
            if enabled:
                flags[model] = True
            else:
                flags.pop(model, None)
            _atomic_json(self.path, {'version': 1, 'routes': routes, 'auto_switch': flags, 'auto_assign': self.read_auto_assign()})
            return flags

    def read_auto_assign(self):
        with self.lock:
            self.read()
            if not self.path.exists():
                return {}
            try:
                value = json.loads(self.path.read_text(encoding='utf-8')).get('auto_assign', {})
                if not isinstance(value, dict) or any(not isinstance(k, str) or not k.strip() or type(v) is not bool for k, v in value.items()):
                    raise ValueError()
                return value
            except (OSError, ValueError, TypeError):
                raise RoutingError('模型路由配置损坏') from None

    def set_auto_assign(self, model, enabled):
        if not isinstance(model, str) or not model.strip() or len(model) > 256 or type(enabled) is not bool:
            raise RoutingError('模型自动分配配置无效')
        with self.lock:
            routes, flags = self.read(), self.read_auto_assign()
            if enabled:
                flags[model] = True
            else:
                flags.pop(model, None)
            _atomic_json(self.path, {'version': 1, 'routes': routes, 'auto_switch': self.read_auto_switch(), 'auto_assign': flags})
            return flags


class FailoverState:
    """Process-local model selection and consecutive upstream failures."""
    def __init__(self):
        self.lock = threading.RLock()
        self.models = {}

    @staticmethod
    def pair(target):
        return target['provider'], target['account_id']

    def choose(self, root, model, initial, options):
        key, initial_pair = (str(root), model), self.pair(initial)
        eligible = {self.pair(option) for option in options}
        with self.lock:
            state = self.models.get(key)
            if state is None or state['initial'] != initial_pair:
                state = {'initial': initial_pair, 'current': initial_pair, 'failed': set(), 'counts': {}}
                self.models[key] = state
            if state['current'] not in eligible and state['current'] != initial_pair:
                state['current'] = initial_pair
            provider, account_id = state['current']
            return {'provider': provider, 'account_id': account_id}

    def record(self, root, model, initial, target, options, outcome):
        key, pair = (str(root), model), self.pair(target)
        with self.lock:
            state = self.models.get(key)
            if not state or state['initial'] != self.pair(initial) or state['current'] != pair:
                return
            if outcome == 'success':
                state['counts'][pair] = 0
                return
            if outcome not in ('quota', 'error'):
                return
            state['counts'][pair] = state['counts'].get(pair, 0) + 1
            if outcome != 'quota' and state['counts'][pair] < 5:
                return
            state['failed'].add(pair)
            for option in options:
                candidate = self.pair(option)
                if candidate not in state['failed']:
                    state['current'] = candidate
                    break

    def clear(self, root, model):
        with self.lock:
            self.models.pop((str(root), model), None)


failover_state = FailoverState()


def provider_defaults(manager, pinned_client=None):
    """Capture each provider's default account without changing either store."""
    if pinned_client is not None and hasattr(manager, 'account_id_for_client'):
        work_id = manager.account_id_for_client(pinned_client)
    else:
        work_id = getattr(manager.store, 'active_account_id', None)
    trae = getattr(manager, 'trae_accounts', None)
    trae_id = None
    if trae is not None:
        with trae.lock:
            trae_id = trae._active_id_locked()
    return {'workbuddy': work_id, 'trae': trae_id}


async def list_candidates(app, manager, refresh=False, defaults=None, include_metadata=False):
    """List only account/model pairs from non-static provider catalogs."""
    from codebuddy_proxy.trae_models import models_for_account
    work = manager.store.list_accounts()
    trae = getattr(manager, 'trae_accounts', None)
    trae_records = trae.snapshot()['accounts'] if trae is not None else []
    if defaults is not None:
        work = [record for record in work if record['id'] == defaults.get('workbuddy')]
        trae_records = [record for record in trae_records if record['id'] == defaults.get('trae')]
    async def wb(record):
        client = manager.store.get_client(record['id'])
        result = await app.state.admin_workbuddy_models(client, refresh)
        if result.get('catalog', {}).get('source') == 'builtin':
            return None
        return record, 'workbuddy', result.get('models', []), result.get('catalog', {}).get('source')
    async def tr(record):
        result = await models_for_account(app, trae, record['id'], refresh)
        if result.get('source') == 'static_fallback':
            return None
        return record, 'trae', result.get('data', []), result.get('source')
    results = await asyncio.gather(*(wb(r) for r in work), *(tr(r) for r in trae_records), return_exceptions=True)
    models, failed = {}, 0
    for result in results:
        if isinstance(result, BaseException) or result is None:
            failed += 1
            continue
        record, provider, entries, source = result
        if not isinstance(entries, list):
            failed += 1
            continue
        for entry in entries:
            model = entry.get('id') if isinstance(entry, dict) else None
            if not isinstance(model, str) or not model.strip():
                continue
            options = models.setdefault(model, [])
            target = {'provider': provider, 'account_id': record['id'],
                      'label': record.get('nickname') or record.get('uid') or provider,
                      'backend': record.get('backend') if provider == 'workbuddy' else 'trae-cn',
                      'source': source}
            if include_metadata:
                target['metadata'] = entry
            if not any(o['provider'] == provider and o['account_id'] == record['id'] for o in options):
                options.append(target)
    return models, failed


def automatic_target(model, candidates):
    """Prefer WorkBuddy only when its default account actually offers the model."""
    options = candidates.get(model, [])
    for provider in ('workbuddy', 'trae'):
        option = next((item for item in options if item['provider'] == provider), None)
        if option is not None:
            return {'provider': provider, 'account_id': option['account_id']}
    return None
