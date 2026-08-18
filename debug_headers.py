#!/usr/bin/env python3
"""调试工具：输出实际发送到CodeBuddy的headers"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from codebuddy_proxy.codebuddy_client_demo import CodeBuddyClient
import json

def main():
    client = CodeBuddyClient()
    
    print("=" * 60)
    print("当前Session信息")
    print("=" * 60)
    print(json.dumps(client.session, indent=2, ensure_ascii=False))
    
    print("\n" + "=" * 60)
    print("auth_headers() 输出")
    print("=" * 60)
    headers = client.auth_headers()
    for k, v in headers.items():
        print(f"{k}: {v}")
    
    print("\n" + "=" * 60)
    print("完整请求Headers（模拟聊天请求）")
    print("=" * 60)
    full_headers = {
        "User-Agent": "Mozilla/5.0 (compatible; Genie-IDE/1.0)",
        "X-Product-Code": "codebuddy",
        **headers,
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    for k, v in sorted(full_headers.items()):
        print(f"{k}: {v}")
    
    print("\n" + "=" * 60)
    print("可能遗漏的Headers（根据插件分析）")
    print("=" * 60)
    missing_candidates = [
        "X-IDE-Name",
        "X-IDE-Type", 
        "X-IDE-Version",
        "X-Machine-Id",
        "X-Product-Version",
        "X-Session-ID",
    ]
    for header in missing_candidates:
        status = "✅" if header in full_headers else "❌"
        print(f"{status} {header}")

if __name__ == "__main__":
    main()
