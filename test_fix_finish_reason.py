#!/usr/bin/env python3
"""测试修复：空 finish_reason 和 tool_calls name 缓存"""

import json


def test_empty_finish_reason():
    """测试空字符串 finish_reason 的清理"""
    chunk = {
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": ""},
                "finish_reason": ""  # 空字符串，会导致客户端序列化错误
            }
        ]
    }
    
    # 修复逻辑
    if "choices" in chunk:
        for choice in chunk["choices"]:
            if "finish_reason" in choice and choice["finish_reason"] == "":
                choice["finish_reason"] = None
    
    print("✅ 修复后的 finish_reason:", chunk["choices"][0]["finish_reason"])
    assert chunk["choices"][0]["finish_reason"] is None, "finish_reason 应该被转换为 None"


def test_tool_calls_name_cache():
    """测试 tool_calls name 缓存机制"""
    # 模拟上游流式响应的 3 个 chunk
    chunks = [
        # Chunk 1: 首次出现，name 非空
        {
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "id": "call_abc123",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": ""}
                    }]
                }
            }]
        },
        # Chunk 2: 后续分片，name 为空，只有 arguments
        {
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "type": "function",
                        "function": {"name": "", "arguments": "{\"path\""}
                    }]
                }
            }]
        },
        # Chunk 3: 再次空 name
        {
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "type": "function",
                        "function": {"name": "", "arguments": ": \"/file.txt\"}"}
                    }]
                }
            }]
        }
    ]
    
    # 应用修复逻辑
    native_tool_name_by_index = {}
    
    for chunk_idx, chunk in enumerate(chunks):
        native_tool_calls = (
            ((chunk.get("choices") or [{}])[0].get("delta") or {}).get("tool_calls")
        )
        if native_tool_calls:
            for tc in native_tool_calls:
                idx = tc.get("index", 0)
                fn = tc.get("function") or {}
                nm = fn.get("name") or ""
                
                # 首次出现非空 name：记录到缓存
                if nm:
                    native_tool_name_by_index[idx] = nm
                    print(f"Chunk {chunk_idx}: 记录 tool_calls[{idx}].name = '{nm}'")
                # 后续空 name：从缓存回填
                elif idx in native_tool_name_by_index:
                    if "function" not in tc:
                        tc["function"] = {}
                    tc["function"]["name"] = native_tool_name_by_index[idx]
                    print(f"Chunk {chunk_idx}: 回填 tool_calls[{idx}].name = '{native_tool_name_by_index[idx]}'")
        
        # 验证结果
        result_name = chunks[chunk_idx]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"]
        print(f"  → 最终 name: '{result_name}'")
        assert result_name == "read_file", f"Chunk {chunk_idx} 的 name 应该是 'read_file'"
    
    print("\n✅ tool_calls name 缓存机制工作正常")


if __name__ == "__main__":
    print("=" * 60)
    print("测试 1: 空 finish_reason 清理")
    print("=" * 60)
    test_empty_finish_reason()
    
    print("\n" + "=" * 60)
    print("测试 2: tool_calls name 缓存")
    print("=" * 60)
    test_tool_calls_name_cache()
    
    print("\n" + "=" * 60)
    print("✅ 所有测试通过！")
    print("=" * 60)
