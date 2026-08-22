#!/usr/bin/env python3
"""完整测试修复后的请求头传递"""

import json
from unittest.mock import patch, Mock
from src.codebuddy_proxy.codebuddy_client_demo import CodeBuddyClient

def test_request_headers_merging():
    """测试 _request 方法正确合并 headers"""
    print("=" * 60)
    print("测试 _request 方法 headers 合并")
    print("=" * 60)
    
    client = CodeBuddyClient()
    
    # Mock urllib.request.urlopen
    with patch('urllib.request.urlopen') as mock_urlopen:
        mock_response = Mock()
        mock_response.read.return_value = b'{"data": {"data": "test"}}'
        mock_response.headers.get.return_value = "application/json"
        mock_response.__enter__ = Mock(return_value=mock_response)
        mock_response.__exit__ = Mock(return_value=False)
        mock_urlopen.return_value = mock_response
        
        # 调用 _request 并传入自定义 headers
        test_headers = {
            "X-Domain": "copilot.tencent.com",
            "Authorization": "Bearer test_token_123",
            "X-No-User-Id": "true",
            "X-No-Enterprise-Id": "true",
        }
        
        client._request("GET", "/v2/plugin/test", headers=test_headers)
        
        # 验证实际发出的请求
        assert mock_urlopen.called, "_request 应该调用 urlopen"
        
        actual_request = mock_urlopen.call_args[0][0]
        actual_headers = dict(actual_request.headers)
        
        print("\n实际发送的 headers:")
        print(json.dumps(actual_headers, indent=2, ensure_ascii=False))
        
        # 验证基础 headers
        assert actual_headers.get("User-agent") == "Mozilla/5.0 (compatible; Genie-IDE/1.0)", \
            "应该包含 User-Agent"
        assert actual_headers.get("X-product-code") == "codebuddy", \
            "应该包含 X-Product-Code"
        
        # 验证传入的 headers
        assert actual_headers.get("X-domain") == "copilot.tencent.com", \
            "应该包含 X-Domain"
        assert actual_headers.get("Authorization") == "Bearer test_token_123", \
            "应该包含 Authorization"
        assert actual_headers.get("X-no-user-id") == "true", \
            "应该包含 X-No-User-Id"
        
        print("\n✓ 基础 headers 正确")
        print("✓ 传入的 headers 全部合并")
        print("=" * 60)

if __name__ == "__main__":
    try:
        test_request_headers_merging()
        print("\n🎉 修复验证通过！")
        print("_request 方法现在正确合并调用者传入的 headers")
    except AssertionError as e:
        print(f"\n❌ 测试失败: {e}")
        exit(1)
    except Exception as e:
        print(f"\n❌ 运行错误: {e}")
        import traceback
        traceback.print_exc()
        exit(1)
