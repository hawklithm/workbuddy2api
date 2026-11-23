"""Durable session bindings and account/model availability."""
import hashlib
import sqlite3
import time
from pathlib import Path

from codebuddy_proxy.session_context import SESSION_HEADERS, text

INACTIVE_BINDING_SECONDS = 30 * 24 * 3600


class SessionRoutingError(ValueError):
    pass


def session_key(body, headers):
    metadata = body.get('metadata') if isinstance(body.get('metadata'), dict) else {}
    headers = {k.lower(): text(v) for k, v in (headers or {}).items()}
    value = (text(metadata.get('conversation_id')) or text(metadata.get('conversationId'))
             or text(body.get('session_id')) or text(metadata.get('session_id'))
             or text(metadata.get('sessionId'))
             or next((headers[key] for key in SESSION_HEADERS if headers.get(key)), '')
             or text(body.get('conversation_id')))
    if not value or len(value) > 512:
        raise SessionRoutingError('自动分配需要客户端提供稳定的 session ID（例如 X-Session-Id 请求头）')
    return hashlib.sha256(('session-routing-v1|' + value).encode()).hexdigest()


class SessionRouting:
    def __init__(self, root):
        self.path = Path(root) / 'session-routing.sqlite3'

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.execute('PRAGMA busy_timeout=10000')
        db.execute('CREATE TABLE IF NOT EXISTS bindings (session TEXT NOT NULL, model TEXT NOT NULL, provider TEXT NOT NULL, account TEXT NOT NULL, updated REAL NOT NULL, PRIMARY KEY(session,model))')
        db.execute('CREATE TABLE IF NOT EXISTS availability (model TEXT NOT NULL, provider TEXT NOT NULL, account TEXT NOT NULL, failures INTEGER NOT NULL DEFAULT 0, blocked_until REAL NOT NULL DEFAULT 0, reason TEXT, PRIMARY KEY(model,provider,account))')
        return db

    def resolve(self, session, model, options, catalog_incomplete=False, known_accounts=None):
        now = time.time()
        eligible = {(o['provider'], o['account_id']) for o in options}
        db = self.connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM bindings WHERE updated<?', (now - INACTIVE_BINDING_SECONDS,))
            row = db.execute('SELECT provider,account FROM bindings WHERE session=? AND model=?', (session, model)).fetchone()
            blocked = {tuple(r) for r in db.execute('SELECT provider,account FROM availability WHERE model=? AND blocked_until>?', (model, now))}
            if row and row not in blocked and (row in eligible or (catalog_incomplete and known_accounts is not None and row in known_accounts)):
                db.execute('UPDATE bindings SET updated=? WHERE session=? AND model=?', (now, session, model))
                db.commit()
                return {'provider': row[0], 'account_id': row[1]}
            candidates = eligible - blocked
            if not candidates:
                db.rollback()
                raise SessionRoutingError('该模型没有可用的候选账号')
            loads = {(p, a): count for p, a, count in db.execute('SELECT provider,account,COUNT(*) FROM bindings WHERE model=? GROUP BY provider,account', (model,))}
            provider, account = min(candidates, key=lambda pair: (loads.get(pair, 0), pair[0] != 'workbuddy', pair))
            db.execute('INSERT INTO bindings(session,model,provider,account,updated) VALUES(?,?,?,?,?) ON CONFLICT(session,model) DO UPDATE SET provider=excluded.provider,account=excluded.account,updated=excluded.updated', (session, model, provider, account, now))
            db.commit()
            return {'provider': provider, 'account_id': account}
        finally:
            db.close()

    def record(self, model, target, outcome, switch_errors):
        provider, account, now = target['provider'], target['account_id'], time.time()
        db = self.connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT failures,blocked_until FROM availability WHERE model=? AND provider=? AND account=?', (model, provider, account)).fetchone()
            failures, until = row or (0, 0)
            if until > now:
                db.commit()
                return
            if until:
                failures, until = 0, 0
            if outcome == 'success':
                failures, until, reason = 0, 0, None
            elif outcome == 'quota':
                failures, until, reason = 0, now + 3600, 'quota'
            elif outcome == 'error' and switch_errors:
                failures += 1
                until, reason = (now + 60, 'errors') if failures >= 5 else (0, None)
            else:
                db.commit()
                return
            db.execute('INSERT INTO availability(model,provider,account,failures,blocked_until,reason) VALUES(?,?,?,?,?,?) ON CONFLICT(model,provider,account) DO UPDATE SET failures=excluded.failures,blocked_until=excluded.blocked_until,reason=excluded.reason', (model, provider, account, failures, until, reason))
            db.commit()
        finally:
            db.close()

    def clear_bindings(self, model):
        db = self.connect()
        try:
            db.execute('DELETE FROM bindings WHERE model=?', (model,))
        finally:
            db.close()

    def binding_counts(self):
        db = self.connect()
        try:
            result = {}
            for model, provider, account, count in db.execute('SELECT model,provider,account,COUNT(*) FROM bindings GROUP BY model,provider,account'):
                result.setdefault(model, []).append({'provider': provider, 'account_id': account, 'count': count})
            return result
        finally:
            db.close()
