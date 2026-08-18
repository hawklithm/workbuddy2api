#!/usr/bin/env python3
"""实时测试：启动代理并发送请求，验证实际headers"""

import subprocess
import time
import httpx
import json
import sys
import signal

def test_live_proxy():
    """测试实际运行的代理"""
    print("=" * 60)
    print("启动代理服务器并测试实际headers")
    print("=" * 60)
    
    # 启动代理服务器
    print("\n🚀 启动代理服务器...")
    proxy_process = subprocess.Popen(
        ["python3", "-m", "codebuddy_proxy"],
        cwd="src",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    
    # 等待服务器启动
    time.sleep(3)
    
    try:
        # 测试请求
        print("\n📤 发送测试请求...")
        proxy_url = "http://localhost:8787/v1/chat/completions"
        
        test_body = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "测试IDE识别headers"}],
            "stream": False,
            "max_tokens": 5
        }
        
        resp = httpx.post(
            proxy_url,
            json=test_body,
            headers={"Content-Type": "application/json"},
            timeout=30.0
        )
        
        print(f"\n✅ 响应状态: {resp.status_code}")
        
        if resp.status_code == 200:
            result = resp.json()
            content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
            print(f"响应内容: {content[:100]}")
            print("\n" + "=" * 60)
            print("✅ 代理工作正常！")
            print("=" * 60)
            print("\n💡 检查代理日志确认IDE识别headers:")
            print("   tail -f logs/proxy.log | grep -E 'X-IDE-|X-Product-Version|X-Machine-Id'")
        else:
            print(f"\n❌ 请求失败: {resp.status_code}")
            print(resp.text[:500])
            
    except httpx.ConnectError:
        print("\n❌ 无法连接到代理服务器")
        print("可能代理启动失败，请检查:")
        print("  1. 是否已登录: python3 -m codebuddy_proxy.codebuddy_client_demo login")
        print("  2. session文件是否存在")
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
    finally:
        # 停止代理
        print("\n🛑 停止代理服务器...")
        proxy_process.send_signal(signal.SIGTERM)
        proxy_process.wait(timeout=5)

if __name__ == "__main__":
    test_live_proxy()
