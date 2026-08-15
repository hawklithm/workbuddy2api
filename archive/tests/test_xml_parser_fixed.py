"""
完整 XML 解析器测试套件（修复版）
"""
import sys
sys.path.insert(0, '.')

from xml_parser_fixed import (
    parse_tool_calls,
    remove_tool_call_markup,
    ToolCallStreamBuffer,
)


def test_basic_formats():
    """测试基本格式"""
    print("=" * 60)
    print("测试 1: 基本格式")
    print("=" * 60)
    
    test_cases = [
        {
            "name": "标准 XML 格式",
            "input": '''<tool_calls>
  <invoke name="bash">
    <parameter name="cmd">pwd</parameter>
  </invoke>
</tool_calls>''',
            "expected_name": "bash",
            "expected_args": '{"cmd": "pwd"}'
        },
        {
            "name": "DSML 格式",
            "input": '''<｜｜DSML｜｜tool_calls>
  <｜｜DSML｜｜invoke name="bash">
    <｜｜DSML｜｜parameter name="cmd">ls -la</｜｜DSML｜｜parameter>
  </｜｜DSML｜｜invoke>
</｜｜DSML｜｜tool_calls>''',
            "expected_name": "bash",
            "expected_args": '{"cmd": "ls -la"}'
        },
        {
            "name": "简化标签格式",
            "input": '''<tool_calls>
  <invoke name="write_file">
    <path>/tmp/test.txt</path>
    <content>Hello World</content>
  </invoke>
</tool_calls>''',
            "expected_name": "write_file",
            "expected_args": '{"path": "/tmp/test.txt", "content": "Hello World"}'
        },
        {
            "name": "混合格式（<tool_call> + <invoke>）",
            "input": '''<tool_call>
  <invoke name="exec_command">
    <cmd>pwd && ls -la</cmd>
  </invoke>
</tool_call>''',
            "expected_name": "exec_command",
            "expected_args": '{"cmd": "pwd && ls -la"}'
        },
    ]
    
    passed = 0
    failed = 0
    
    for test in test_cases:
        print(f"\n测试: {test['name']}")
        
        result = parse_tool_calls(test['input'])
        
        if not result:
            print(f"  ❌ 失败: 没有解析出工具调用")
            failed += 1
            continue
        
        call = result[0]
        actual_name = call["function"]["name"]
        actual_args = call["function"]["arguments"]
        
        import json
        expected_args_obj = json.loads(test["expected_args"])
        actual_args_obj = json.loads(actual_args)
        
        if actual_name == test["expected_name"] and actual_args_obj == expected_args_obj:
            print(f"  ✅ 通过")
            print(f"     工具: {actual_name}")
            print(f"     参数: {actual_args}")
            passed += 1
        else:
            print(f"  ❌ 失败:")
            print(f"     期望工具: {test['expected_name']}, 实际: {actual_name}")
            print(f"     期望参数: {test['expected_args']}")
            print(f"     实际参数: {actual_args}")
            failed += 1
    
    print(f"\n结果: {passed} 通过 / {failed} 失败\n")
    return passed, failed


def test_markdown_fence_ignore():
    """测试 Markdown fence 忽略"""
    print("=" * 60)
    print("测试 2: Markdown Fence 忽略")
    print("=" * 60)
    
    text = '''说明文档：

```xml
<tool_calls>
  <invoke name="read_file">
    <parameter name="path">example.txt</parameter>
  </invoke>
</tool_calls>
```

实际调用：

<tool_calls>
  <invoke name="read_file">
    <parameter name="path">real.txt</parameter>
  </invoke>
</tool_calls>'''
    
    result = parse_tool_calls(text)
    
    print(f"检测到的工具调用数: {len(result)}")
    
    if len(result) == 1:
        import json
        args = json.loads(result[0]["function"]["arguments"])
        
        if args.get("path") == "real.txt":
            print(f"✅ 通过: 正确跳过了 fence 内的示例")
            return 1, 0
        else:
            print(f"❌ 失败: 解析了错误的调用")
            return 0, 1
    else:
        print(f"❌ 失败: 应该只检测到 1 个调用")
        return 0, 1


def test_auto_repair():
    """测试自动修复缺失包装器"""
    print("=" * 60)
    print("测试 3: 自动修复缺失包装器")
    print("=" * 60)
    
    text = '''<invoke name="bash">
  <cmd>pwd</cmd>
</invoke>'''
    
    result = parse_tool_calls(text, auto_repair=True)
    
    if len(result) == 1 and result[0]["function"]["name"] == "bash":
        import json
        args = json.loads(result[0]["function"]["arguments"])
        if "cmd" in args:
            print(f"✅ 通过: 自动修复了缺失的 <tool_calls> 包装器")
            print(f"   参数: {result[0]['function']['arguments']}")
            return 1, 0
    
    print(f"❌ 失败: 未能自动修复")
    return 0, 1


def test_nested_parameters():
    """测试嵌套参数"""
    print("=" * 60)
    print("测试 4: 嵌套参数")
    print("=" * 60)
    
    text = '''<tool_calls>
  <invoke name="complex_tool">
    <config>
      <host>localhost</host>
      <port>8080</port>
    </config>
    <options>
      <timeout>30</timeout>
    </options>
  </invoke>
</tool_calls>'''
    
    result = parse_tool_calls(text)
    
    if result:
        import json
        args = json.loads(result[0]["function"]["arguments"])
        
        has_config = "config" in args
        has_options = "options" in args
        
        if has_config and has_options:
            print(f"✅ 通过: 正确解析了嵌套参数")
            print(f"   参数: {json.dumps(args, indent=2, ensure_ascii=False)}")
            return 1, 0
        else:
            print(f"❌ 失败: 嵌套参数解析不完整")
            print(f"   实际: {args}")
            return 0, 1
    
    print(f"❌ 失败: 未能解析")
    return 0, 1


def test_stream_buffer():
    """测试流式缓冲器"""
    print("=" * 60)
    print("测试 5: 流式缓冲器")
    print("=" * 60)
    
    buffer = ToolCallStreamBuffer()
    
    chunks = [
        "前言文本\n",
        "<tool_calls>\n",
        "  <invoke name=\"bash\">\n",
        "    <cmd>ls -la</cmd>\n",
        "  </invoke>\n",
        "</tool_calls>\n",
        "后续文本"
    ]
    
    outputs = []
    detected_calls = None
    
    for chunk in chunks:
        cleaned, calls = buffer.add_chunk(chunk)
        if cleaned:
            outputs.append(cleaned)
        if calls:
            detected_calls = calls
    
    final_text = ' '.join(outputs)
    
    if detected_calls and len(detected_calls) == 1:
        if detected_calls[0]["function"]["name"] == "bash":
            if "tool_calls" not in final_text.lower():
                print(f"✅ 通过: 流式缓冲器正确工作")
                return 1, 0
    
    print(f"❌ 失败")
    return 0, 1


def test_markup_removal():
    """测试标记清理"""
    print("=" * 60)
    print("测试 6: 标记清理")
    print("=" * 60)
    
    text = '''让我帮你分析。

<tool_calls>
  <invoke name="bash">
    <cmd>pwd</cmd>
  </invoke>
</tool_calls>

分析完成。'''
    
    cleaned = remove_tool_call_markup(text)
    
    if "tool_calls" not in cleaned and "让我帮你分析" in cleaned:
        print(f"✅ 通过: 标记被完全清理")
        return 1, 0
    
    print(f"❌ 失败")
    return 0, 1


def test_multiple_calls():
    """测试多个工具调用"""
    print("=" * 60)
    print("测试 7: 多个工具调用")
    print("=" * 60)
    
    text = '''<tool_calls>
  <invoke name="bash">
    <cmd>pwd</cmd>
  </invoke>
  <invoke name="bash">
    <cmd>ls</cmd>
  </invoke>
  <invoke name="write_file">
    <path>/tmp/test.txt</path>
    <content>Hello</content>
  </invoke>
</tool_calls>'''
    
    result = parse_tool_calls(text)
    
    if len(result) == 3:
        names = [call["function"]["name"] for call in result]
        if names == ["bash", "bash", "write_file"]:
            print(f"✅ 通过: 正确解析了 3 个工具调用")
            return 1, 0
    
    print(f"❌ 失败: 应该检测到 3 个调用，实际 {len(result)} 个")
    return 0, 1


def run_all_tests():
    """运行所有测试"""
    print("\n")
    print("=" * 60)
    print("完整 XML 解析器测试套件（修复版）")
    print("=" * 60)
    print("\n")
    
    total_passed = 0
    total_failed = 0
    
    tests = [
        test_basic_formats,
        test_markdown_fence_ignore,
        test_auto_repair,
        test_nested_parameters,
        test_stream_buffer,
        test_markup_removal,
        test_multiple_calls,
    ]
    
    for test_func in tests:
        try:
            passed, failed = test_func()
            total_passed += passed
            total_failed += failed
        except Exception as e:
            print(f"❌ 测试异常: {e}")
            import traceback
            traceback.print_exc()
            total_failed += 1
        
        print("\n")
    
    print("=" * 60)
    print(f"总结: {total_passed} 通过 / {total_failed} 失败")
    print("=" * 60)
    
    return total_passed, total_failed


if __name__ == "__main__":
    passed, failed = run_all_tests()
    sys.exit(0 if failed == 0 else 1)
