#!/usr/bin/env python3
"""Optional live-proxy integration smoke test.

By default pytest skips this test because it requires a running local proxy,
which may in turn make authenticated upstream requests.

Opt in from a prepared environment:
    WORKBUDDY_RUN_PROXY_INTEGRATION=1 python -m pytest test_proxy_headers.py -vv -s

Standalone execution remains available:
    python test_proxy_headers.py
"""
from __future__ import annotations

import json
import os

import httpx
import pytest


@pytest.mark.skipif(
    os.environ.get("WORKBUDDY_RUN_PROXY_INTEGRATION") != "1",
    reason="requires a running proxy; set WORKBUDDY_RUN_PROXY_INTEGRATION=1 to opt in",
)
def test_proxy_headers():
    """Manually inspect real proxy header forwarding with a running server."""
    proxy_url = os.environ.get(
        "WORKBUDDY_PROXY_TEST_URL",
        "http://127.0.0.1:8787/v1/chat/completions",
    )
    test_body = {
        "model": "gpt-4",
        "messages": [{"role": "user", "content": "测试headers"}],
        "stream": False,
        "max_tokens": 10,
    }

    print("=" * 60)
    print("测试代理Headers")
    print("=" * 60)

    try:
        resp = httpx.post(
            proxy_url,
            json=test_body,
            headers={"Content-Type": "application/json"},
            timeout=30.0,
        )
    except httpx.ConnectError as exc:
        pytest.fail(
            f"无法连接代理服务器 {proxy_url}。请先启动服务，然后重试：{exc}"
        )
    except httpx.HTTPError as exc:
        pytest.fail(f"代理请求失败: {exc}")

    print(f"状态码: {resp.status_code}")
    if resp.status_code == 200:
        try:
            print(json.dumps(resp.json(), indent=2, ensure_ascii=False)[:500])
        except ValueError:
            print(resp.text[:500])
        print("请求成功；请结合服务端日志检查转发的 headers")
    else:
        print(f"错误: {resp.text[:500]}")
    assert resp.status_code == 200, f"代理响应 HTTP {resp.status_code}: {resp.text[:500]}"


if __name__ == "__main__":
    # Direct invocation is an explicit opt-in; pytest's skip marker applies
    # only when collected through pytest, not when calling this function here.
    test_proxy_headers()
