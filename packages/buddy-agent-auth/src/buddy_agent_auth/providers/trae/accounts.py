"""Independent Trae accounts and cancellable login jobs."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
import traceback
import uuid
from buddy_agent_auth.providers.codebuddy.login_jobs import AccountManagerError
from buddy_agent_auth.providers.codebuddy.account_store import _atomic_json

_ID = re.compile(r'^[0-9a-f]{32}$')
_LOG = logging.getLogger('codebuddy_proxy.trae_accounts')


class _LoginFailure(Exception):
    def __init__(self, public_message):
        self.public_message = public_message


def _login_error(exc, stage):
    if isinstance(exc, _LoginFailure):
        return exc.public_message
    if isinstance(exc, TimeoutError):
        return '登录超时，请重新尝试'
    if isinstance(exc, PermissionError):
        return '无法读写 Trae 账号目录，请检查文件权限'
    return {
        'initializing': '无法初始化 Trae 登录，请检查本机账号目录和认证组件',
        'preparing': '准备 Trae 登录地址失败，请检查设备准备及网络后重试',
        'authorizing': 'Trae 浏览器授权失败，请重新尝试',
        'exchanging': 'Trae 授权信息交换失败，请重新尝试',
        'profile': '获取 Trae 账号信息失败，请重新尝试',
        'committing': '保存 Trae 账号失败，请检查本机账号目录',
    }[stage]



class TraeAccountAuth:
    def _init_trae_auth(self, root):
        self.root = Path(root) / 'trae-cn'
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.jobs = {}
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='trae-login')
        self.closed = False

    def _login_committed(self, account_id):
        """Optional callback after a login has committed."""

    def _path(self, account_id):
        if not isinstance(account_id, str) or not _ID.fullmatch(account_id):
            raise AccountManagerError('未找到该账号', status_code=404)
        return self.root / account_id

    def _records(self):
        records = []
        for path in self.root.iterdir():
            if not path.is_dir() or not _ID.fullmatch(path.name):
                continue
            try:
                data = json.loads((path / 'account.json').read_text(encoding='utf-8'))
                if not isinstance(data, dict) or not isinstance(data.get('uid'), str) or not data['uid']:
                    raise ValueError()
                if not (path / 'credentials.json').is_file() or not (path / 'credentials.device.secret').is_file():
                    raise ValueError()
                records.append({'id': path.name, 'uid': data['uid'], 'nickname': str(data.get('nickname') or '')[:200]})
            except (OSError, ValueError, TypeError):
                raise AccountManagerError('Trae 账号数据损坏，请检查账号目录', status_code=503) from None
        return records

    def start_login(self, account_id=None):
        with self.lock:
            if self.closed:
                raise AccountManagerError('账号管理器已关闭', status_code=503)
            if len(self.jobs) >= 100:
                for old_id, old_job in list(self.jobs.items()):
                    if old_job['future'].done():
                        del self.jobs[old_id]
                        if len(self.jobs) < 100:
                            break
            if sum(not j['future'].done() for j in self.jobs.values()) >= 2:
                raise AccountManagerError('当前登录任务较多，请稍后重试', status_code=429)
            target = next((x for x in self._records() if x['id'] == account_id), None) if account_id else None
            if account_id and not target:
                raise AccountManagerError('未找到该账号', status_code=404)
            if account_id and any(j['status'] == 'pending' and j['target'] == account_id for j in self.jobs.values()):
                raise AccountManagerError('该账号已有登录任务', status_code=409)
            job = {'id': uuid.uuid4().hex, 'target': account_id, 'status': 'pending', 'auth_url': None,
                   'account_id': None, 'error': None, 'flow': None}
            self.jobs[job['id']] = job
            job['future'] = self.pool.submit(self._login, job, target)
            return self._public(job)

    @staticmethod
    def _public(job):
        return {key: job[key] for key in ('id', 'status', 'auth_url', 'account_id', 'error')}

    def job(self, job_id, cancel=False):
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                raise AccountManagerError('未找到该登录任务', status_code=404)
            if cancel and job['status'] == 'pending':
                job['status'] = 'cancelled'
                job['auth_url'] = None
                if job['flow'] is not None:
                    job['flow'].cancel()
            return self._public(job)

    def _login(self, job, target):
        stage = 'initializing'
        try:
            with tempfile.TemporaryDirectory(prefix='.login-', dir=self.root) as temp:
                directory = Path(temp)
                credentials = directory / 'credentials.json'
                original_identity = None
                if target:
                    source = self._path(target['id'])
                    shutil.copyfile(source / 'credentials.json', credentials)
                    identity = source / 'credentials.device.secret'
                    original_identity = identity.read_bytes()
                    shutil.copyfile(identity, directory / identity.name)
                from buddy_agent_auth.providers.trae.login import TraeLoginFlow
                flow = TraeLoginFlow(creds_file=credentials)
                with self.lock:
                    if job['status'] != 'pending' or self.closed:
                        flow.http.close()
                        return
                    job['flow'] = flow
                def on_event(event, value=None):
                    nonlocal stage
                    if event in {'exchanging', 'profile'}:
                        stage = event
                    elif event == 'auth_url' and isinstance(value, str):
                        stage = 'authorizing'
                        with self.lock:
                            if job['status'] == 'pending':
                                job['auth_url'] = value
                flow.on_event = on_event
                stage = 'preparing'
                auth = flow.start(open_browser=False, timeout_sec=300, log=lambda message: None)
                stage = 'committing'
                try:
                    uid = str(auth.creds.user_id or '')
                    nickname = str(auth.creds.username or '')[:200]
                finally:
                    auth.http.close()
                if not uid:
                    raise ValueError('missing user')
                with self.lock:
                    if self.closed or job['status'] != 'pending':
                        return
                    records = self._records()
                    if target:
                        if not any(x['id'] == target['id'] and x['uid'] == uid for x in records):
                            raise _LoginFailure('登录账号与所选账号不一致，或所选账号已删除')
                        if (directory / 'credentials.device.secret').read_bytes() != original_identity:
                            raise _LoginFailure('设备身份已变化，未替换原账号凭据')
                        os.replace(credentials, self._path(target['id']) / 'credentials.json')
                        account_id = target['id']
                    else:
                        if any(x['uid'] == uid for x in records):
                            raise _LoginFailure('该 Trae 账号已关联，请使用重新登录')
                        account_id = uuid.uuid4().hex
                        _atomic_json(directory / 'account.json', {'uid': uid, 'nickname': nickname})
                        os.rename(directory, self._path(account_id))
                    job.update(status='succeeded', account_id=account_id, auth_url=None)
                    self._login_committed(account_id)
        except Exception as exc:
            frames = traceback.extract_tb(exc.__traceback__)
            location = f'{Path(frames[-1].filename).name}:{frames[-1].lineno}' if frames else 'unknown'
            with self.lock:
                if job['status'] == 'pending':
                    # Provider exceptions can include tokens, URLs, or response bodies.
                    # Record only a fixed stage, exception type and source location.
                    _LOG.warning('Trae login failed: stage=%s type=%s location=%s',
                                 stage, type(exc).__name__, location)
                    job.update(status='failed', error=_login_error(exc, stage), auth_url=None)
        finally:
            with self.lock:
                job['flow'] = None

    def _delete_record(self, account_id):
        with self.lock:
            if not any(r['id'] == account_id for r in self._records()):
                raise AccountManagerError('未找到该账号', status_code=404)
            if any(j['target'] == account_id and j['status'] == 'pending' for j in self.jobs.values()):
                raise AccountManagerError('请先取消该账号的登录任务', status_code=409)
            shutil.rmtree(self._path(account_id))

    def _stop_logins(self):
        with self.lock:
            self.closed = True
            for job in self.jobs.values():
                if job['status'] == 'pending':
                    job['status'] = 'cancelled'
                    job['auth_url'] = None
                    if job['flow']:
                        job['flow'].cancel()
        self.pool.shutdown(wait=False, cancel_futures=True)


class TraeAccountManager(TraeAccountAuth):
    """Standalone Trae account store and login coordinator."""
    def __init__(self, root):
        self._init_trae_auth(root)

    def list_accounts(self):
        with self.lock:
            return self._records()

    def credentials_file(self, account_id):
        with self.lock:
            if not any(record['id'] == account_id for record in self._records()):
                raise AccountManagerError('未找到该账号', status_code=404)
            return self._path(account_id) / 'credentials.json'

    def delete(self, account_id):
        self._delete_record(account_id)

    def close(self):
        self._stop_logins()
