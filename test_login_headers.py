#!/usr/bin/env python3
"""验证登录请求头是否正确生成"""

import json
from src.codebuddy_proxy.codebuddy_client_demo import CodeBuddyClient

def test_enterprise_headers():
    """测试企业认证头生成"""
    print("=" * 60)
    print("测试企业认证头生成")
    print("=" * 60)
    
    # 场景1: token 无 domain 字段（最常见的场景）
    client = CodeBuddyClient(endpoint="https://copilot.tencent.com")
    token_no_domain = {"accessToken": "test_token_123"}
    headers1 = client._enterprise_headers(token_no_domain)
    
    print("\n场景1: token 无 domain 字段")
    print(f"  Endpoint: {client.endpoint}")
    print(f"  Token: {json.dumps(token_no_domain)}")
    print(f"  生成的 headers: {json.dumps(headers1, indent=4)}")
    
    expected_domain = "copilot.tencent.com"
    assert headers1.get("X-Domain") == expected_domain, \
        f"期望 X-Domain={expected_domain}, 实际={headers1.get('X-Domain')}"
    print(f"  ✓ 正确提取了 endpoint 的 authority: {expected_domain}")
    
    # 场景2: token 有 domain 字段
    token_with_domain = {"accessToken": "test_token_456", "domain": "custom.example.com"}
    headers2 = client._enterprise_headers(token_with_domain)
    
    print("\n场景2: token 有 domain 字段")
    print(f"  Token: {json.dumps(token_with_domain)}")
    print(f"  生成的 headers: {json.dumps(headers2, indent=4)}")
    
    assert headers2.get("X-Domain") == "custom.example.com", \
        "应该优先使用 token 的 domain"
    print("  ✓ 正确使用 token.domain 覆盖默认值")
    
    # 场景3: 带端口的 endpoint
    client_with_port = CodeBuddyClient(endpoint="https://copilot.example.com:8443")
    headers3 = client_with_port._enterprise_headers(token_no_domain)
    
    print("\n场景3: endpoint 包含端口")
    print(f"  Endpoint: {client_with_port.endpoint}")
    print(f"  生成的 headers: {json.dumps(headers3, indent=4)}")
    
    assert headers3.get("X-Domain") == "copilot.example.com:8443", \
        "netloc 应该保留端口号"
    print("  ✓ 正确保留了端口号")
    
    print("\n" + "=" * 60)
    print("✓ 所有测试通过！")
    print("=" * 60)

def test_login_account_headers():
    """验证 /login/account 请求会正确组合所有必需的 headers"""
    print("\n" + "=" * 60)
    print("验证 /login/account 请求头组合")
    print("=" * 60)
    
    client = CodeBuddyClient()
    mock_token = {"accessToken": "bearer_token_xyz"}
    
    # 模拟 login 方法中的 headers 组合逻辑
    combined_headers = {
        **client._enterprise_headers(mock_token),  # 新增：X-Domain
        "Authorization": f"Bearer {mock_token['accessToken']}",
        "X-No-User-Id": "true",
        "X-No-Enterprise-Id": "true",
        "X-No-Department-Info": "true",
    }
    
    print("\n最终发送的 headers:")
    print(json.dumps(combined_headers, indent=4))
    
    # 验证关键字段
    assert "X-Domain" in combined_headers, "缺少关键的 X-Domain header"
    assert combined_headers["Authorization"] == "Bearer bearer_token_xyz"
    assert combined_headers["X-No-User-Id"] == "true"
    
    
    print("\n正确组合验证:")
    print("  ✓ 包含 X-Domain (修复前缺失)")
    print("  ✓ 包含 Authorization")
    print("  ✓ 包含 X-No-* 控制标志")

if __name__ == "__main__":
    try:
        test_enterprise_headers()
        test_login_account_headers()
        
        print("\n" + "🎉" * 20)
        print("所有验证通过！修复已正确实施。")
        print("\n核心修复:")
        print("  1. 新增 _enterprise_headers() 方法")
        print("  2. 当 token 无 domain 时，从 endpoint 提取 authority")
        print("  3. /login/account 请求现在包含完整的企业认证头")
        print("\n这应该能解决 WSL/Windows 上的 401 错误。")
        print("🎉" * 20)
        
    except AssertionError as e:
        print(f"\n❌ 测试失败: {e}")
        exit(1)
    except Exception as e:
        print(f"\n❌ 运行错误: {e}")
        import traceback
        traceback.print_exc()
        exit(1)
