#!/usr/bin/env python3
"""模拟完整的登录流程以验证所有 headers"""

import json
from unittest.mock import patch, Mock, MagicMock
from src.codebuddy_proxy.codebuddy_client_demo import CodeBuddyClient

def test_full_login_flow():
    """模拟完整登录流程，验证每个请求的 headers"""
    print("=" * 70)
    print("完整登录流程 Headers 验证")
    print("=" * 70)
    
    client = CodeBuddyClient()
    
    # 捕获所有 HTTP 请求
    requests_made = []
    
    def mock_urlopen(request, timeout=None):
        # 记录请求
        requests_made.append({
            'url': request.full_url,
            'method': request.get_method(),
            'headers': dict(request.headers),
        })
        
        # 根据 URL 返回不同的响应
        response = Mock()
        response.headers.get.return_value = "application/json"
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        
        if '/auth/state' in request.full_url:
            # 第1步：获取登录 state
            response.read.return_value = json.dumps({
                "data": {
                    "data": {
                        "authUrl": "https://copilot.tencent.com/login?state=test-state",
                        "state": "test-state-123"
                    }
                }
            }).encode()
        elif '/auth/token' in request.full_url:
            # 第2步：获取 token
            response.read.return_value = json.dumps({
                "data": {
                    "data": {
                        "accessToken": "test_access_token_xyz",
                        "refreshToken": "test_refresh_token_xyz",
                        "expiresIn": 3600
                    }
                }
            }).encode()
        elif '/login/account' in request.full_url:
            # 第3步：获取账户信息
            response.read.return_value = json.dumps({
                "data": {
                    "data": {
                        "uid": "12345",
                        "nickname": "Test User",
                        "pluginEnabled": True
                    }
                }
            }).encode()
        else:
            response.read.return_value = b'{}'
        
        return response
    
    with patch('urllib.request.urlopen', side_effect=mock_urlopen):
        with patch('webbrowser.open'):  # 禁用浏览器打开
            with patch('time.sleep'):  # 跳过等待
                try:
                    client.login(open_browser=False)
                except Exception as e:
                    print(f"登录过程出错: {e}")
    
    print(f"\n捕获到 {len(requests_made)} 个 HTTP 请求\n")
    
    # 验证每个请求
    for i, req in enumerate(requests_made, 1):
        print(f"请求 {i}: {req['method']} {req['url']}")
        print("  Headers:")
        
        # 高亮关键 headers
        key_headers = [
            'User-agent', 'X-product-code', 'X-domain', 
            'Authorization', 'X-no-user-id', 'X-no-enterprise-id'
        ]
        
        for key in key_headers:
            value = req['headers'].get(key)
            if value:
                marker = "✓" if value else "✗"
                print(f"    {marker} {key}: {value}")
        
        print()
    
    # 验证关键请求
    login_account_req = [r for r in requests_made if '/login/account' in r['url']]
    
    if login_account_req:
        req = login_account_req[0]
        headers = req['headers']
        
        print("=" * 70)
        print("关键验证：/login/account 请求")
        print("=" * 70)
        
        checks = [
            ("X-product-code", "codebuddy"),
            ("X-domain", "copilot.tencent.com"),
            ("Authorization", "Bearer test_access_token_xyz"),
            ("X-no-user-id", "true"),
            ("X-no-enterprise-id", "true"),
        ]
        
        all_passed = True
        for header_name, expected_value in checks:
            actual = headers.get(header_name)
            matches = actual == expected_value
            symbol = "✓" if matches else "✗"
            
            if matches:
                print(f"{symbol} {header_name}: {actual}")
            else:
                print(f"{symbol} {header_name}: 期望 '{expected_value}', 实际 '{actual}'")
                all_passed = False
        
        print("=" * 70)
        
        if all_passed:
            print("\n🎉 所有 headers 验证通过！")
            print("修复已完整生效：")
            print("  1. _enterprise_headers() 正确提取 X-Domain")
            print("  2. _request() 正确合并传入的 headers")
            print("  3. /login/account 请求包含所有必需的认证头")
            return True
        else:
            print("\n❌ 部分 headers 验证失败")
            return False
    else:
        print("\n❌ 未找到 /login/account 请求")
        return False

if __name__ == "__main__":
    try:
        success = test_full_login_flow()
        exit(0 if success else 1)
    except Exception as e:
        print(f"\n❌ 测试运行错误: {e}")
        import traceback
        traceback.print_exc()
        exit(1)
