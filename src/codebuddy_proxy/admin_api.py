"""Loopback-only admin page and account-management API."""

from __future__ import annotations

import ipaddress
import json
import math
import re
import time
import unicodedata
from importlib import resources
from typing import Any, Callable
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.routing import APIRoute
from fastapi.exceptions import RequestValidationError

from daily_checkin.workbuddy_api import safe_checkin as _safe_checkin

from codebuddy_proxy.account_manager import (
    AccountManager,
    AccountManagerError,
    is_loopback_address,
)


_WRITE_HEADER = "X-Account-Manager"
_MAX_LOGIN_BODY = 4096
_MAX_SETTINGS_BODY = 4096
_PUBLIC_ACCOUNT_FIELDS = (
    "id",
    "backend",
    "endpoint",
    "uid",
    "nickname",
    "active",
    "model_cooldowns",
    "checkin",
)
_PUBLIC_JOB_FIELDS = ("id", "backend", "status", "auth_url", "error", "account_id")



class _NoStoreRoute(APIRoute):
    """Keep admin route failures generic and mark every routed response private."""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def no_store_handler(request: Request) -> Response:
            try:
                response = await original(request)
            except HTTPException as exc:
                detail = exc.detail if isinstance(exc.detail, str) else "请求被拒绝"
                response = JSONResponse({"detail": detail}, status_code=exc.status_code)
            except RequestValidationError:
                response = JSONResponse({"detail": "请求格式无效"}, status_code=400)
            except Exception:
                # Never serialize an exception, provider response, or traceback
                # into a local administrative response.
                response = JSONResponse({"detail": "管理请求失败"}, status_code=500)
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
            return response

        return no_store_handler


def _one_header(request: Request, name: str) -> str | None:
    values = request.headers.getlist(name)
    if len(values) != 1:
        return None
    return values[0]


def _host_identity(raw_host: str, scheme: str) -> tuple[str, int] | None:
    if not raw_host or len(raw_host) > 320 or any(ch.isspace() for ch in raw_host):
        return None
    try:
        parts = urlsplit("//" + raw_host)
        if parts.username is not None or parts.password is not None:
            return None
        if parts.path or parts.query or parts.fragment or not parts.hostname:
            return None
        hostname = parts.hostname.lower()
        if hostname.endswith("."):
            hostname = hostname[:-1]
        if hostname.endswith("."):
            return None
        try:
            address = ipaddress.ip_address(hostname)
            if not address.is_loopback:
                return None
            canonical_host = address.compressed.lower()
        except ValueError:
            if hostname != "localhost":
                return None
            canonical_host = hostname
        port = parts.port
        if port is None:
            port = 443 if scheme.lower() == "https" else 80
        if not 1 <= port <= 65535:
            return None
        return canonical_host, port
    except ValueError:
        return None


def _origin_identity(raw_origin: str) -> tuple[str, str, int] | None:
    if not raw_origin or len(raw_origin) > 512:
        return None
    try:
        parts = urlsplit(raw_origin)
        scheme = parts.scheme.lower()
        if scheme not in {"http", "https"}:
            return None
        if not parts.netloc or parts.path or parts.query or parts.fragment:
            return None
        if parts.username is not None or parts.password is not None or not parts.hostname:
            return None
        host = parts.hostname.lower()
        if host.endswith("."):
            host = host[:-1]
        if host.endswith("."):
            return None
        try:
            address = ipaddress.ip_address(host)
            if not address.is_loopback:
                return None
            host = address.compressed.lower()
        except ValueError:
            if host != "localhost":
                return None
        port = parts.port
        if port is None:
            port = 443 if scheme == "https" else 80
        if not 1 <= port <= 65535:
            return None
        return scheme, host, port
    except ValueError:
        return None


def _check_admin_request(request: Request) -> None:
    peer = request.client.host if request.client is not None else None
    if not is_loopback_address(peer):
        raise HTTPException(status_code=403, detail="仅允许本机访问管理页面")

    raw_host = _one_header(request, "host")
    host = _host_identity(raw_host or "", request.url.scheme)
    if host is None:
        raise HTTPException(status_code=403, detail="管理页面 Host 无效")

    raw_origin = _one_header(request, "origin")
    if raw_origin is None:
        # Missing or duplicated Origin is allowed for reads.  Mutations are
        # separately protected by the dedicated header below.
        if request.headers.getlist("origin"):
            raise HTTPException(status_code=403, detail="管理页面 Origin 无效")
        return

    origin = _origin_identity(raw_origin)
    expected = (request.url.scheme.lower(), host[0], host[1])
    if origin != expected:
        raise HTTPException(status_code=403, detail="跨站管理请求已拒绝")


def _require_write_header(request: Request) -> None:
    if _one_header(request, _WRITE_HEADER) != "1":
        raise HTTPException(status_code=403, detail="缺少管理请求标记")


def _safe_job(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {key: None for key in _PUBLIC_JOB_FIELDS}
    return {key: value.get(key) for key in _PUBLIC_JOB_FIELDS}


def _safe_snapshot(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {
            "accounts": [],
            "active_account_id": None,
            "settings": _safe_settings(None),
            "server_time": time.time(),
        }
    accounts = value.get("accounts")
    safe_accounts = []
    if isinstance(accounts, list):
        for account in accounts:
            if isinstance(account, dict):
                safe = {
                    key: account[key]
                    for key in _PUBLIC_ACCOUNT_FIELDS[:6]
                    if key in account
                }
                safe["model_cooldowns"] = _safe_model_cooldowns(
                    account.get("model_cooldowns")
                )
                safe["checkin"] = _safe_checkin(account.get("checkin"))
                safe_accounts.append(safe)
    server_time = value.get("server_time")
    return {
        "accounts": safe_accounts,
        "active_account_id": value.get("active_account_id"),
        "settings": _safe_settings(value.get("settings")),
        "server_time": float(server_time) if _finite_number(server_time) else time.time(),
    }


def _finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        return False


def _safe_model_name(value: Any) -> str | None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or not value.strip()
        or any(unicodedata.category(char) in {"Cc", "Cs"} for char in value)
    ):
        return None
    return value


def _safe_model_cooldowns(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result = []
    for record in value:
        if not isinstance(record, dict):
            continue
        model = _safe_model_name(record.get("model"))
        if model is None:
            continue
        result.append(
            {
                "model": model,
                "rate_limited": (
                    record.get("rate_limited")
                    if type(record.get("rate_limited")) is bool
                    else False
                ),
                "cooldown_until": (
                    float(record["cooldown_until"])
                    if _finite_number(record.get("cooldown_until"))
                    else None
                ),
                "cooldown_remaining_seconds": (
                    record["cooldown_remaining_seconds"]
                    if type(record.get("cooldown_remaining_seconds")) is int
                    and record["cooldown_remaining_seconds"] >= 0
                    else 0
                ),
                "last_rate_limited_at": (
                    float(record["last_rate_limited_at"])
                    if _finite_number(record.get("last_rate_limited_at"))
                    else None
                ),
                "cooldown_source": (
                    record.get("cooldown_source")
                    if record.get("cooldown_source") in ("retry_after", "default")
                    else None
                ),
            }
        )
    return result


def _safe_settings(value: Any) -> dict[str, Any]:
    auto_switch = value.get("auto_switch_on_429") if isinstance(value, dict) else None
    auto_checkin = value.get("auto_checkin_enabled") if isinstance(value, dict) else None
    return {
        "auto_switch_on_429": auto_switch if type(auto_switch) is bool else False,
        "auto_checkin_enabled": auto_checkin if type(auto_checkin) is bool else False,
        "default_cooldown_seconds": 60,
    }




class _DuplicateJSONKey(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKey("duplicate JSON key")
        result[key] = value
    return result


async def _read_json_object(request: Request, max_bytes: int) -> dict[str, Any]:
    chunks: list[bytes] = []
    length = 0
    async for chunk in request.stream():
        length += len(chunk)
        if length > max_bytes:
            raise HTTPException(status_code=413, detail="请求内容过大")
        chunks.append(chunk)
    try:
        payload = json.loads(b"".join(chunks) or b"{}", object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, UnicodeDecodeError, _DuplicateJSONKey):
        raise HTTPException(status_code=400, detail="请求 JSON 无效") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求 JSON 必须为对象")
    return payload


def _raise_manager_error(exc: Exception) -> None:
    if isinstance(exc, AccountManagerError):
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None
    if isinstance(exc, KeyError):
        raise HTTPException(status_code=404, detail="未找到请求的账号或任务") from None
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail="请求参数无效") from None
    raise HTTPException(status_code=500, detail="管理请求失败") from None


def _manager_has_account(manager: Any, account_id: str) -> bool:
    has_account = getattr(manager, "has_account", None)
    if callable(has_account):
        try:
            return bool(has_account(account_id))
        except Exception:
            return False
    try:
        accounts = manager.snapshot().get("accounts", [])
    except Exception:
        return False
    return any(
        isinstance(account, dict) and account.get("id") == account_id
        for account in accounts
    )


def create_admin_router(get_manager: Callable[[], AccountManager]) -> APIRouter:
    """Create the loopback-only `/admin` page and `/admin/api` routes."""

    router = APIRouter(route_class=_NoStoreRoute, dependencies=[Depends(_check_admin_request)])

    @router.get("/admin", response_class=HTMLResponse)
    @router.get("/admin/", response_class=HTMLResponse, include_in_schema=False)
    async def admin_page() -> Response:
        try:
            html = resources.files("codebuddy_proxy").joinpath("admin.html").read_text(
                encoding="utf-8"
            )
        except (FileNotFoundError, ModuleNotFoundError, OSError):
            raise HTTPException(status_code=503, detail="管理页面暂不可用") from None
        return HTMLResponse(html)

    @router.get("/admin/api/accounts")
    async def list_accounts() -> dict[str, Any]:
        try:
            return _safe_snapshot(get_manager().snapshot())
        except Exception as exc:
            _raise_manager_error(exc)

    @router.get("/admin/api/model-routing")
    async def get_model_routing(request: Request, refresh: bool = False):
        from codebuddy_proxy.model_routing import ModelRouting, RoutingError, list_candidates
        from codebuddy_proxy.session_routing import SessionRouting
        manager = get_manager()
        try:
            routing = ModelRouting(manager.store.root)
            routes = routing.read()
            candidates, failed = await list_candidates(request.app, manager, refresh)
            session_store = SessionRouting(manager.store.root)
            counts = session_store.binding_counts() if session_store.path.exists() else {}
            exclusions = session_store.assignment_exclusions() if session_store.path.exists() else {}
            return {'routes': routes, 'auto_switch': routing.read_auto_switch(), 'auto_assign': routing.read_auto_assign(), 'assignment_exclusions': exclusions, 'binding_counts': counts, 'models': candidates, 'unavailable_sources': failed}
        except RoutingError:
            raise HTTPException(503, '模型路由配置损坏') from None

    @router.put("/admin/api/model-routing")
    async def set_model_routing(request: Request):
        from codebuddy_proxy.model_routing import ModelRouting, RoutingError, list_candidates, failover_state
        _require_write_header(request)
        payload = await _read_json_object(request, _MAX_SETTINGS_BODY)
        if set(payload) != {'model', 'target'} or not isinstance(payload['model'], str):
            raise HTTPException(400, '请求参数无效')
        model, target = payload['model'], payload['target']
        manager = get_manager()
        if target is not None:
            if (not isinstance(target, dict) or set(target) != {'provider', 'account_id'}
                    or target.get('provider') not in ('workbuddy', 'trae') or not isinstance(target.get('account_id'), str)):
                raise HTTPException(400, '请求参数无效')
            candidates, _ = await list_candidates(request.app, manager)
            if not any(item['provider'] == target['provider'] and item['account_id'] == target['account_id'] for item in candidates.get(model, [])):
                raise HTTPException(409, '所选账号的模型列表中没有该模型')
        try:
            routes = ModelRouting(manager.store.root).set(model, target)
            failover_state.clear(manager.store.root, model)
            from codebuddy_proxy.session_routing import SessionRouting
            session_store = SessionRouting(manager.store.root)
            if session_store.path.exists():
                session_store.clear_bindings(model)
            return {'routes': routes}
        except RoutingError:
            raise HTTPException(400, '模型路由配置无效') from None

    @router.put('/admin/api/model-routing/auto-switch')
    async def set_model_auto_switch(request: Request):
        from codebuddy_proxy.model_routing import ModelRouting, RoutingError, failover_state
        _require_write_header(request)
        payload = await _read_json_object(request, _MAX_SETTINGS_BODY)
        if set(payload) != {'model', 'enabled'} or not isinstance(payload['model'], str) or type(payload['enabled']) is not bool:
            raise HTTPException(400, '请求参数无效')
        root = get_manager().store.root
        try:
            flags = ModelRouting(root).set_auto_switch(payload['model'], payload['enabled'])
            failover_state.clear(root, payload['model'])
            return {'auto_switch': flags}
        except RoutingError:
            raise HTTPException(400, '模型自动切换配置无效') from None

    @router.put('/admin/api/model-routing/auto-assign')
    async def set_model_auto_assign(request: Request):
        from codebuddy_proxy.model_routing import ModelRouting, RoutingError
        _require_write_header(request)
        payload = await _read_json_object(request, _MAX_SETTINGS_BODY)
        if set(payload) != {'model', 'enabled'} or not isinstance(payload['model'], str) or type(payload['enabled']) is not bool:
            raise HTTPException(400, '请求参数无效')
        root = get_manager().store.root
        try:
            routing = ModelRouting(root)
            flags = routing.set_auto_assign(payload['model'], payload['enabled'])
            return {'auto_assign': flags}
        except RoutingError:
            raise HTTPException(400, '模型自动分配配置无效') from None

    @router.put('/admin/api/model-routing/assignment-account')
    async def set_assignment_account(request: Request):
        from codebuddy_proxy.model_routing import list_candidates
        from codebuddy_proxy.session_routing import SessionRouting
        _require_write_header(request)
        payload = await _read_json_object(request, _MAX_SETTINGS_BODY)
        if (set(payload) != {'model', 'provider', 'account_id', 'enabled'}
                or not isinstance(payload['model'], str) or not payload['model'].strip() or len(payload['model']) > 256
                or payload['provider'] not in ('workbuddy', 'trae')
                or not isinstance(payload['account_id'], str) or not payload['account_id']
                or type(payload['enabled']) is not bool):
            raise HTTPException(400, '请求参数无效')
        manager = get_manager()
        candidates, _ = await list_candidates(request.app, manager)
        if not any(item['provider'] == payload['provider'] and item['account_id'] == payload['account_id']
                   for item in candidates.get(payload['model'], [])):
            raise HTTPException(409, '未确认该账号支持此模型')
        store = SessionRouting(manager.store.root)
        store.set_assignment_excluded(payload['model'], payload['provider'], payload['account_id'], not payload['enabled'])
        return {'assignment_exclusions': store.assignment_exclusions()}

    @router.get("/admin/api/accounts/{account_id}/models")
    async def account_models(account_id: str, request: Request, refresh: bool = False) -> dict[str, Any]:
        manager = get_manager()
        if not _manager_has_account(manager, account_id):
            raise HTTPException(status_code=404, detail="未找到该账号")
        try:
            client = manager.store.get_client(account_id)
            return await request.app.state.admin_workbuddy_models(client, refresh)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=502, detail="获取模型列表失败") from None

    @router.get("/admin/api/accounts/{account_id}/credits")
    async def account_credits(account_id: str):
        from codebuddy_proxy.account_credits import CreditsError, workbuddy_credits
        manager = get_manager()
        if not _manager_has_account(manager, account_id):
            raise HTTPException(status_code=404, detail="未找到该账号")
        try:
            client = manager.store.get_client(account_id)
            return await workbuddy_credits(client)
        except CreditsError as exc:
            return JSONResponse({'error': str(exc)}, status_code=502)
        except Exception:
            return JSONResponse({'error': 'query_failed'}, status_code=502)

    @router.get("/admin/api/settings")
    async def get_settings() -> dict[str, Any]:
        try:
            return _safe_settings(get_manager().get_settings())
        except Exception as exc:
            _raise_manager_error(exc)

    @router.patch("/admin/api/settings")
    async def patch_settings(request: Request) -> dict[str, Any]:
        _require_write_header(request)
        payload = await _read_json_object(request, _MAX_SETTINGS_BODY)
        allowed_settings = {"auto_switch_on_429", "auto_checkin_enabled"}
        if (
            not payload
            or not set(payload).issubset(allowed_settings)
            or any(type(value) is not bool for value in payload.values())
        ):
            raise HTTPException(
                status_code=400,
                detail="仅接受非空布尔值账号管理设置",
            )
        try:
            updated = get_manager().set_settings(
                payload.get("auto_switch_on_429"),
                auto_checkin_enabled=payload.get("auto_checkin_enabled"),
            )
            return _safe_settings(updated)
        except Exception as exc:
            _raise_manager_error(exc)

    @router.get(
        "/admin/api/accounts/{account_id}/checkin-status", response_model=None
    )
    async def checkin_status(account_id: str) -> dict[str, Any] | JSONResponse:
        manager = get_manager()
        if not _manager_has_account(manager, account_id):
            raise HTTPException(status_code=404, detail="未找到该账号")
        try:
            result = await manager.checkins.check_status(
                account_id, reason="manual_status"
            )
            return _safe_checkin(result)
        except Exception:
            return JSONResponse(
                _safe_checkin({"status": "error", "error": "签到状态查询失败"}),
                status_code=502,
            )

    @router.post("/admin/api/accounts/{account_id}/checkin", response_model=None)
    async def checkin(account_id: str, request: Request) -> dict[str, Any] | JSONResponse:
        _require_write_header(request)
        manager = get_manager()
        if not _manager_has_account(manager, account_id):
            raise HTTPException(status_code=404, detail="未找到该账号")
        try:
            result = await manager.checkins.check_in(account_id, reason="manual")
            return _safe_checkin(result)
        except Exception:
            return JSONResponse(
                _safe_checkin({"status": "error", "error": "签到请求失败"}),
                status_code=502,
            )

    @router.post("/admin/api/accounts/{account_id}/activate")
    async def activate_account(account_id: str, request: Request) -> dict[str, bool]:
        _require_write_header(request)
        try:
            get_manager().activate(account_id)
            return {"ok": True}
        except Exception as exc:
            _raise_manager_error(exc)

    @router.delete("/admin/api/accounts/{account_id}")
    async def delete_account(account_id: str, request: Request) -> dict[str, bool]:
        _require_write_header(request)
        try:
            get_manager().delete(account_id)
            return {"ok": True}
        except Exception as exc:
            _raise_manager_error(exc)

    @router.post("/admin/api/logins", status_code=202)
    async def start_login(request: Request) -> JSONResponse:
        _require_write_header(request)
        chunks: list[bytes] = []
        length = 0
        async for chunk in request.stream():
            length += len(chunk)
            if length > _MAX_LOGIN_BODY:
                raise HTTPException(status_code=413, detail="请求内容过大")
            chunks.append(chunk)
        try:
            payload = json.loads(b"".join(chunks) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise HTTPException(status_code=400, detail="请求 JSON 无效") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("backend"), str):
            raise HTTPException(status_code=400, detail="backend 必须为 domestic 或 global")
        account_id = payload.get("account_id")
        if account_id is not None and (not isinstance(account_id, str) or not account_id):
            raise HTTPException(status_code=400, detail="account_id 无效")
        try:
            if account_id is None:
                job = _safe_job(get_manager().start_login(payload["backend"]))
            else:
                job = _safe_job(get_manager().start_login(payload["backend"], account_id=account_id))
            return JSONResponse(job, status_code=202)
        except Exception as exc:
            _raise_manager_error(exc)

    @router.get("/admin/api/logins/{job_id}")
    async def get_login(job_id: str) -> dict[str, Any]:
        try:
            return _safe_job(get_manager().get_login(job_id))
        except Exception as exc:
            _raise_manager_error(exc)

    @router.delete("/admin/api/logins/{job_id}")
    async def cancel_login(job_id: str, request: Request) -> dict[str, Any]:
        _require_write_header(request)
        try:
            return _safe_job(get_manager().cancel_login(job_id))
        except Exception as exc:
            _raise_manager_error(exc)

    def trae_manager():
        manager = get_manager()
        with manager._lock:
            result = getattr(manager, "trae_accounts", None)
            if result is None:
                from codebuddy_proxy.trae_accounts import TraeAccounts
                result = TraeAccounts(manager.store.root)
                manager.trae_accounts = result
            return result

    @router.get("/admin/api/trae/accounts")
    async def trae_list():
        try:
            return trae_manager().snapshot()
        except Exception as exc:
            _raise_manager_error(exc)

    @router.post("/admin/api/trae/accounts/{account_id}/activate")
    async def trae_activate(account_id: str, request: Request):
        _require_write_header(request)
        try:
            return trae_manager().activate(account_id)
        except Exception as exc:
            _raise_manager_error(exc)

    @router.get("/admin/api/trae/accounts/{account_id}/credits")
    async def trae_account_credits(account_id: str):
        from codebuddy_proxy.account_credits import CreditsError, trae_credits
        manager = trae_manager()
        with manager.lock:
            if not any(record['id'] == account_id for record in manager._records()):
                raise HTTPException(status_code=404, detail="未找到该账号")
        try:
            return await trae_credits(manager, account_id)
        except CreditsError as exc:
            return JSONResponse({'error': str(exc)}, status_code=502)
        except Exception:
            return JSONResponse({'error': 'query_failed'}, status_code=502)

    @router.post("/admin/api/trae/logins", status_code=202)
    async def trae_login(request: Request):
        _require_write_header(request)
        payload = await _read_json_object(request, _MAX_LOGIN_BODY)
        if set(payload) - {"account_id"} or ("account_id" in payload and (not isinstance(payload["account_id"], str) or not payload["account_id"])):
            raise HTTPException(status_code=400, detail="请求参数无效")
        try:
            return trae_manager().start_login(payload.get("account_id"))
        except Exception as exc:
            _raise_manager_error(exc)

    @router.get("/admin/api/trae/logins/{job_id}")
    async def trae_login_status(job_id: str):
        try:
            manager = trae_manager()
            job = manager.job(job_id)
            if job["status"] == "succeeded":
                await manager.ensure_scheduled(job["account_id"])
            return job
        except Exception as exc:
            _raise_manager_error(exc)

    @router.delete("/admin/api/trae/logins/{job_id}")
    async def trae_cancel_login(job_id: str, request: Request):
        _require_write_header(request)
        try:
            return trae_manager().job(job_id, cancel=True)
        except Exception as exc:
            _raise_manager_error(exc)

    @router.delete("/admin/api/trae/accounts/{account_id}")
    async def trae_delete(account_id: str, request: Request):
        _require_write_header(request)
        try:
            await trae_manager().delete(account_id)
            return {"ok": True}
        except Exception as exc:
            _raise_manager_error(exc)

    @router.get("/admin/api/trae/accounts/{account_id}/checkin-status")
    async def trae_status(account_id: str):
        try:
            return await trae_manager().checkin(account_id)
        except Exception as exc:
            _raise_manager_error(exc)

    @router.post("/admin/api/trae/accounts/{account_id}/checkin")
    async def trae_claim(account_id: str, request: Request):
        _require_write_header(request)
        try:
            return await trae_manager().checkin(account_id, claim=True)
        except Exception as exc:
            _raise_manager_error(exc)

    @router.patch("/admin/api/trae/settings")
    async def trae_settings(request: Request):
        _require_write_header(request)
        payload = await _read_json_object(request, _MAX_SETTINGS_BODY)
        if set(payload) != {"auto_checkin_enabled"} or type(payload["auto_checkin_enabled"]) is not bool:
            raise HTTPException(status_code=400, detail="请求参数无效")
        try:
            return await trae_manager().set_auto(payload["auto_checkin_enabled"])
        except Exception as exc:
            _raise_manager_error(exc)

    @router.api_route(
        "/admin/{unmatched_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
        include_in_schema=False,
    )
    async def unknown_admin_path() -> Response:
        return JSONResponse({"detail": "未找到管理接口"}, status_code=404)

    return router
