"""Small browser-free checks for the packaged admin page's inline UI logic."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest


ADMIN_HTML = Path(__file__).parent / "src" / "codebuddy_proxy" / "admin.html"


class _InlineScriptParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._in_script = False
        self.scripts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self._in_script = True
            self.scripts.append("")

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_script = False

    def handle_data(self, data: str) -> None:
        if self._in_script:
            self.scripts[-1] += data


class _TranslationManifestParser(HTMLParser):
    """Collect real translated static nodes for the browser-free DOM harness."""

    def __init__(self) -> None:
        super().__init__()
        self.nodes: list[dict[str, object]] = []
        self._stack: list[tuple[str, dict[str, object] | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        translated = any(key.startswith("data-i18n") for key in attributes)
        node: dict[str, object] | None = None
        if translated:
            node = {"tag": tag, "attributes": attributes, "text": ""}
            self.nodes.append(node)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self._stack.append((tag, node))

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == tag:
                del self._stack[index:]
                break

    def handle_data(self, data: str) -> None:
        for _, node in self._stack:
            if node is not None:
                node["text"] = str(node["text"]) + data


def _inline_script() -> str:
    parser = _InlineScriptParser()
    parser.feed(ADMIN_HTML.read_text(encoding="utf-8"))
    assert parser.scripts, "admin page must have its self-contained controller script"
    return parser.scripts[-1]


_DOM_HARNESS = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const translationManifest = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));

class Element {
  constructor(tag = 'div', id = '') {
    this.tagName = tag.toUpperCase();
    this.id = id;
    this.children = [];
    this.dataset = {};
    this.attributes = {};
    this.listeners = {};
    this.className = '';
    this.textContent = '';
    this.value = '';
    this.lang = '';
    this.hidden = false;
    this.disabled = false;
    this.href = '';
    this.style = {};
    this.classList = { add: (name) => {
      const classes = new Set(this.className.split(/\s+/).filter(Boolean));
      classes.add(name);
      this.className = [...classes].join(' ');
    }};
  }
  append(...nodes) { this.children.push(...nodes); }
  prepend(node) { this.children.unshift(node); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
  async dispatch(name) {
    for (const fn of this.listeners[name] || []) await fn({ target: this });
  }
  querySelector(selector) {
    return selector === 'span' ? this.children.find((child) => child.tagName === 'SPAN') || null : null;
  }
  querySelectorAll(selector) { return walk(this).slice(1).filter((node) => matchesSelector(node, selector)); }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name.startsWith('data-')) this.dataset[name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = String(value);
    if (name === 'lang') this.lang = String(value);
    if (name === 'id') this.id = String(value);
  }
  getAttribute(name) { return this.attributes[name] ?? null; }
  hasAttribute(name) { return Object.hasOwn(this.attributes, name); }
  removeAttribute(name) { delete this.attributes[name]; }
}

function makeTimers() {
  let nextId = 0;
  const timeouts = new Map();
  const intervals = new Map();
  return {
    timeouts,
    intervals,
    setTimeout(fn, delay) { const id = ++nextId; timeouts.set(id, { fn, delay }); return id; },
    clearTimeout(id) { timeouts.delete(id); },
    setInterval(fn, delay) { const id = ++nextId; intervals.set(id, { fn, delay }); return id; },
    clearInterval(id) { intervals.delete(id); },
  };
}

function makePageElements() {
  const ids = [
    'notice', 'account-list', 'refresh-button', 'associate-button', 'login-progress',
    'progress-title', 'progress-copy', 'progress-spinner', 'cancel-button',
    'retry-button', 'manual-auth-link', 'auto-switch-toggle', 'settings-status',
    'auto-checkin-toggle', 'checkin-settings-status', 'sync-status', 'language-select'
  ];
  const elements = Object.fromEntries(ids.map((id) => [id, new Element(
    id === 'associate-button' ? 'button' : id === 'language-select' ? 'select' : 'div', id
  )]));
  const label = new Element('span');
  label.textContent = '关联所选区域';
  elements['associate-button'].append(label);
  return elements;
}

function makeStaticNodes(elements = {}) {
  return translationManifest.map((row) => {
    const element = row.attributes && row.attributes.id && elements[row.attributes.id]
      ? elements[row.attributes.id]
      : new Element(row.tag);
    for (const [name, value] of Object.entries(row.attributes || {})) element.setAttribute(name, value);
    if (!row.attributes || !row.attributes.id || !elements[row.attributes.id]) element.textContent = row.text || '';
    return element;
  });
}

function matchesSelector(node, selector) {
  if (!node || typeof node.hasAttribute !== 'function') return false;
  return selector.split(',').some((part) => {
    const attributeChecks = [...part.matchAll(/\[([^\]=]+)(?:=["']([^"']*)["'])?\]/g)];
    if (!attributeChecks.length) return false;
    return attributeChecks.every((match) => {
      const name = match[1].trim();
      const expected = match[2];
      return node.hasAttribute(name) && (expected === undefined || node.getAttribute(name) === expected);
    });
  });
}

function makeDocument(elements, staticNodes = []) {
  const regions = ['domestic', 'global'].map((backend) => {
    const button = new Element('button');
    button.dataset.backend = backend;
    button.setAttribute('data-backend', backend);
    return button;
  });
  const listeners = {};
  const documentElement = new Element('html');
  documentElement.lang = 'zh-CN';
  const allElements = () => [...new Set([
    ...Object.values(elements).flatMap(walk),
    ...regions,
    ...staticNodes,
    documentElement,
  ])];
  return {
    regions,
    staticNodes,
    documentElement,
    title: 'CodeBuddy 账号管理',
    visibilityState: 'visible',
    getElementById: (id) => elements[id] || null,
    querySelectorAll: (selector) => allElements().filter((node) => matchesSelector(node, selector)),
    createElement: (tag) => new Element(tag),
    addEventListener(name, fn) { (listeners[name] ||= []).push(fn); },
    async dispatch(name) { for (const fn of listeners[name] || []) await fn(); },
  };
}

function makeStorage(initial = {}, { getThrows = false, setThrows = false } = {}) {
  const values = new Map(Object.entries(initial));
  return {
    values,
    getItem(key) {
      if (getThrows) throw new Error('storage read unavailable');
      return values.has(key) ? values.get(key) : null;
    },
    setItem(key, value) {
      if (setThrows) throw new Error('storage write unavailable');
      values.set(key, String(value));
    },
  };
}

function makeWindow(timers, now = () => Date.now(), options = {}) {
  const popup = {
    closed: false,
    opener: null,
    location: { href: '' },
    document: { title: '', body: { textContent: '', style: { cssText: '' } } },
    close() { this.closed = true; },
  };
  const localStorage = Object.hasOwn(options, 'localStorage') ? options.localStorage : makeStorage();
  const navigator = Object.hasOwn(options, 'navigator')
    ? options.navigator
    : { language: 'zh-CN', languages: ['zh-CN'] };
  return {
    popup,
    localStorage,
    navigator,
    window: {
      location: { href: 'http://127.0.0.1:8765/admin' },
      confirm: () => true,
      open: () => popup,
      setTimeout: timers.setTimeout,
      clearTimeout: timers.clearTimeout,
      setInterval: timers.setInterval,
      clearInterval: timers.clearInterval,
      localStorage,
      navigator,
    },
  };
}

function walk(node) {
  return [node, ...(node.children || []).flatMap(walk)];
}

const accountFixture = (overrides = {}) => ({
  id: 'existing', backend: 'domestic', endpoint: 'https://example.test', uid: 'u1', nickname: '旧账号',
  model_cooldowns: [],
  checkin: { date: null, status: 'unchecked', checked: false, last_checked_at: null, last_checkin_at: null, next_checkin_at: null, error: null },
  ...overrides,
});
const settingsFixture = (autoSwitch = false, autoCheckin = false) => ({
  auto_switch_on_429: autoSwitch, auto_checkin_enabled: autoCheckin, default_cooldown_seconds: 60
});
const snapshotFixture = (accounts = [accountFixture()], autoSwitch = false, serverTime = Math.floor(Date.now() / 1000), autoCheckin = false) => {
  const date = new Date(serverTime * 1000 + 8 * 60 * 60 * 1000).toISOString().slice(0, 10);
  return {
    accounts: accounts.map((account) => ({ ...account, checkin: { ...account.checkin, date: account.checkin.date || date } })),
    active_account_id: 'existing', settings: settingsFixture(autoSwitch, autoCheckin), server_time: serverTime,
  };
};

function startPage(document, window, fetch, clock = Date) {
  const script = fs.readFileSync(process.argv[2], 'utf8');
  const DateType = typeof clock === 'function' ? clock : class TestDate extends Date { static now() { return clock.now(); } };
  vm.runInNewContext(script, {
    document, window, fetch, Headers, URL, AbortController, Promise, Date: DateType, Error,
    localStorage: window.localStorage,
    navigator: window.navigator,
  });
}

async function flush() { await new Promise((resolve) => setImmediate(resolve)); }

async function verifyTerminalState(status) {
  const elements = makePageElements();
  const document = makeDocument(elements);
  const timers = makeTimers();
  const { window, popup } = makeWindow(timers);
  const writes = [];
  async function fetch(url, options = {}) {
    const method = options.method || 'GET';
    if (method !== 'GET') writes.push({ url, headers: options.headers });
    let payload;
    if (url.endsWith('/accounts')) {
      payload = snapshotFixture();
    } else if (url.endsWith('/settings') && method === 'GET') {
      payload = settingsFixture();
    } else if (url.endsWith('/logins') && method === 'POST') {
      payload = { id: 'job-1', status, account_id: status === 'succeeded' ? 'new' : null, error: status === 'failed' ? '模拟登录失败' : null };
    } else {
      throw new Error(`Unexpected API request: ${method} ${url}`);
    }
    return { ok: true, status: 200, headers: { get: () => 'application/json' }, json: async () => payload };
  }
  startPage(document, window, fetch);
  await flush();
  await elements['associate-button'].dispatch('click');
  assert.equal(elements['cancel-button'].hidden, true, `${status}: cancel must be hidden after terminal result`);
  assert.equal(elements['cancel-button'].disabled, true, `${status}: cancel must be disabled after terminal result`);
  assert.equal(elements['retry-button'].hidden, status === 'succeeded', `${status}: retry visibility must match terminal result`);
  assert.equal(writes.length, 1);
  assert.equal(writes[0].headers.get('X-Account-Manager'), '1');
  assert.equal(popup.opener, null, 'auth popup must not retain access to the admin window');
}

async function verifyCancelFailureRecovery() {
  const elements = makePageElements();
  const document = makeDocument(elements);
  const timers = makeTimers();
  const { window, popup } = makeWindow(timers);
  const writes = [];
  let deleteCount = 0;
  let loginReads = 0;
  async function fetch(url, options = {}) {
    const method = options.method || 'GET';
    if (method !== 'GET') writes.push({ url, headers: options.headers });
    let response = { ok: true, status: 200, payload: null };
    if (url.endsWith('/accounts')) {
      response.payload = snapshotFixture();
    } else if (url.endsWith('/settings') && method === 'GET') {
      response.payload = settingsFixture();
    } else if (url.endsWith('/logins') && method === 'POST') {
      response.payload = { id: 'job-cancel', status: 'pending' };
    } else if (url.endsWith('/logins/job-cancel') && method === 'GET') {
      loginReads += 1;
      response.payload = { id: 'job-cancel', status: 'pending' };
    } else if (url.endsWith('/logins/job-cancel') && method === 'DELETE') {
      deleteCount += 1;
      if (deleteCount === 1) response = { ok: false, status: 503, payload: { detail: '模拟取消失败' } };
      else response.payload = { id: 'job-cancel', status: 'cancelled' };
    } else {
      throw new Error(`Unexpected API request: ${method} ${url}`);
    }
    return { ok: response.ok, status: response.status, headers: { get: () => 'application/json' }, json: async () => response.payload };
  }
  startPage(document, window, fetch);
  await flush();
  await elements['associate-button'].dispatch('click');
  assert.equal(elements['cancel-button'].hidden, false, 'pending login must expose cancel');
  await elements['cancel-button'].dispatch('click');
  assert.equal(deleteCount, 1);
  assert.equal(elements['cancel-button'].hidden, false, 'failed cancellation must leave cancel available');
  assert.equal(elements['cancel-button'].disabled, false, 'failed cancellation must re-enable cancel');
  assert.match(elements.notice.textContent, /模拟取消失败/);
  assert.ok(loginReads >= 1, 'pending job remains tracked after cancel failure');
  await elements['cancel-button'].dispatch('click');
  assert.equal(deleteCount, 2, 'user can retry cancellation');
  assert.equal(elements['cancel-button'].hidden, true, 'confirmed cancellation hides cancel');
  assert.equal(elements['retry-button'].hidden, false, 'confirmed cancellation exposes retry');
  assert.ok(writes.every((write) => write.headers.get('X-Account-Manager') === '1'));
}

async function verifyAutoSwitchSettings() {
  const elements = makePageElements();
  const document = makeDocument(elements);
  const timers = makeTimers();
  const { window } = makeWindow(timers);
  let remoteSetting = true;
  let patchCount = 0;
  let settingsReads = 0;
  const writes = [];
  async function fetch(url, options = {}) {
    const method = options.method || 'GET';
    if (method !== 'GET') writes.push({ url, headers: options.headers, body: options.body });
    if (url.endsWith('/accounts')) return jsonResponse(snapshotFixture([accountFixture()], remoteSetting));
    if (url.endsWith('/settings') && method === 'GET') {
      settingsReads += 1;
      return jsonResponse(settingsFixture(remoteSetting));
    }
    if (url.endsWith('/settings') && method === 'PATCH') {
      patchCount += 1;
      const desired = JSON.parse(options.body).auto_switch_on_429;
      if (patchCount === 3) return jsonResponse({ detail: '模拟保存失败' }, false, 503);
      remoteSetting = desired;
      return jsonResponse(settingsFixture(remoteSetting));
    }
    throw new Error(`Unexpected API request: ${method} ${url}`);
  }
  startPage(document, window, fetch);
  await flush();
  assert.equal(elements['auto-switch-toggle'].checked, true, 'initial setting must be restored from the server');

  elements['auto-switch-toggle'].checked = false;
  await elements['auto-switch-toggle'].dispatch('change');
  assert.equal(remoteSetting, false, 'turning the switch off must persist');
  assert.equal(elements['auto-switch-toggle'].checked, false);

  elements['auto-switch-toggle'].checked = true;
  await elements['auto-switch-toggle'].dispatch('change');
  assert.equal(remoteSetting, true, 'turning the switch on must persist');
  assert.equal(elements['auto-switch-toggle'].checked, true);

  elements['auto-switch-toggle'].checked = false;
  await elements['auto-switch-toggle'].dispatch('change');
  assert.equal(elements['auto-switch-toggle'].checked, true, 'failed save must restore the last server value');
  assert.equal(elements['auto-switch-toggle'].disabled, false, 'failed save must re-enable the setting');
  assert.match(elements.notice.textContent, /保存结果未确认/);
  assert.match(elements.notice.textContent, /模拟保存失败/);
  assert.ok(settingsReads >= 2, 'an unclear save response must trigger a settings resync');
  assert.ok(writes.every((write) => write.headers.get('X-Account-Manager') === '1'));
  assert.deepEqual(writes.map((write) => JSON.parse(write.body).auto_switch_on_429), [false, true, false]);
}

function jsonResponse(payload, ok = true, status = 200) {
  return { ok, status, headers: { get: () => 'application/json' }, json: async () => payload };
}

async function verifyCooldownPollingAndLocalExpiry() {
  const elements = makePageElements();
  const document = makeDocument(elements);
  const timers = makeTimers();
  const { window } = makeWindow(timers);
  let nowMs = 1900000000000;
  let accountReads = 0;
  const unsafeModel = 'model-<img src=x onerror=alert(1)>';
  const initialModelCooldowns = [
    { model: 'model-a', rate_limited: true, cooldown_until: 1700000003, cooldown_remaining_seconds: 3, last_rate_limited_at: 1699999997, cooldown_source: 'default' },
    { model: unsafeModel, rate_limited: true, cooldown_until: 1700000008, cooldown_remaining_seconds: 8, last_rate_limited_at: 1699999990, cooldown_source: 'retry_after' },
  ];
  const refreshedModelCooldowns = [
    { model: 'model-a', rate_limited: true, cooldown_until: 1700000010, cooldown_remaining_seconds: 7, last_rate_limited_at: 1699999997, cooldown_source: 'default' },
    { model: unsafeModel, rate_limited: false, cooldown_until: null, cooldown_remaining_seconds: 0, last_rate_limited_at: 1699999990, cooldown_source: 'retry_after' },
  ];
  async function fetch(url, options = {}) {
    const method = options.method || 'GET';
    if (url.endsWith('/accounts')) {
      accountReads += 1;
      if (accountReads === 1) return jsonResponse(snapshotFixture([accountFixture()], false, 1700000000));
      if (accountReads === 2) {
        return jsonResponse(snapshotFixture([accountFixture({ model_cooldowns: initialModelCooldowns })], true, 1700000000));
      }
      return jsonResponse(snapshotFixture([accountFixture({ model_cooldowns: refreshedModelCooldowns })], true, 1700000003));
    }
    if (url.endsWith('/settings') && method === 'GET') return jsonResponse(settingsFixture(false));
    throw new Error(`Unexpected API request: ${method} ${url}`);
  }
  startPage(document, window, fetch, { now: () => nowMs });
  await flush();
  assert.equal(elements['auto-switch-toggle'].checked, false);

  document.visibilityState = 'hidden';
  await document.dispatch('visibilitychange');
  assert.equal(accountReads, 1, 'hiding the page must not trigger an immediate poll');
  assert.ok([...timers.timeouts.values()].some((timer) => timer.delay === 60000), 'hidden page must use a slower refresh interval');

  document.visibilityState = 'visible';
  await document.dispatch('visibilitychange');
  await flush();
  await flush();
  assert.equal(accountReads, 2, 'returning to the page must refresh immediately');
  assert.equal(elements['auto-switch-toggle'].checked, true, 'poll must pick up settings changed in another page');
  let badges = walk(elements['account-list']).filter((node) => node.className.split(/\s+/).includes('model-cooldown'));
  assert.equal(badges.length, 2);
  const modelABadge = badges.find((badge) => badge.textContent.includes('model-a'));
  const unsafeModelBadge = badges.find((badge) => badge.textContent.includes(unsafeModel));
  assert.ok(modelABadge && unsafeModelBadge);
  assert.equal(modelABadge.hidden, false);
  assert.match(modelABadge.textContent, /冷却 00:03.*默认估算/);
  assert.match(unsafeModelBadge.textContent, /冷却 00:08.*上游指定/);
  assert.equal(walk(elements['account-list']).some((node) => node.tagName === 'IMG'), false, 'model names are rendered as text, never markup');

  const requestsBeforeTick = accountReads;
  nowMs += 1000;
  for (const timer of timers.intervals.values()) timer.fn();
  assert.match(modelABadge.textContent, /冷却 00:02/);
  assert.match(unsafeModelBadge.textContent, /冷却 00:07/);
  nowMs += 3000;
  for (const timer of timers.intervals.values()) timer.fn();
  assert.equal(modelABadge.hidden, true, 'one model badge expires independently');
  assert.equal(unsafeModelBadge.hidden, false, 'a later model cooldown remains visible');
  assert.equal(accountReads, requestsBeforeTick, 'countdown ticks must not send network requests');

  await document.dispatch('visibilitychange');
  await flush();
  badges = walk(elements['account-list']).filter((node) => node.className.split(/\s+/).includes('model-cooldown'));
  assert.equal(accountReads, 3, 'a regular snapshot refresh updates cooldowns without extra requests');
  assert.equal(badges.length, 2);
  assert.match(badges[0].textContent, /model-a.*冷却 00:07/);
  assert.equal(badges[1].hidden, true, 'a refreshed non-limited model is hidden');
  const requestsAfterRefresh = accountReads;
  for (const timer of timers.intervals.values()) timer.fn();
  assert.equal(accountReads, requestsAfterRefresh, 'refreshed one-second countdown also remains local');
}

async function verifyOlderSnapshotCannotResetNewSetting() {
  const elements = makePageElements();
  const document = makeDocument(elements);
  const timers = makeTimers();
  const { window } = makeWindow(timers);
  let accountReads = 0;
  let releaseStaleSnapshot;
  async function fetch(url, options = {}) {
    const method = options.method || 'GET';
    if (url.endsWith('/accounts')) {
      accountReads += 1;
      if (accountReads === 1) return jsonResponse(snapshotFixture([accountFixture()], false));
      if (accountReads === 2) return new Promise((resolve) => { releaseStaleSnapshot = () => resolve(jsonResponse(snapshotFixture([accountFixture()], false))); });
      return jsonResponse(snapshotFixture([accountFixture()], false));
    }
    if (url.endsWith('/settings') && method === 'GET') return jsonResponse(settingsFixture(false));
    if (url.endsWith('/settings') && method === 'PATCH') return jsonResponse(settingsFixture(true));
    throw new Error(`Unexpected API request: ${method} ${url}`);
  }
  startPage(document, window, fetch);
  await flush();
  const olderRefresh = elements['refresh-button'].dispatch('click');
  await flush();
  assert.equal(typeof releaseStaleSnapshot, 'function');
  elements['auto-switch-toggle'].checked = true;
  await elements['auto-switch-toggle'].dispatch('change');
  releaseStaleSnapshot();
  await olderRefresh;
  assert.equal(elements['auto-switch-toggle'].checked, true, 'an older in-flight account snapshot must not undo a newer saved setting');
}

async function verifyForegroundRefreshOwnsBusyState() {
  const elements = makePageElements();
  const document = makeDocument(elements);
  const timers = makeTimers();
  const { window } = makeWindow(timers);
  let accountReads = 0;
  let releaseForegroundSnapshot;
  async function fetch(url, options = {}) {
    const method = options.method || 'GET';
    if (url.endsWith('/accounts')) {
      accountReads += 1;
      if (accountReads === 1) return jsonResponse(snapshotFixture());
      if (accountReads === 2) {
        return new Promise((resolve) => {
          releaseForegroundSnapshot = () => resolve(jsonResponse(snapshotFixture()));
        });
      }
      return jsonResponse(snapshotFixture([accountFixture({ nickname: '后台同步账号' })]));
    }
    if (url.endsWith('/settings') && method === 'GET') return jsonResponse(settingsFixture(false));
    throw new Error(`Unexpected API request: ${method} ${url}`);
  }
  startPage(document, window, fetch);
  await flush();

  const foregroundRefresh = elements['refresh-button'].dispatch('click');
  await flush();
  assert.equal(typeof releaseForegroundSnapshot, 'function');
  assert.equal(elements['refresh-button'].disabled, true, 'manual refresh must mark the button busy');

  document.visibilityState = 'visible';
  await document.dispatch('visibilitychange');
  await flush();
  await flush();
  assert.equal(accountReads, 3, 'visibility sync must run while a manual refresh is pending');
  assert.equal(elements['refresh-button'].disabled, true, 'background sync must not clear foreground busy state');

  releaseForegroundSnapshot();
  await foregroundRefresh;
  assert.equal(elements['refresh-button'].disabled, false, 'foreground completion must clear busy state after a newer background read');
}

function findAccountCard(elements, id) {
  return walk(elements['account-list']).find((node) => node.tagName === 'ARTICLE' && node.dataset.accountId === id);
}

function checkinButtons(card) {
  const buttons = walk(card).filter((node) => node.tagName === 'BUTTON');
  return {
    query: buttons.find((node) => node.textContent === '查询签到'),
    sign: buttons.find((node) => node.textContent === '签到'),
  };
}

async function flushMany(count = 8) { for (let i = 0; i < count; i += 1) await flush(); }

function makeLanguagePage(config = {}) {
  const elements = makePageElements();
  const document = makeDocument(elements, makeStaticNodes(elements));
  const timers = makeTimers();
  const windowOptions = {};
  if (Object.hasOwn(config, 'localStorage')) windowOptions.localStorage = config.localStorage;
  if (Object.hasOwn(config, 'navigator')) windowOptions.navigator = config.navigator;
  const { window } = makeWindow(timers, () => Date.now(), windowOptions);
  const fetches = [];
  const accounts = config.accounts || [accountFixture()];
  const serverTime = Math.floor(Date.now() / 1000);
  async function fetch(url, options = {}) {
    fetches.push({ url, method: options.method || 'GET' });
    if (url.endsWith('/accounts')) return jsonResponse(snapshotFixture(accounts, false, serverTime));
    if (url.endsWith('/settings') && (options.method || 'GET') === 'GET') return jsonResponse(settingsFixture(false));
    if (url.endsWith('/checkin-status')) return jsonResponse(config.checkinStatus || { status: 'unchecked', checked: false });
    throw new Error(`Unexpected API request: ${options.method || 'GET'} ${url}`);
  }
  startPage(document, window, fetch);
  return { elements, document, window, fetches, localStorage: window.localStorage };
}

function staticTranslation(document, key) {
  return document.staticNodes.find((node) => node.getAttribute('data-i18n') === key);
}

function assertEnglishShell(page) {
  const { elements, document } = page;
  assert.equal(elements['language-select'].value, 'en');
  assert.equal(document.documentElement.lang, 'en');
  assert.match(document.title, /CodeBuddy.*account/i);
  assert.match(elements['language-select'].getAttribute('aria-label'), /language/i);
  assert.equal(staticTranslation(document, 'hero.title').textContent, 'Account manager');
  assert.equal(staticTranslation(document, 'button.refresh').textContent, 'Refresh list');
  assert.equal(staticTranslation(document, 'button.refresh').getAttribute('aria-label'), 'Refresh account list');
}

function assertChineseShell(page) {
  const { elements, document } = page;
  assert.equal(elements['language-select'].value, 'zh-CN');
  assert.equal(document.documentElement.lang, 'zh-CN');
  assert.equal(document.title, 'CodeBuddy 账号管理');
  assert.equal(elements['language-select'].getAttribute('aria-label'), '页面语言');
  assert.equal(staticTranslation(document, 'hero.title').textContent, '账号管理');
  assert.equal(staticTranslation(document, 'button.refresh').textContent, '刷新列表');
  assert.equal(staticTranslation(document, 'button.refresh').getAttribute('aria-label'), '刷新账号列表');
}

async function verifyLanguagePreferenceAndDynamicTranslations() {
  const key = 'codebuddy.admin.language';
  const storage = makeStorage({ [key]: 'en' });
  const serverTime = Math.floor(Date.now() / 1000);
  const accounts = [
    accountFixture({
      id: 'existing', nickname: '账号甲', uid: 'uid-alpha',
      model_cooldowns: [
        { model: 'model-alpha', rate_limited: true, cooldown_until: serverTime + 120, cooldown_remaining_seconds: 120, last_rate_limited_at: serverTime - 1, cooldown_source: 'retry_after' },
        { model: 'embedding-v2', rate_limited: true, cooldown_until: serverTime + 240, cooldown_remaining_seconds: 240, last_rate_limited_at: serverTime - 1, cooldown_source: 'default' },
      ],
      checkin: { date: null, status: 'unchecked', checked: false, error: null },
    }),
    accountFixture({
      id: 'closed', nickname: '签到活动账号', uid: 'uid-closed',
      checkin: { date: null, status: 'error', checked: null, error: '签到活动未开放' },
    }),
    accountFixture({
      id: 'unknown-error', nickname: '未知错误账号', uid: 'uid-unknown',
      checkin: { date: null, status: 'error', checked: null, error: '__proto__' },
    }),
  ];
  const page = makeLanguagePage({
    localStorage: storage,
    navigator: { language: 'zh-CN', languages: ['zh-CN'] },
    accounts,
  });
  await flushMany(12);
  assertEnglishShell(page);
  assert.equal(storage.getItem(key), 'en', 'saved language takes priority over browser preference');

  const accountText = (id) => walk(findAccountCard(page.elements, id)).map((node) => node.textContent).join(' ');
  const englishChecked = accountText('existing');
  assert.match(englishChecked, /Today's check-in: Not checked in/);
  assert.match(englishChecked, /Model model-alpha .*cooling down .*upstream specified/);
  assert.match(englishChecked, /Model embedding-v2 .*estimated default/);
  assert.match(englishChecked, /账号甲/, 'account names remain user data and are not translated');
  assert.match(englishChecked, /uid-alpha/);
  const englishClosed = accountText('closed');
  assert.match(englishClosed, /Check-in is not available yet/);
  assert.match(accountText('unknown-error'), /__proto__/);
  assert.doesNotMatch(accountText('unknown-error'), /\[object Object\]|undefined/);

  const requestCount = page.fetches.length;
  const originalUrl = page.window.location.href;
  page.elements['language-select'].value = 'zh-CN';
  await page.elements['language-select'].dispatch('change');
  await flushMany();
  assertChineseShell(page);
  assert.equal(storage.getItem(key), 'zh-CN', 'language selection is persisted');
  assert.equal(page.fetches.length, requestCount, 'switching language must not issue API requests');
  assert.equal(page.window.location.href, originalUrl, 'switching language must not reload or navigate');
  const chineseChecked = accountText('existing');
  assert.match(chineseChecked, /今日签到：未签到/);
  assert.match(chineseChecked, /模型 model-alpha · 冷却/);
  assert.match(chineseChecked, /模型 embedding-v2 · 冷却/);
  assert.match(accountText('closed'), /签到活动未开放/);

  const reloaded = makeLanguagePage({
    localStorage: storage,
    navigator: { language: 'en-US', languages: ['en-US'] },
    accounts,
  });
  await flushMany(12);
  assertChineseShell(reloaded);
}

async function verifyCheckinQueryNoticeRelocalizes() {
  const account = accountFixture({
    id: 'notice-account',
    nickname: '签到显示账号',
    uid: 'uid-notice',
    checkin: { date: null, status: 'unchecked', checked: false },
  });
  const page = makeLanguagePage({
    navigator: { language: 'zh-CN', languages: ['zh-CN'] },
    accounts: [account],
    checkinStatus: { status: 'unchecked', checked: false },
  });
  await flushMany(12);
  const card = findAccountCard(page.elements, 'notice-account');
  assert.ok(card, 'account card should render before querying its check-in state');
  const queryButton = checkinButtons(card).query;
  assert.ok(queryButton, 'account card should expose a manual check-in query');
  await queryButton.dispatch('click');
  assert.match(page.elements.notice.textContent, /签到显示账号.*今日签到状态：未签到/);

  page.elements['language-select'].value = 'en';
  await page.elements['language-select'].dispatch('change');
  assert.match(page.elements.notice.textContent, /签到显示账号.*today's check-in status is Not checked in/i);

  page.elements['language-select'].value = 'zh-CN';
  await page.elements['language-select'].dispatch('change');
  assert.match(page.elements.notice.textContent, /签到显示账号.*今日签到状态：未签到/);
}

async function verifyHttpErrorNoticeRelocalizes() {
  const elements = makePageElements();
  const document = makeDocument(elements);
  const timers = makeTimers();
  const { window } = makeWindow(timers);
  window.localStorage.setItem('codebuddy.admin.language', 'en');
  const requests = [];
  async function fetch(url) {
    requests.push(url);
    if (url.endsWith('/accounts')) return jsonResponse(null, false, 503);
    if (url.endsWith('/settings')) return jsonResponse(settingsFixture());
    throw new Error(`Unexpected API request: ${url}`);
  }
  startPage(document, window, fetch);
  await flush();
  assert.match(elements.notice.textContent, /Request failed.*503/);
  elements['language-select'].value = 'zh-CN';
  await elements['language-select'].dispatch('change');
  assert.match(elements.notice.textContent, /请求失败.*503/);
  elements['language-select'].value = 'en';
  await elements['language-select'].dispatch('change');
  assert.match(elements.notice.textContent, /Request failed.*503/);
  assert.equal(requests.length, 2, 'language changes must not retry failed requests');
}

async function verifyLanguageFallbackAndUnavailableStorage() {
  const key = 'codebuddy.admin.language';
  const cases = [
    { saved: '__proto__', navigator: { language: 'zh-TW', languages: ['zh-TW'] }, expected: 'zh-CN' },
    { saved: 'fr', navigator: { language: 'en-US', languages: ['en-US'] }, expected: 'en' },
    { saved: 'invalid-language', navigator: { languages: ['zh-Hans-CN'] }, expected: 'zh-CN' },
    { saved: null, navigator: { language: 'de-DE' }, expected: 'en' },
    { saved: null, navigator: undefined, expected: 'zh-CN' },
  ];
  for (const scenario of cases) {
    const storage = scenario.saved === null ? makeStorage() : makeStorage({ [key]: scenario.saved });
    const page = makeLanguagePage({ localStorage: storage, navigator: scenario.navigator });
    await flushMany(10);
    assert.equal(page.elements['language-select'].value, scenario.expected, `preference ${scenario.saved} should fall back correctly`);
    assert.equal(page.document.documentElement.lang, scenario.expected);
  }

  const failedReadStorage = makeStorage({}, { getThrows: true });
  const readFailurePage = makeLanguagePage({
    localStorage: failedReadStorage,
    navigator: { language: 'en-US', languages: ['en-US'] },
  });
  await flushMany(10);
  assertEnglishShell(readFailurePage);
  readFailurePage.elements['language-select'].value = 'zh-CN';
  await readFailurePage.elements['language-select'].dispatch('change');
  assertChineseShell(readFailurePage);
  assert.equal(failedReadStorage.values.get(key), 'zh-CN', 'a read failure must not disable later writes or switching');

  const failedWriteStorage = makeStorage({}, { setThrows: true });
  const writeFailurePage = makeLanguagePage({
    localStorage: failedWriteStorage,
    navigator: { language: 'zh-CN', languages: ['zh-CN'] },
  });
  await flushMany(10);
  assertChineseShell(writeFailurePage);
  writeFailurePage.elements['language-select'].value = 'en';
  await writeFailurePage.elements['language-select'].dispatch('change');
  assertEnglishShell(writeFailurePage);

  const absentStoragePage = makeLanguagePage({
    localStorage: null,
    navigator: { language: 'zh-CN', languages: ['zh-CN'] },
  });
  await flushMany(10);
  absentStoragePage.elements['language-select'].value = 'en';
  await absentStoragePage.elements['language-select'].dispatch('change');
  assertEnglishShell(absentStoragePage);
}

async function verifyLanguageSwitchDuringPendingOperations() {
  const elements = makePageElements();
  const document = makeDocument(elements, makeStaticNodes(elements));
  const timers = makeTimers();
  const storage = makeStorage();
  const { window, popup } = makeWindow(timers, () => Date.now(), {
    localStorage: storage,
    navigator: { language: 'zh-CN', languages: ['zh-CN'] },
  });
  const fetches = [];
  let releaseLoginRead;
  let releaseSettingsSave;
  async function fetch(url, options = {}) {
    const method = options.method || 'GET';
    fetches.push({ url, method });
    if (url.endsWith('/accounts')) return jsonResponse(snapshotFixture([accountFixture()], false));
    if (url.endsWith('/settings') && method === 'GET') return jsonResponse(settingsFixture(false));
    if (url.endsWith('/settings') && method === 'PATCH') {
      return new Promise((resolve) => { releaseSettingsSave = () => resolve(jsonResponse(settingsFixture(true))); });
    }
    if (url.endsWith('/logins') && method === 'POST') return jsonResponse({ id: 'pending-language', status: 'pending' });
    if (url.endsWith('/logins/pending-language') && method === 'GET') {
      return new Promise((resolve) => {
        releaseLoginRead = () => resolve(jsonResponse({ id: 'pending-language', status: 'failed', error: '模拟登录失败' }));
      });
    }
    throw new Error(`Unexpected API request: ${method} ${url}`);
  }
  startPage(document, window, fetch);
  await flushMany(12);
  assertChineseShell({ elements, document });

  const loginStart = elements['associate-button'].dispatch('click');
  await flushMany(12);
  await loginStart;
  assert.equal(typeof releaseLoginRead, 'function', 'pending login polling must stay active');
  assert.equal(elements['associate-button'].disabled, true);
  assert.equal(popup.closed, false);

  elements['auto-switch-toggle'].checked = true;
  const settingsSave = elements['auto-switch-toggle'].dispatch('change');
  await flushMany(8);
  assert.equal(typeof releaseSettingsSave, 'function', 'settings write must be pending');
  assert.equal(elements['auto-switch-toggle'].disabled, true);
  assert.match(elements['settings-status'].textContent, /正在保存设置/);

  const requestCount = fetches.length;
  elements['language-select'].value = 'en';
  await elements['language-select'].dispatch('change');
  await flushMany();
  assertEnglishShell({ elements, document });
  assert.equal(fetches.length, requestCount, 'a pending operation is not restarted by translation');
  assert.equal(window.location.href, 'http://127.0.0.1:8765/admin');
  assert.equal(elements['associate-button'].disabled, true, 'login remains busy after switching language');
  assert.equal(elements['auto-switch-toggle'].disabled, true, 'setting remains busy after switching language');
  assert.match(elements['progress-title'].textContent, /sign-in/i);
  assert.match(elements['progress-copy'].textContent, /proxy|address/i);
  assert.match(elements['settings-status'].textContent, /Saving setting/i);

  releaseLoginRead();
  await flushMany(12);
  assert.equal(elements['associate-button'].disabled, false);
  assert.equal(popup.closed, true, 'the terminal login result closes its popup normally');
  assert.match(elements['progress-title'].textContent, /Linking failed/);
  assert.match(elements['progress-copy'].textContent, /模拟登录失败/);

  releaseSettingsSave();
  await settingsSave;
  assert.equal(elements['auto-switch-toggle'].checked, true);
  assert.equal(elements['auto-switch-toggle'].disabled, false);
  assert.match(elements['settings-status'].textContent, /Setting loaded/i);
  assert.match(elements.notice.textContent, /Automatic account switching on 429 is enabled/);

  elements['language-select'].value = 'zh-CN';
  await elements['language-select'].dispatch('change');
  assertChineseShell({ elements, document });
  assert.match(elements['progress-title'].textContent, /关联失败/);
  assert.match(elements['notice'].textContent, /已开启遇到 429 时自动切换账号/);
}

async function verifyIndependentSettingsWrites() {
  const elements = makePageElements();
  const document = makeDocument(elements);
  const timers = makeTimers();
  const { window } = makeWindow(timers);
  const remote = { autoSwitch: true, autoCheckin: false };
  const writes = [];
  let releaseStaleSwitch;
  let checkinPatchCount = 0;
  async function fetch(url, options = {}) {
    const method = options.method || 'GET';
    if (url.endsWith('/accounts')) return jsonResponse(snapshotFixture([accountFixture()], remote.autoSwitch, undefined, remote.autoCheckin));
    if (url.endsWith('/settings') && method === 'GET') return jsonResponse(settingsFixture(remote.autoSwitch, remote.autoCheckin));
    if (url.endsWith('/settings') && method === 'PATCH') {
      const body = JSON.parse(options.body);
      writes.push({ body, headers: options.headers });
      if (Object.hasOwn(body, 'auto_switch_on_429')) {
        remote.autoSwitch = body.auto_switch_on_429;
        const delayedResponse = settingsFixture(remote.autoSwitch, remote.autoCheckin);
        return new Promise((resolve) => { releaseStaleSwitch = () => resolve(jsonResponse(delayedResponse)); });
      }
      if (Object.hasOwn(body, 'auto_checkin_enabled')) {
        checkinPatchCount += 1;
        if (checkinPatchCount === 1) remote.autoCheckin = body.auto_checkin_enabled;
        if (checkinPatchCount === 2) return jsonResponse({ detail: '模拟自动签到保存失败' }, false, 503);
        return jsonResponse(settingsFixture(remote.autoSwitch, remote.autoCheckin));
      }
    }
    throw new Error(`Unexpected API request: ${method} ${url}`);
  }
  startPage(document, window, fetch);
  await flushMany();
  assert.equal(elements['auto-switch-toggle'].checked, true);
  assert.equal(elements['auto-checkin-toggle'].checked, false);

  elements['auto-switch-toggle'].checked = false;
  const switchSave = elements['auto-switch-toggle'].dispatch('change');
  await flush();
  assert.equal(elements['auto-switch-toggle'].disabled, true);
  assert.equal(elements['auto-checkin-toggle'].disabled, false, 'one setting must not lock the other control');

  elements['auto-checkin-toggle'].checked = true;
  await elements['auto-checkin-toggle'].dispatch('change');
  assert.equal(remote.autoCheckin, true);
  assert.equal(typeof releaseStaleSwitch, 'function');
  releaseStaleSwitch();
  await switchSave;
  assert.equal(elements['auto-switch-toggle'].checked, false);
  assert.equal(elements['auto-checkin-toggle'].checked, true, 'a stale switch response must not overwrite the independent check-in setting');

  elements['auto-checkin-toggle'].checked = false;
  await elements['auto-checkin-toggle'].dispatch('change');
  assert.equal(elements['auto-checkin-toggle'].checked, true, 'failed setting save must restore the value confirmed by GET');
  assert.equal(elements['auto-switch-toggle'].checked, false, 'check-in save failure must preserve the 429 setting');
  assert.match(elements.notice.textContent, /模拟自动签到保存失败/);
  assert.ok(writes.every((write) => write.headers.get('X-Account-Manager') === '1'));
  assert.deepEqual(writes.map((write) => Object.keys(write.body)), [['auto_switch_on_429'], ['auto_checkin_enabled'], ['auto_checkin_enabled']]);
}

async function verifyCheckinDiscoveryAndActions() {
  const elements = makePageElements();
  const document = makeDocument(elements);
  const timers = makeTimers();
  const { window } = makeWindow(timers);
  const serverTime = Math.floor(Date.now() / 1000);
  const today = new Date(serverTime * 1000 + 8 * 60 * 60 * 1000).toISOString().slice(0, 10);
  const accounts = [
    accountFixture({ id: 'existing', nickname: '当前账号', checkin: { date: null, status: 'unknown', checked: null } }),
    accountFixture({ id: 'second', nickname: '第二账号', checkin: { date: null, status: 'unknown', checked: null } }),
    accountFixture({ id: 'third', nickname: '第三账号', checkin: { date: null, status: 'unknown', checked: null } }),
  ];
  const serverCheckins = {
    existing: { date: today, status: 'unchecked', checked: false },
    second: { date: today, status: 'checked', checked: true, last_checkin_at: serverTime, next_checkin_at: serverTime + 86400 },
    third: { date: today, status: 'error', checked: null, error: '<img src=x onerror=alert(1)>' },
  };
  const statusReads = [];
  const writes = [];
  let concurrentReads = 0;
  let maxConcurrentReads = 0;
  let activations = 0;
  let snapshotHasFreshCheckins = false;
  async function fetch(url, options = {}) {
    const method = options.method || 'GET';
    if (method !== 'GET') writes.push({ url, method, headers: options.headers });
    if (url.endsWith('/accounts') && method === 'GET') {
      const rows = snapshotHasFreshCheckins ? accounts.map((account) => ({ ...account, checkin: serverCheckins[account.id] })) : accounts;
      snapshotHasFreshCheckins = true;
      return jsonResponse(snapshotFixture(rows, false, serverTime));
    }
    if (url.endsWith('/settings') && method === 'GET') return jsonResponse(settingsFixture(false));
    const match = url.match(/\/accounts\/([^/]+)\/checkin-status$/);
    if (match && method === 'GET') {
      const id = decodeURIComponent(match[1]);
      statusReads.push(id);
      concurrentReads += 1;
      maxConcurrentReads = Math.max(maxConcurrentReads, concurrentReads);
      await new Promise((resolve) => setImmediate(resolve));
      concurrentReads -= 1;
      return jsonResponse(serverCheckins[id]);
    }
    const signMatch = url.match(/\/accounts\/([^/]+)\/checkin$/);
    if (signMatch && method === 'POST') {
      const id = decodeURIComponent(signMatch[1]);
      serverCheckins[id] = { date: today, status: 'checked', checked: true, last_checkin_at: serverTime };
      return jsonResponse(serverCheckins[id]);
    }
    if (url.endsWith('/activate')) activations += 1;
    throw new Error(`Unexpected API request: ${method} ${url}`);
  }
  startPage(document, window, fetch);
  await flushMany(12);
  assert.deepEqual([...statusReads].sort(), ['existing', 'second', 'third']);
  assert.ok(maxConcurrentReads <= 2, 'initial account checks must use at most two concurrent requests');
  assert.equal(writes.filter((write) => write.method === 'POST' && write.url.endsWith('/checkin')).length, 0,
    'initial discovery and snapshot synchronization must never auto-sign');

  const activeCard = findAccountCard(elements, 'existing');
  const secondCard = findAccountCard(elements, 'second');
  const thirdCard = findAccountCard(elements, 'third');
  assert.ok(activeCard && secondCard && thirdCard);
  assert.ok(checkinButtons(activeCard).query && checkinButtons(activeCard).sign, 'the active account also has check-in controls');
  assert.equal(checkinButtons(activeCard).sign.disabled, false, 'a confirmed unchecked account can be signed');
  assert.equal(checkinButtons(secondCard).sign.disabled, true, 'a checked account cannot be signed again');
  assert.match(walk(secondCard).map((node) => node.textContent).join(' '), /下次计划/);
  assert.ok(walk(thirdCard).some((node) => typeof node.textContent === 'string' && node.textContent.includes('<img src=x onerror=alert(1)>')), 'upstream error text must be rendered as text');
  assert.equal(checkinButtons(thirdCard).sign.disabled, false, 'an error state remains actionable so the server can check first');

  await checkinButtons(activeCard).sign.dispatch('click');
  assert.equal(serverCheckins.existing.status, 'checked');
  assert.equal(checkinButtons(findAccountCard(elements, 'existing')).sign.disabled, true);
  await checkinButtons(findAccountCard(elements, 'existing')).query.dispatch('click');
  assert.equal(statusReads.filter((id) => id === 'existing').length, 2, 'manual query calls the live status endpoint');
  document.visibilityState = 'visible';
  await document.dispatch('visibilitychange');
  await flushMany();
  assert.match(walk(findAccountCard(elements, 'existing')).map((node) => node.textContent).join(' '), /今日签到：已签到/,
    'a later account snapshot must reflect server-side status changes');
  assert.equal(activations, 0, 'check-in actions never switch the current account');
  assert.ok(writes.some((write) => write.method === 'POST' && write.url.endsWith('/checkin')));
  assert.ok(writes.every((write) => write.headers.get('X-Account-Manager') === '1'));

  const readsBeforePoll = statusReads.length;
  await document.dispatch('visibilitychange');
  await flushMany();
  assert.equal(statusReads.length, readsBeforePoll, '15-second account snapshots must not re-query known check-in states');
}

async function verifyCheckinRechecksAfterBeijingMidnight() {
  const elements = makePageElements();
  const document = makeDocument(elements);
  const timers = makeTimers();
  const { window } = makeWindow(timers);
  let nowMs = Date.UTC(2026, 9, 23, 15, 59, 50);
  let accountReads = 0;
  let statusReads = 0;
  let serverCheckin = { date: '2026-10-23', status: 'unknown', checked: null };
  async function fetch(url, options = {}) {
    const method = options.method || 'GET';
    if (url.endsWith('/accounts') && method === 'GET') {
      accountReads += 1;
      return jsonResponse(snapshotFixture([accountFixture({ checkin: serverCheckin })], false, Math.floor(nowMs / 1000)));
    }
    if (url.endsWith('/settings') && method === 'GET') return jsonResponse(settingsFixture(false));
    if (url.endsWith('/accounts/existing/checkin-status') && method === 'GET') {
      statusReads += 1;
      const date = statusReads === 1 ? '2026-10-23' : '2026-10-24';
      serverCheckin = { date, status: 'unchecked', checked: false };
      return jsonResponse(serverCheckin);
    }
    throw new Error(`Unexpected API request: ${method} ${url}`);
  }
  startPage(document, window, fetch, { now: () => nowMs });
  await flushMany();
  assert.equal(statusReads, 1);
  nowMs += 20000;
  document.visibilityState = 'visible';
  await document.dispatch('visibilitychange');
  await flushMany();
  assert.ok(accountReads >= 2);
  assert.equal(statusReads, 2, 'Beijing date change must trigger one fresh underlying status check');
  await document.dispatch('visibilitychange');
  await flushMany();
  assert.equal(statusReads, 2, 'same-day account polling must not repeat underlying status checks');
  assert.match(walk(elements['account-list']).map((node) => node.textContent).join(' '), /未签到/);
}

async function verifyAutoCheckinIsBackendOwned() {
  const elements = makePageElements();
  const document = makeDocument(elements);
  const timers = makeTimers();
  const { window } = makeWindow(timers);
  let nowMs = Date.UTC(2026, 9, 23, 15, 59, 50);
  let setting = false;
  const statusReads = [];
  const signWrites = [];
  const serverCheckins = {
    existing: { date: null, status: 'unknown', checked: null },
    second: { date: null, status: 'unknown', checked: null },
  };
  const accounts = [
    accountFixture({ id: 'existing', checkin: serverCheckins.existing }),
    accountFixture({ id: 'second', nickname: '第二账号', checkin: serverCheckins.second }),
  ];
  const currentBeijingDate = () => new Date(nowMs + 8 * 60 * 60 * 1000).toISOString().slice(0, 10);
  async function fetch(url, options = {}) {
    const method = options.method || 'GET';
    if (url.endsWith('/accounts') && method === 'GET') {
      return jsonResponse(snapshotFixture(accounts.map((account) => ({ ...account, checkin: serverCheckins[account.id] })), false, Math.floor(nowMs / 1000), setting));
    }
    if (url.endsWith('/settings') && method === 'GET') return jsonResponse(settingsFixture(false, setting));
    if (url.endsWith('/settings') && method === 'PATCH') {
      const body = JSON.parse(options.body);
      assert.deepEqual(Object.keys(body), ['auto_checkin_enabled']);
      setting = body.auto_checkin_enabled;
      return jsonResponse(settingsFixture(false, setting));
    }
    const statusMatch = url.match(/\/accounts\/([^/]+)\/checkin-status$/);
    if (statusMatch && method === 'GET') {
      const id = decodeURIComponent(statusMatch[1]);
      statusReads.push(id);
      const result = id === 'existing'
        ? { date: currentBeijingDate(), status: 'unchecked', checked: false }
        : { date: currentBeijingDate(), status: 'checked', checked: true };
      serverCheckins[id] = result;
      return jsonResponse(result);
    }
    const signMatch = url.match(/\/accounts\/([^/]+)\/checkin$/);
    if (signMatch && method === 'POST') {
      const id = decodeURIComponent(signMatch[1]);
      signWrites.push(id);
      serverCheckins[id] = { date: currentBeijingDate(), status: 'checked', checked: true };
      return jsonResponse(serverCheckins[id]);
    }
    throw new Error(`Unexpected API request: ${method} ${url}`);
  }
  startPage(document, window, fetch, { now: () => nowMs });
  await flushMany();
  assert.equal(elements['auto-checkin-toggle'].checked, false, 'global auto check-in defaults to off');
  assert.deepEqual([...statusReads].sort(), ['existing', 'second'], 'initial unknown states are queried once per account');
  assert.deepEqual(signWrites, [], 'initial status discovery only reads; it does not sign');

  elements['auto-checkin-toggle'].checked = true;
  await elements['auto-checkin-toggle'].dispatch('change');
  assert.equal(setting, true);
  assert.match(elements.notice.textContent, /代理正在检查全部关联账号/);
  assert.deepEqual(signWrites, [], 'enabling the setting only saves it; the proxy owns automatic check-ins');
  assert.equal(elements['auto-checkin-toggle'].checked, true);

  // Simulate an automatic check-in performed by the backend, then let the page synchronize its snapshot.
  serverCheckins.existing = { date: currentBeijingDate(), status: 'checked', checked: true };
  document.visibilityState = 'visible';
  await document.dispatch('visibilitychange');
  await flushMany();
  assert.match(walk(findAccountCard(elements, 'existing')).map((node) => node.textContent).join(' '), /今日签到：已签到/);
  assert.deepEqual(signWrites, [], 'background snapshot synchronization never sends a check-in write');

  // A Beijing date change performs one status GET per newly unknown account, but no automatic POST.
  nowMs += 20000;
  serverCheckins.existing = { date: currentBeijingDate() === '2026-10-24' ? '2026-10-23' : currentBeijingDate(), status: 'checked', checked: true };
  document.visibilityState = 'visible';
  await document.dispatch('visibilitychange');
  await flushMany();
  assert.equal(statusReads.filter((id) => id === 'existing').length, 2, 'a new Beijing day performs one status check');
  assert.deepEqual(signWrites, [], 'cross-day rediscovery only reads; automatic writes remain backend-owned');

  // Manual per-account action still uses the write endpoint.
  serverCheckins.existing = { date: currentBeijingDate(), status: 'unchecked', checked: false };
  await checkinButtons(findAccountCard(elements, 'existing')).sign.dispatch('click');
  assert.deepEqual(signWrites, ['existing']);
}

(async () => {
  for (const status of ['succeeded', 'cancelled', 'failed']) await verifyTerminalState(status);
  await verifyCancelFailureRecovery();
  await verifyAutoSwitchSettings();
  await verifyLanguagePreferenceAndDynamicTranslations();
  await verifyHttpErrorNoticeRelocalizes();
  await verifyCheckinQueryNoticeRelocalizes();
  await verifyLanguageFallbackAndUnavailableStorage();
  await verifyLanguageSwitchDuringPendingOperations();
  await verifyCooldownPollingAndLocalExpiry();
  await verifyOlderSnapshotCannotResetNewSetting();
  await verifyForegroundRefreshOwnsBusyState();
  await verifyIndependentSettingsWrites();
  await verifyCheckinDiscoveryAndActions();
  await verifyCheckinRechecksAfterBeijingMidnight();
  await verifyAutoCheckinIsBackendOwned();
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""


def test_admin_inline_script_syntax_and_terminal_controls(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed to execute the admin page's browser-free mock")

    html = ADMIN_HTML.read_text(encoding="utf-8")
    assert re.search(r"(?m)^\s*\[hidden\]\s*\{\s*display:\s*none\s*!important;\s*\}", html)
    script_path = tmp_path / "admin-page.js"
    script_path.write_text(_inline_script(), encoding="utf-8")
    translation_parser = _TranslationManifestParser()
    translation_parser.feed(html)
    translations_path = tmp_path / "admin-translations.json"
    translations_path.write_text(json.dumps(translation_parser.nodes), encoding="utf-8")

    subprocess.run([node, "--check", str(script_path)], check=True, capture_output=True, text=True)
    harness_path = tmp_path / "admin-page-mock.cjs"
    harness_path.write_text(_DOM_HARNESS, encoding="utf-8")
    subprocess.run(
        [node, str(harness_path), str(script_path), str(translations_path)],
        check=True,
        capture_output=True,
        text=True,
    )
