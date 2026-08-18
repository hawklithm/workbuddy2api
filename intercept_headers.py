#!/usr/bin/env python3
"""拦截并打印实际发送到CodeBuddy的headers"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

# Monkey patch httpx to intercept requests
import httpx
from typing import Any

original_stream = httpx.AsyncClient.stream

async def patched_stream(self, method: str, url: Any, **kwargs):
    """拦截stream请求，打印headers"""
    headers = kwargs.get('headers', {})
    json_body = kwargs.get('json', {})
    
    print("\n" + "=" * 60)
    print(f"拦截到请求: {method} {url}")
    print("=" * 60)
    print("\n发送的Headers:")
    for k, v in sorted(headers.items()):
        # 隐藏敏感信息
        if k.lower() in ('authorization', 'x-refresh-token'):
            v = f"{v[:20]}..." if len(v) > 20 else v
        print(f"  {k}: {v}")
    
    print(f"\nModel: {json_body.get('model', 'N/A')}")
    print(f"Stream: {json_body.get('stream', False)}")
    print("=" * 60)
    
    # 调用原始方法
    return await original_stream(self, method, url, **kwargs)

# 应用补丁
httpx.AsyncClient.stream = patched_stream

# 导入并运行代理
from codebuddy_proxy.__main__ import app
import uvicorn

if __name__ == "__main__":
    print("🔍 Headers拦截模式启动")
    print("所有发送到CodeBuddy的请求headers都会被打印")
    print("-" * 60)
    uvicorn.run(app, host="127.0.0.1", port=8787, log_level="warning")
