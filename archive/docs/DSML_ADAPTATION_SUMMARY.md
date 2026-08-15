# DSML 适配总结

## 背景

### codebuddy2api 项目
- **后端返回**：标准 OpenAI `tool_calls` JSON 格式
- **处理方式**：协议转换（OpenAI ↔ Anthropic/Responses）
- **无需 DSML 解析**：后端已经返回标准格式

### 我们的 analyse_codebuddy 项目
- **后端返回**：文本标记嵌入在 `content` 中
- **处理方式**：从 content 解析 → 转换成 tool_calls → 清理 content
- **必须 DSML 解析**：后端返回文本标记而非 JSON

## 发现的工具调用格式

### 1. DSML 格式（DeepSeek Markup Language）
```
<｜｜DSML｜｜tool_calls>
<｜｜DSML｜｜invoke name="bash">
<｜｜DSML｜｜parameter name="command" string="true">ls -la</｜｜DSML｜｜parameter>
</｜｜DSML｜｜invoke>
</｜｜DSML｜｜tool_calls>
```

### 2. 简化格式（驼峰）
```
<tool_call>
<toolName>bash</toolName>
<toolName>ls -la</toolName>
</tool_call>
```

### 3. 简化格式（下划线）
```
<tool_call>
<tool_name>bash</tool_name>
<tool_name>pwd</tool_name>
</tool_call>
```

### 4. 混合格式
```
<tool_call>
<toolName>bash</toolName>
<command>ls -la</command>
<description>查看目录</description>
</tool_call>
```

## 我们的解决方案

### dsml_parser.py 功能

#### 1. DSML 格式解析
- `contains_dsml()` - 检测 DSML 标记
- `parse_dsml_to_tool_calls()` - 解析 DSML 为 tool_calls
- `remove_dsml_from_content()` - 清理 DSML 标记

#### 2. 简化格式解析
- `parse_simple_tool_call()` - 支持所有变体（驼峰/下划线/混合）
- `extract_simple_tool_call_blocks()` - 提取 `<tool_call>` 块
- `remove_simple_tool_calls()` - 清理简化格式标记

#### 3. 统一接口
- `parse_all_tool_calls()` - 解析所有格式
- `remove_all_tool_call_markers()` - 清理所有标记

#### 4. 流式缓冲
- `UnifiedToolCallBuffer` - 累积跨 chunk 的完整工具调用
- `add_chunk()` - 逐 chunk 处理，返回 (content, tool_calls)
- `should_emit_tool_calls()` - 是否应该发送 tool_calls

### codebuddy_proxy.py 集成

#### 在 collect_upstream() 中使用
```python
dsml_buffer = UnifiedToolCallBuffer()

async for line in resp.aiter_lines():
    # ... 解析 SSE chunk ...
    
    if delta.get("content"):
        chunk_content = delta["content"]
        
        # 使用 DSML 缓冲区处理
        cleaned_content, detected_tool_calls = dsml_buffer.add_chunk(chunk_content)
        
        # 累积清理后的 content
        if cleaned_content:
            content += cleaned_content
        
        # 如果检测到 tool_calls
        if detected_tool_calls:
            tool_calls.extend(detected_tool_calls)

# 如果检测到工具调用，修改 finish_reason
if tool_calls and dsml_buffer.should_emit_tool_calls():
    finish_reason = "tool_calls"
```

## 测试结果

### 解析测试
✓ 驼峰格式：`<toolName>bash</toolName>` → `{"command": "ls -la"}`
✓ 下划线格式：`<tool_name>bash</tool_name>` → `{"command": "pwd"}`
✓ 混合格式：`<toolName>bash</toolName><command>ls</command>` → `{"command": "ls"}`
✓ 多个工具调用：正确检测和解析

### 流式缓冲测试
✓ 跨 chunk 累积：`<tool_call>` 分多次到达，正确组装
✓ Content 输出时机：工具调用前的文本立即输出，工具调用中的文本缓冲
✓ Tool calls 触发：完整的 `</tool_call>` 到达时触发
✓ 清理完成：最终 content 不包含任何标记

## 与 codebuddy2api 的差异对比

| 特性 | codebuddy2api | analyse_codebuddy |
|------|---------------|-------------------|
| 后端格式 | 标准 OpenAI JSON | 文本标记 |
| 需要解析 DSML | ❌ 否 | ✅ 是 |
| 协议转换 | OpenAI ↔ Anthropic | 文本 → OpenAI |
| 流式处理 | 直接转发 | 解析+缓冲+转换 |
| 适配目标 | Anthropic/Responses API | Claude CLI (Messages) |

## 当前状态

✅ DSML 格式解析 - 完成
✅ 简化格式解析 - 完成（支持所有变体）
✅ 流式缓冲 - 完成
✅ 与 proxy 集成 - 完成
✅ 测试验证 - 通过

## 使用方法

### 启动服务
```bash
cd ~/workspace/analyse_codebuddy
uv run codebuddy_proxy.py --desensitize
```

### 使用 Claude CLI
```bash
claude "分析当前项目"
```

### 预期行为
- ❌ **之前**：显示 `<tool_call><toolName>bash</toolName>...` 原始文本
- ✅ **现在**：执行工具调用，返回结果

## 关键代码文件

1. **dsml_parser.py** - DSML/简化格式解析器（270 行）
2. **codebuddy_proxy.py** - Proxy 服务，集成 dsml_parser（840 行）
3. **collect_upstream()** - 非流式聚合，使用 UnifiedToolCallBuffer
4. **stream_upstream()** - 流式转发（暂未集成 DSML 处理）

## 下一步

如果仍然显示原始标记，可能原因：
1. **流式场景未处理** - `stream_upstream()` 还没集成 DSML 解析
2. **Claude CLI 使用流式** - 需要在 `stream_upstream()` 中也添加 DSML 处理
3. **日志验证** - 查看 `logs/codebuddy-proxy.jsonl` 确认是否检测到工具调用

解决方案：在 `stream_upstream()` 中也使用 `UnifiedToolCallBuffer`。
