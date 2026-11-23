"""Host-side Trae account selection layered on DailyCheckin coordination."""
import json
from buddy_agent_auth.providers.codebuddy.account_store import _atomic_json
from buddy_agent_auth.providers.codebuddy.login_jobs import AccountManagerError
from daily_checkin.trae_accounts import TraeAccounts as _TraeAccounts


def selected_record(accounts, requested=None):
    """Resolve an explicit or current account while the caller holds accounts.lock."""
    records = accounts._records()
    if requested:
        match = next((record for record in records if record['id'] == requested), None)
    else:
        get_active = getattr(accounts, '_active_id_locked', None)
        active = get_active(records) if get_active else (records[0]['id'] if len(records) == 1 else None)
        match = next((record for record in records if record['id'] == active), None)
    if match is None:
        raise ValueError('select a linked Trae account')
    return match


class TraeAccounts(_TraeAccounts):
    """Keep proxy account selection independent of login and check-in state."""

    def start_login(self, account_id=None):
        with self.lock:
            records = self._records()
            path = self.root / 'active-account.json'
            # A sole account is implicitly active. Preserve it when a second
            # account is linked, even if the new login is later cancelled.
            if account_id is None and len(records) == 1 and not path.exists():
                try:
                    _atomic_json(path, {'account_id': records[0]['id']})
                except OSError:
                    raise AccountManagerError('无法保存 Trae 当前账号', status_code=500) from None
            return super().start_login(account_id)

    def _active_id_locked(self, records=None):
        records = self._records() if records is None else records
        path = self.root / 'active-account.json'
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
                if not isinstance(data, dict) or not isinstance(data.get('account_id'), str):
                    raise ValueError()
                account_id = data['account_id']
                if any(record['id'] == account_id for record in records):
                    return account_id
            except (OSError, ValueError, TypeError):
                raise AccountManagerError('Trae 当前账号状态损坏', status_code=503) from None
        return records[0]['id'] if len(records) == 1 else None

    def activate(self, account_id):
        with self.lock:
            if not any(record['id'] == account_id for record in self._records()):
                raise AccountManagerError('未找到该账号', status_code=404)
            try:
                _atomic_json(self.root / 'active-account.json', {'account_id': account_id})
            except OSError:
                raise AccountManagerError('无法保存 Trae 当前账号', status_code=500) from None
            return {'active_account_id': account_id}

    def snapshot(self):
        with self.lock:
            result = super().snapshot()
            result['active_account_id'] = self._active_id_locked(result['accounts'])
            return result

    async def delete(self, account_id):
        await super().delete(account_id)
        with self.lock:
            path = self.root / 'active-account.json'
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding='utf-8'))
                    if isinstance(data, dict) and data.get('account_id') == account_id:
                        path.unlink()
                except (OSError, ValueError, TypeError):
                    raise AccountManagerError('无法更新 Trae 当前账号状态', status_code=500) from None

__all__ = ["TraeAccounts"]
