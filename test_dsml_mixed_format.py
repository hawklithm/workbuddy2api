"""测试混合格式 DSML 工具调用解析"""

import pytest
import json
from codebuddy_proxy.dsml_parser import parse_tool_calls, ToolCallStreamBuffer, remove_tool_call_markup


class TestMixedFormatParsing:
    """测试混合格式解析"""
    
    def test_invoke_with_simple_child_tags(self):
        """测试 <invoke name="X"><cmd>...</cmd></invoke> 格式"""
        text = '<invoke name="exec_command"><cmd>pwd</cmd></invoke>'
        result = parse_tool_calls(text)
        
        assert len(result) == 1
        assert result[0]["function"]["name"] == "exec_command"
        args = json.loads(result[0]["function"]["arguments"])
        assert args["cmd"] == "pwd"
    
    def test_invoke_with_multiple_params(self):
        """测试多参数混合格式"""
        text = '<invoke name="bash"><command>ls</command><cwd>/tmp</cwd></invoke>'
        result = parse_tool_calls(text)
        
        assert len(result) == 1
        args = json.loads(result[0]["function"]["arguments"])
        assert args["command"] == "ls"
        assert args["cwd"] == "/tmp"
    
    def test_dsml_format(self):
        """测试 DSML 格式"""
        text = '<||DSML||invoke name="bash"><||DSML||parameter name="command" string="true">ls</||DSML||parameter></||DSML||invoke>'
        result = parse_tool_calls(text)
        
        assert len(result) == 1
        assert result[0]["function"]["name"] == "bash"
    
    def test_dsml_parameter_priority(self):
        """测试 DSML 参数优先级"""
        text = '<invoke name="test"><||DSML||parameter name="key" string="true">dsml_value</||DSML||parameter><key>simple_value</key></invoke>'
        result = parse_tool_calls(text)
        
        assert len(result) == 1
        args = json.loads(result[0]["function"]["arguments"])
        # DSML 参数应该优先
        assert args["key"] == "dsml_value"


class TestStreamBuffer:
    """测试流式缓冲器"""
    
    def test_stream_buffer_basic(self):
        """测试流式缓冲器基本功能"""
        buffer = ToolCallStreamBuffer()
        chunks = ['<invoke name="bash">', '<command>ls</command>', '</invoke>']
        
        detected_calls = None
        for chunk in chunks:
            cleaned, calls = buffer.add_chunk(chunk)
            if calls:
                detected_calls = calls
        
        assert detected_calls is not None
        assert len(detected_calls) == 1


class TestEdgeCases:
    """测试边缘情况"""
    
    def test_empty_parameter(self):
        """测试空参数"""
        text = '<invoke name="test"><param1></param1><param2>value</param2></invoke>'
        result = parse_tool_calls(text)
        
        assert len(result) == 1
        args = json.loads(result[0]["function"]["arguments"])
        assert args["param1"] == ""
        assert args["param2"] == "value"
    
    def test_html_entities(self):
        """测试 HTML 实体解码"""
        text = '<invoke name="bash"><command>echo &lt;test&gt;</command></invoke>'
        result = parse_tool_calls(text)
        
        assert len(result) == 1
        args = json.loads(result[0]["function"]["arguments"])
        assert "<test>" in args["command"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
