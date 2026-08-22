"""回归测试：ensure_auth() 认证失败应包装为结构化 401，而非底层异常 500。

根因：ProxyState.ensure_auth() 直接调用 client.ensure_authenticated()，token 过期
+ 刷新网络错误（CodeBuddyError）或登录失败时异常未捕获，一路传播到 FastAPI → 500。
修复：ensure_auth() 捕获底层异常，包装成 HTTPException(401) 结构化错误；
已结构化的 HTTPException（如 503）原样透传。
"""

import pathlib
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

import codebuddy_proxy.__main__ as m
from codebuddy_proxy.codebuddy_client_demo import CodeBuddyError


def _make_state(client):
    return m.ProxyState(client=client, mock_dir=None, log_file=None)


def test_ensure_auth_wraps_auth_failure_as_401():
    """认证失败（CodeBuddyError）应包装为 401 + authentication_error，而非 500。"""
    client = MagicMock()
    client.ensure_authenticated.side_effect = CodeBuddyError("请求失败 /auth/token/refresh: 网络不可达")
    state = _make_state(client)

    with pytest.raises(HTTPException) as exc_info:
        state.ensure_auth()

    assert exc_info.value.status_code == 401
    err = exc_info.value.detail["error"]
    assert err["type"] == "authentication_error"
    assert "重新登录" in err["message"]


def test_ensure_auth_passes_http_exception_through():
    """已结构化的 HTTPException（如 503）应原样透传，不被包成 401。"""
    client = MagicMock()
    client.ensure_authenticated.side_effect = HTTPException(status_code=503, detail="proxy not ready")
    state = _make_state(client)

    with pytest.raises(HTTPException) as exc_info:
        state.ensure_auth()

    assert exc_info.value.status_code == 503


def test_ensure_auth_skips_when_mock_dir_set():
    """mock_dir 非 None 时不应调用 client.ensure_authenticated。"""
    client = MagicMock()
    state = m.ProxyState(client=client, mock_dir=pathlib.Path("mock"), log_file=None)
    state.ensure_auth()
    client.ensure_authenticated.assert_not_called()


def test_ensure_auth_noop_when_token_valid():
    """token 有效时 ensure_authenticated 正常返回（不抛异常）。"""
    client = MagicMock()
    client.ensure_authenticated.return_value = None
    state = _make_state(client)
    state.ensure_auth()  # 不抛异常即通过
    client.ensure_authenticated.assert_called_once()
