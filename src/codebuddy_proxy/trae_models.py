"""Trae CN model discovery, scoped to the selected linked account."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import sqlite3
import time
import uuid

import httpx
from fastapi import HTTPException, Request

from codebuddy_proxy.trae_gateway import APP_ID, load_account, router

CATALOG_URL = "https://api5-normal.mchost.guru/api/ide/v1/batch_get_detail_param"
TTL_SECONDS = 6 * 3600
INTERNAL = {"summary", "fast_apply", "title_generation", "input_optimization"}
logger = logging.getLogger(__name__)
FALLBACK = ("Doubao-Seed-Evolving", "Doubao-Seed-2.1-Pro", "Doubao-Seed-2.1-Turbo", "step-5-preview",
            "glm-5.3", "glm-5.2", "deepseek-v4.1-flash", "DeepSeek-V4-Flash-Official",
            "DeepSeek-V4-Pro-Official", "kimi-k3", "kimi-k2.7-code", "kimi-k2.6", "minimax-m3",
            "qwen3.8-max", "qwen-3.7-plus", "deepseek-v4-flash", "deepseek-v4-pro", "glm-5-turbo",
            "glm-5", "sagitta", "aquila")


def catalog_headers(jwt, device, machine):
    return {"accept": "*/*", "request-traffic-type": "prod", "user-agent": "TraeClient/TTNet",
            "x-agent-host-type": "0", "x-app-id": APP_ID, "x-app-version": "default",
            "x-app-version-code": "20260924", "x-bridge-transport": "aha",
            "x-custom-trace-id": uuid.uuid4().hex, "x-device-brand": "MacBookPro18,3",
            "x-device-cpu": "Apple", "x-device-id": device, "x-device-type": "mac",
            "x-ide-token": jwt.removeprefix("Cloud-IDE-JWT "), "x-ide-version": "3.4.4",
            "x-ide-version-code": "20260924", "x-ide-version-type": "stable",
            "x-lgw-req-sdk-type": "3", "x-machine-id": machine, "x-os-version": "macOS 27.0.1",
            "package-type": "stable_cn", "x-request-id": "req_" + uuid.uuid4().hex,
            "x-lscbd-aid": "787976", "x-lscbd-platform": "mac", "x-ss-dp": "787976"}


def number(value):
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result > 0 else None


def parse_catalog(payload):
    if not isinstance(payload, dict) or payload.get("error_code") not in (None, 0):
        raise ValueError("catalog upstream error")
    groups = payload.get("function_configs")
    if not isinstance(groups, list):
        raise ValueError("invalid catalog")
    group = next((g for g in groups if isinstance(g, dict) and g.get("function") == "chat_v3"), None)
    if group is None and len(groups) == 1 and isinstance(groups[0], dict) and not groups[0].get("function"):
        group = groups[0]
    if not isinstance(group, dict) or not isinstance(group.get("config_info_list"), list):
        raise ValueError("chat_v3 catalog missing")
    result, seen = [], set()
    for item in group["config_info_list"]:
        if not isinstance(item, dict) or item.get("is_invisible_to_user") is True:
            continue
        name = item.get("config_name")
        if not isinstance(name, str):
            continue
        name = name.strip()
        if not name or name.lower().startswith("custom_model") or name.lower() in INTERNAL or name in seen:
            continue
        seen.add(name)
        display = item.get("display_config") if isinstance(item.get("display_config"), dict) else {}
        model = {"id": name, "object": "model", "created": 1753600000, "owned_by": "trae",
                 "display_name": display.get("display_name") or name}
        context = item.get("context_window_tokens")
        dev = number(context.get("dev")) if isinstance(context, dict) else number(context)
        maximum = number(context.get("max")) if isinstance(context, dict) else None
        if dev:
            model["context_length"] = min(dev, 900000)
            model["context_length_default"] = min(dev, 900000)
        if maximum:
            model["context_length_max"] = min(maximum, 900000)
        effort = item.get("reasoning_effort_config")
        if isinstance(effort, dict):
            options = effort.get("options")
            if isinstance(options, list):
                options = [v.strip() for v in options if isinstance(v, str) and v.strip()]
                if options:
                    model["reasoning_efforts"] = options
            default = effort.get("default_level")
            if isinstance(default, str) and default.strip():
                model["default_reasoning_effort"] = default.strip()
        if item.get("is_default") is True:
            model["is_default"] = True
        result.append(model)
    if not result:
        raise ValueError("empty catalog")
    return result


def selected_record(accounts, account_id):
    with accounts.lock:
        records = accounts._records()
        if account_id:
            records = [record for record in records if record["id"] == account_id]
        if len(records) != 1:
            raise ValueError("select one linked account")
        record = records[0]
        return record, accounts._path(record["id"]) / "models-cache.json"


def read_cache(path, uid):
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        fetched = raw.get("fetched_at")
        if raw.get("uid") != uid or not isinstance(fetched, (int, float)) or fetched <= 0 or fetched > time.time() + 300:
            return None
        return fetched, parse_catalog(raw.get("response"))
    except (OSError, TypeError, ValueError, AttributeError):
        return None


def write_cache(accounts, record, path, payload):
    from buddy_agent_auth.providers.codebuddy.account_store import _atomic_json
    with accounts.lock:
        # A deleted or re-linked account must never be recreated or inherit a cache.
        if not any(r["id"] == record["id"] and r["uid"] == record["uid"] for r in accounts._records()):
            return
        _atomic_json(path, {"uid": record["uid"], "fetched_at": time.time(), "response": payload})


def client_cache(uid, path=None):
    """Read only the selected UID's local Trae CN client cache."""
    appdata = os.environ.get("APPDATA")
    if path is None:
        if not appdata:
            return []
        path = Path(appdata) / "Trae CN" / "User" / "globalStorage" / "state.vscdb"
    if not path.is_file():
        return []
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2) as db:
            keys = (uid + "_AI.agent.model.model_list_map", uid + ":AI.agent.model.model_list_map")
            row = db.execute("SELECT value FROM ItemTable WHERE key IN (?, ?) LIMIT 1", keys).fetchone()
        groups = json.loads(row[0]) if row else {}
        entries = groups.get("chat_v3") if isinstance(groups, dict) else None
        if not isinstance(entries, list):
            return []
        result, seen = [], set()
        for item in entries:
            if not isinstance(item, dict) or item.get("provider") or item.get("custom_model_id"):
                continue
            name = item.get("name")
            if not isinstance(name, str) or not name.strip() or name in seen:
                continue
            name = name.strip()
            seen.add(name)
            model = {"id": name, "object": "model", "created": 1753600000, "owned_by": "trae",
                     "display_name": item.get("display_name") or name}
            ctx = item.get("context_window_size")
            dev = number(ctx.get("default")) if isinstance(ctx, dict) else None
            if dev:
                model["context_length"] = min(dev, 900000)
            result.append(model)
        return result
    except (sqlite3.Error, OSError, TypeError, ValueError):
        return []


async def fetch_catalog(token, device, machine):
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(CATALOG_URL, headers=catalog_headers(token, device, machine),
                                     json={"functions": ["chat_v3"]})
        response.raise_for_status()
        payload = response.json()
    return payload, parse_catalog(payload)


@router.get("/trae/v1/models")
async def trae_models(request: Request, refresh: bool = False):
    manager = request.app.state.trae_get_account_manager()
    accounts = getattr(manager, "trae_accounts", None)
    if accounts is None:
        raise HTTPException(503, "Trae account management unavailable")
    try:
        record, cache_path = await asyncio.to_thread(selected_record, accounts, request.headers.get("x-trae-account-id"))
    except ValueError:
        raise HTTPException(400, "Select one linked Trae account with X-Trae-Account-Id") from None
    # Per-app, per-account single flight; subsequent waiters recheck the cache.
    locks = getattr(request.app.state, "trae_model_locks", None)
    if locks is None:
        locks = request.app.state.trae_model_locks = {}
    lock = locks.setdefault(str(cache_path), asyncio.Lock())
    async with lock:
        cached = await asyncio.to_thread(read_cache, cache_path, record["uid"])
        if cached and not refresh and time.time() - cached[0] < TTL_SECONDS:
            return {"object": "list", "data": cached[1], "source": "upstream_cache", "fetched_at": cached[0], "stale": False}
        try:
            uid, token, device, machine = await asyncio.to_thread(load_account, accounts, record["id"])
            if uid != record["uid"]:
                raise ValueError("account changed")
            payload, models = await fetch_catalog(token, device, machine)
            try:
                await asyncio.to_thread(write_cache, accounts, record, cache_path, payload)
            except OSError:
                pass
            return {"object": "list", "data": models, "source": "upstream", "fetched_at": time.time(), "stale": False}
        except Exception as exc:
            # Provider errors can contain tokens or response bodies. Log only type.
            logger.warning("Trae model catalog fetch failed: %s", type(exc).__name__)
            local = await asyncio.to_thread(client_cache, record["uid"])
            if local:
                return {"object": "list", "data": local, "source": "client_cache", "stale": True}
            if cached:
                return {"object": "list", "data": cached[1], "source": "upstream_cache", "fetched_at": cached[0], "stale": True}
            data = [{"id": name, "object": "model", "created": 1753600000, "owned_by": "trae"} for name in FALLBACK]
            return {"object": "list", "data": data, "source": "static_fallback", "stale": True}
