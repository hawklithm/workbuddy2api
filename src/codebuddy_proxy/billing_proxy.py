"""Transparent, authenticated proxying of CodeBuddy billing endpoints.

Do not reinterpret upstream business codes, response bodies, or HTTP statuses.
The only responses synthesized here are for local network/transport failures.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

import httpx
from fastapi.responses import JSONResponse, Response


# A shared lock prevents competing 401s from spending the same refresh token
# concurrently. Check token identity under the lock: another request may have
# already refreshed it while we waited.
_refresh_lock = asyncio.Lock()


def _upstream_response(response: httpx.Response) -> Response:
    # Preserve raw bytes and Content-Type including original charset; do not
    # turn upstream errors or non-JSON payloads into HTTP 200 JSON envelopes.
    headers = {}
    content_type = response.headers.get("content-type")
    if content_type:
        headers["content-type"] = content_type
    retry_after = response.headers.get("Retry-After")
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return Response(
        content=response.content,
        status_code=response.status_code,
        headers=headers,
    )


def _transport_error(status: int, error_type: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"type": error_type, "message": "Billing upstream request failed"}},
    )


async def forward_billing(
    state: Any,
    method: str,
    path: str,
    diagnostic: Callable[..., Any],
) -> Response:
    """Send GET without body, POST with the required empty JSON payload.

    A 401 triggers at most one retry, only after a successful refresh (or when
    another concurrent request already updated the access token). If refresh
    fails, preserve the original upstream 401 instead of blindly retrying.
    """
    # Capture once: an account switch while awaiting a response must never
    # refresh the new account or send its credentials to the old endpoint.
    account_client = state.client
    if getattr(state, "managed_auth", False):
        await asyncio.to_thread(state.ensure_auth)
    else:
        state.ensure_auth()
    url = account_client.build_url(path)
    method = method.upper()
    kwargs = {"json": {}} if method == "POST" else {}

    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
        try:
            headers = account_client.auth_headers()
            response = await client.request(method, url, headers=headers, **kwargs)

            if response.status_code == 401:
                async with _refresh_lock:
                    latest_headers = account_client.auth_headers()
                    old_token = headers.get("Authorization")
                    new_token = latest_headers.get("Authorization")

                    if new_token == old_token:
                        try:
                            refreshed = await asyncio.to_thread(account_client.refresh)
                        except Exception as exc:
                            diagnostic(
                                "billing_auth_refresh_failed",
                                path=path, reason=type(exc).__name__,
                            )
                            return _upstream_response(response)
                        if not refreshed:
                            diagnostic("billing_auth_refresh_failed", path=path, reason="not_refreshed")
                            return _upstream_response(response)
                        latest_headers = account_client.auth_headers()
                        new_token = latest_headers.get("Authorization")

                    if not new_token or new_token == old_token:
                        diagnostic("billing_auth_refresh_failed", path=path, reason="token_unchanged")
                        return _upstream_response(response)

                # A fresh Authorization value, and no more than one retry.
                response = await client.request(method, url, headers=latest_headers, **kwargs)
        except httpx.TimeoutException:
            diagnostic("billing_upstream_failed", path=path, reason="timeout")
            return _transport_error(504, "upstream_timeout")
        except httpx.RequestError as exc:
            diagnostic("billing_upstream_failed", path=path, reason=type(exc).__name__)
            return _transport_error(502, "upstream_connection_error")

    if response.status_code == 429:
        manager = getattr(state, "account_runtime", None)
        if manager is not None:
            try:
                # Billing has no model dimension: it must neither cool down
                # inference models nor change the selected account.
                await asyncio.to_thread(
                    manager.handle_rate_limit, account_client,
                    response.headers.get("Retry-After"), model=None, allow_switch=False,
                )
            except Exception as exc:
                diagnostic("billing_rate_limit_record_failed", error_type=type(exc).__name__)
    return _upstream_response(response)
