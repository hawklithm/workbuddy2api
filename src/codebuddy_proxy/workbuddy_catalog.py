"""WorkBuddy model catalogs, scoped by backend, endpoint and account."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
import time

import httpx

log = logging.getLogger(__name__)
TTL_SECONDS = 24 * 60 * 60
FAILURE_RETRY_SECONDS = 60


def parse_catalog(document):
    if not isinstance(document, dict) or document.get("code", 0) != 0:
        raise ValueError("upstream catalog rejected")
    data = document.get("data") if isinstance(document.get("data"), dict) else document
    agents, models = data.get("agents"), data.get("models")
    if not isinstance(agents, list) or not isinstance(models, list):
        raise ValueError("catalog missing agents or models")
    cli = next((a for a in agents if isinstance(a, dict) and a.get("name") == "cli"), None)
    ids = cli.get("models") if cli else None
    if not isinstance(ids, list) or not ids:
        raise ValueError("catalog missing cli whitelist")
    by_id = {}
    for item in models:
        if not isinstance(item, dict) or item.get("disabled") is True:
            continue
        mid, inp, out = item.get("id"), item.get("maxInputTokens"), item.get("maxOutputTokens")
        if not isinstance(mid, str) or not mid.strip():
            continue
        if type(inp) is not int or inp <= 0 or type(out) is not int or out <= 0:
            continue
        model = dict(item)
        model["supportsImages"] = item.get("supportsImages") is True and item.get("disabledMultimodal") is not True
        by_id[mid] = model
    result, seen = [], set()
    for mid in ids:
        if isinstance(mid, str) and mid in by_id and mid not in seen:
            result.append(by_id[mid])
            seen.add(mid)
    if not result:
        raise ValueError("empty cli catalog")
    return result


def snapshot_client(client):
    session = client._snapshot_session()[0] if hasattr(client, "_snapshot_session") else client.session
    session = session if isinstance(session, dict) else {}
    auth = session.get("auth") if isinstance(session.get("auth"), dict) else {}
    account = session.get("account") if isinstance(session.get("account"), dict) else {}
    token = auth.get("accessToken")
    if not isinstance(token, str) or not token:
        raise ValueError("no access token")
    region, endpoint = client.profile.key, client.endpoint.rstrip("/")
    if region not in ("domestic", "global"):
        raise ValueError("unsupported backend")
    uid = str(account.get("uid") or "")
    # Token fingerprint is only used when account identity is absent; no token is persisted.
    identity = uid or hashlib.sha256(token.encode()).hexdigest()
    enterprise = str(account.get("enterpriseId") or "")
    key = hashlib.sha256(json.dumps([region, endpoint, identity, enterprise], ensure_ascii=False).encode()).hexdigest()
    return region, endpoint, token, key


async def fetch_catalog(region, endpoint, token):
    if region == "global":
        path, origin, ua = "/v3/config", "https://www.workbuddy.ai", "WorkBuddyAI/5.5.2"
    else:
        path, origin, ua = "/console/enterprises/personal/models", "https://www.codebuddy.cn", "CLI/2.63.2 CodeBuddy/2.63.2"
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json", "Origin": origin,
               "Referer": origin + "/", "User-Agent": ua}
    if region == "global":
        headers.update({"X-Requested-With": "XMLHttpRequest", "X-Product": "SaaS"})
    async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
        response = await client.get(endpoint + path, headers=headers)
        response.raise_for_status()
        return parse_catalog(response.json())


class WorkBuddyCatalog:
    def __init__(self, cache_dir: Path, ttl=TTL_SECONDS, static=False):
        self.cache_dir = Path(cache_dir)
        self.ttl = max(0, ttl)
        self.static = static
        self.memory = {}
        self.retry_after = {}
        self.locks = {}

    def _read(self, key):
        try:
            data = json.loads((self.cache_dir / (key + ".json")).read_text(encoding="utf-8"))
            stamp, models = data["fetched_at"], data["models"]
            if type(stamp) not in (int, float) or not 0 < stamp <= time.time() + 300:
                return None
            # Revalidate persisted data using the same parser, not untrusted IDs or fields.
            ids = [item.get("id") for item in models if isinstance(item, dict)] if isinstance(models, list) else []
            valid = parse_catalog({"models": models, "agents": [{"name": "cli", "models": ids}]})
            return stamp, valid
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def _write(self, key, stamp, models):
        from buddy_agent_auth.providers.codebuddy.account_store import _atomic_json
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        _atomic_json(self.cache_dir / (key + ".json"), {"version": 1, "fetched_at": stamp, "models": models})

    async def get(self, client, fallback, *, refresh=False):
        if self.static:
            return fallback, {"source": "builtin", "note": "static models configured"}
        try:
            region, endpoint, token, key = snapshot_client(client)
        except (ValueError, AttributeError, TypeError):
            return fallback, {"source": "builtin", "note": "account credentials unavailable"}
        lock = self.locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self.memory.get(key)
            if cached is None:
                cached = await asyncio.to_thread(self._read, key)
                if cached:
                    self.memory[key] = cached
            now = time.time()
            if cached and not refresh and now - cached[0] < self.ttl:
                return cached[1], {"source": "cached", "fetched_at": cached[0], "stale": False}
            if not refresh and now < self.retry_after.get(key, 0):
                if cached:
                    return cached[1], {"source": "cached", "fetched_at": cached[0], "stale": True, "note": "upstream unavailable"}
                return fallback, {"source": "builtin", "stale": True, "note": "upstream unavailable"}
            try:
                models = await fetch_catalog(region, endpoint, token)
            except (httpx.HTTPError, ValueError, TypeError, OSError) as exc:
                log.warning("WorkBuddy model catalog fetch failed: %s", type(exc).__name__)
                self.retry_after[key] = now + FAILURE_RETRY_SECONDS
                if cached:
                    return cached[1], {"source": "cached", "fetched_at": cached[0], "stale": True, "note": "upstream unavailable"}
                return fallback, {"source": "builtin", "stale": True, "note": "upstream unavailable"}
            stamp = time.time()
            self.memory[key] = (stamp, models)
            self.retry_after.pop(key, None)
            try:
                await asyncio.to_thread(self._write, key, stamp, models)
            except OSError:
                log.warning("WorkBuddy model catalog cache could not be saved")
            return models, {"source": "live", "fetched_at": stamp, "stale": False}
