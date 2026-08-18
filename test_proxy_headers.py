#!/usr/bin/env python3
"""测试代理实际发送的headers"""

import httpx
import json
import sys

def test_proxy_headers():
    """测试代理转发时的实际headers"""
    proxy_url = "http://localhost:8787/v1/chat/completions"
    
    test_body = {
        "model": "gpt-4",
        "messages": [{"role": "user", "content": "测试headers"}],
        "stream": False,
        "max_tokens": 10
    }
    
    print("=" * 60)
    print("测试代理Headers")
    print("=" * 60)
    
    try:
        resp = httpx.post(
            proxy_url,
            json=test_body,
            headers={"Content-Type": "application/json"},
            timeout=30.0
        )
        
        print(f"状态码: {resp.status_code}")
        print(f"\n响应:")
        if resp.status_code == 200:
            print(json.dumps(resp.json(), indent=2, ensure_ascii=False)[:500])
            print("\n✅ 请求成功！检查日志确认headers")
        else:
            print(f"错误: {resp.text[:500]}")
            
    except httpx.ConnectError:
        print("❌ 无法连接到代理服务器")
        print("请先启动代理: uv run --with workbuddy2api python -m codebuddy_proxy")
        sys.exit(1)
    except Exception as e:
        print(f"❌ 请求失败: {e}")
        sys.exit(1)

if __name__ == "__main__":
    test_proxy_headers()
