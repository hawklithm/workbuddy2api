#!/usr/bin/env python3
"""模拟 Bug B2：DSML 解析器覆盖原生 tool_calls"""

import json
import sys
sys.path.insert(0, 'src')

from codebuddy_proxy.dsml_parser import DSMLStreamBuffer


def simulate_bug_b2():
    """
    模拟场景：上游在同一个 chunk 中返回：
    1. delta.tool_calls（原生 OpenAI 格式）
    2. delta.content 包含 <invoke> 标签（如模型输出的示例代码）
    """
    
    print("=" * 80)
    print("Bug B2 模拟测试：DSML 解析器误触发覆盖原生 tool_calls")
    print("=" * 80)
    
    # 模拟上游返回的 chunk（这是真实可能发生的场景）
    upstream_chunk = {
        "id": "test_123",
        "choices": [{
            "index": 0,
            "delta": {
                "role": "assistant",
                # 原生工具调用（正确的）
                "tool_calls": [{
                    "index": 0,
                    "id": "call_real_tool",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path": "/tmp/test.txt"}'
                    }
                }],
                # content 中包含 XML 标签（误触发 DSML）
                "content": """我可以帮你读取文件。使用方法如下：

<invoke name="read_file">
  <parameter name="path">/example/path.txt</parameter>
</invoke>
"""
            },
            "finish_reason": None
        }]
    }
    
    print("\n【上游返回的 chunk】")
    print(json.dumps(upstream_chunk, indent=2, ensure_ascii=False))
    
    # 当前代码的处理逻辑（Bug B2 存在）
    print("\n" + "=" * 80)
    print("【当前代码逻辑 - 存在 Bug】")
    print("=" * 80)
    
    # 1. 提取原生 tool_calls
    native_tool_calls = upstream_chunk["choices"][0]["delta"].get("tool_calls")
    print(f"\n1. 提取原生 tool_calls: {native_tool_calls is not None}")
    if native_tool_calls:
        print(f"   原生工具名称: {native_tool_calls[0]['function']['name']}")
    
    # 2. 提取 content 并通过 DSML 解析
    chunk_content = upstream_chunk["choices"][0]["delta"].get("content", "")
    print(f"\n2. 提取 content 长度: {len(chunk_content)} 字符")
    print(f"   是否包含 <invoke>: {'<invoke' in chunk_content}")
    
    dsml_buffer = DSMLStreamBuffer()
    cleaned_content, detected_tool_calls = dsml_buffer.add_chunk(chunk_content)
    
    print(f"\n3. DSML 解析结果:")
    print(f"   检测到工具调用: {detected_tool_calls is not None}")
    print(f"   should_emit_tool_calls(): {dsml_buffer.should_emit_tool_calls()}")
    
    if detected_tool_calls:
        print(f"   DSML 解析的工具: {[tc['function']['name'] for tc in detected_tool_calls]}")
    
    # 4. 当前代码的覆盖逻辑（Bug 所在）
    print(f"\n4. 当前代码的判断:")
    print(f"   条件: detected_tool_calls and dsml_buffer.should_emit_tool_calls()")
    print(f"   结果: {detected_tool_calls and dsml_buffer.should_emit_tool_calls()}")
    
    if detected_tool_calls and dsml_buffer.should_emit_tool_calls():
        print(f"   ⚠️  Bug 触发！将用 DSML 解析的 tool_calls 覆盖原生 tool_calls")
        print(f"   覆盖前: {native_tool_calls[0]['function']['name']}")
        
        # 模拟覆盖操作（当前代码实际上在 __main__.py 1156-1171 行）
        # 注意：detected_tool_calls 已经是 OpenAI 格式，但当前代码会重新构造
        upstream_chunk["choices"][0]["delta"]["tool_calls"] = [
            {
                "index": idx,
                "id": f"call_{idx}",
                "type": "function",
                "function": {
                    "name": tc["function"]["name"],
                    "arguments": tc["function"]["arguments"]
                }
            }
            for idx, tc in enumerate(detected_tool_calls)
        ]
        
        print(f"   覆盖后: {upstream_chunk['choices'][0]['delta']['tool_calls'][0]['function']['name']}")
        print(f"\n   ❌ 结果：原生的 tool_calls 被 DSML 解析的覆盖")
        print(f"   ❌ 问题场景1：如果 content 中的 <invoke> 是示例代码，会覆盖真实工具调用")
        print(f"   ❌ 问题场景2：如果 DSML 解析的 name 为空或错误，原生 tool_calls 丢失")
        print(f"\n   ❌ 问题：DSML 解析器误判文本中的 XML 标签为工具调用，覆盖真实的原生 tool_calls")
    
    # 修复后的逻辑
    print("\n" + "=" * 80)
    print("【修复后的代码逻辑】")
    print("=" * 80)
    
    # 重新初始化
    upstream_chunk_fixed = {
        "id": "test_123",
        "choices": [{
            "index": 0,
            "delta": {
                "tool_calls": [{
                    "index": 0,
                    "id": "call_real_tool",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path": "/tmp/test.txt"}'
                    }
                }],
                "content": chunk_content
            }
        }]
    }
    
    native_tool_calls_fixed = upstream_chunk_fixed["choices"][0]["delta"].get("tool_calls")
    
    print(f"\n修复后的判断:")
    print(f"   条件: detected_tool_calls and dsml_buffer.should_emit_tool_calls() and not native_tool_calls")
    print(f"   结果: {detected_tool_calls and dsml_buffer.should_emit_tool_calls() and not native_tool_calls_fixed}")
    print(f"\n   ✅ 由于 native_tool_calls 存在，跳过 DSML 覆盖")
    print(f"   ✅ 原生 tool_calls 被保留: {native_tool_calls_fixed[0]['function']['name']}")
    
    print("\n" + "=" * 80)
    print("【结论】")
    print("=" * 80)
    print("Bug B2 确实存在！")
    print("触发条件：上游同时返回原生 tool_calls 和包含 XML 标签的 content")
    print("修复方案：在 DSML 覆盖逻辑中添加 'and not native_tool_calls' 条件")
    print("=" * 80)


if __name__ == "__main__":
    simulate_bug_b2()
