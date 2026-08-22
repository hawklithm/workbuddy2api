# CodeBuddy API 代理

> 一个轻量级的 API 代理服务，将 CodeBuddy 底层接口转换为标准的 OpenAI、Anthropic 和 Responses 协议格式。

> **English version: see [README.md](README.md).**

## ✨ 核心特性

- **协议转换** - 支持 OpenAI Chat Completions、Anthropic Messages API 和 Responses 三种标准格式
- **脱敏处理** - 内置智能脱敏模块，自动过滤敏感信息（账号、密码、密钥、品牌词、路径等），有效缓解审核误拦
- **消息压缩** - 智能压缩历史消息，大幅降低 token 使用量（适用于 Codex CLI 等长上下文场景）
- **工具调用支持** - 完整支持 function calling 和 tool use 特性，自动过滤无效工具定义
- **DSML 解析** - 自动识别和转换 DeepSeek 标记语言（DSML）格式的工具调用
- **流式响应** - 支持 SSE 流式输出，实时返回生成内容，内置 60 秒超时保护
- **多账号管理** - 支持多个登录态隔离，方便工作/个人账号切换
---

## 安装

推荐使用 [uv](https://docs.astral.sh/uv/) 从 PyPI 运行：

```bash
# 安装 uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# 运行最新可用版本（uv 会自动创建环境并安装依赖）
uv run --with workbuddy2api python -m codebuddy_proxy \
  --log-file "$HOME/.workbuddy2api/codebuddy-proxy.jsonl"

# 强制刷新缓存后运行最新版本
uv run --refresh-package workbuddy2api --with workbuddy2api \
  python -m codebuddy_proxy \
  --log-file "$HOME/.workbuddy2api/codebuddy-proxy.jsonl"
```

后续启动无需手动激活虚拟环境，重复执行上述 `uv run` 命令即可。

### 本地源码启动

在项目根目录执行以下命令，使用当前工作区源码而不是 PyPI 中的已发布版本：

```bash
# 同步本地项目依赖
uv sync

# 启动本地源码
uv run python -m codebuddy_proxy --desensitize
```

首次使用需要登录时：

```bash
uv run python -m codebuddy_proxy --login --desensitize
```

## 快速开始

### 1. 启动 proxy

```bash
# 使用最新版本（推荐）
uv run --with workbuddy2api python -m codebuddy_proxy --desensitize

# 首次使用：登录并启动
uv run --with workbuddy2api python -m codebuddy_proxy --login --desensitize
```

默认监听 `http://127.0.0.1:8787`

### 2. 验证

```bash
curl http://127.0.0.1:8787/health
curl http://127.0.0.1:8787/v1/models
```

### 3. 接入客户端

#### Codex CLI

编辑 `~/.codex/config.toml`：

```toml
[model_providers.codebuddy]
name = "CodeBuddy (via local proxy)"
base_url = "http://127.0.0.1:8787/v1"
wire_api = "responses"

[profiles.codebuddy]
model = "glm-5.2"
model_provider = "codebuddy"
```

使用：

```bash
codex --profile codebuddy "你的任务"
```

#### Claude Code + CC Switch

在 CC Switch 配置中添加：

```json
{
  "DeepSeek-V4": {
    "base_url": "http://127.0.0.1:8787/v1/messages",
    "api_key": "",
    "model": "deepseek-v4-pro"
  }
}
```

#### OpenCode

编辑项目根目录的 `opencode.json`：

```json
{
  "$schema": "https://opencode.ai/config.json",
  "model": "codebuddy/glm-5.2",
  "providers": {
    "codebuddy": {
      "name": "CodeBuddy (via local proxy)",
      "package": "@opencode-ai/ai/providers/openai-compatible",
      "settings": {
        "baseURL": "http://127.0.0.1:8787/v1",
        "apiKey": "noop"
      },
      "models": {
        "glm-5.2": { "modelID": "glm-5.2", "name": "GLM-5.2" },
        "deepseek-v4-pro": { "modelID": "deepseek-v4-pro", "name": "DeepSeek V4 Pro" },
        "kimi-k2.7": { "modelID": "kimi-k2.7", "name": "Kimi K2.7" }
      }
    }
  }
}
```

启动 opencode 后，用 `/models` 命令在 `codebuddy` provider 下选择模型（如 `codebuddy/glm-5.2`）。

> 说明：`baseURL` 指向本地代理；`apiKey` 填任意占位值即可（本地代理不校验密钥）。`models` 的 key 是 OpenCode 内的模型 ID（供选择），`modelID` 是发给代理的实际模型名。密钥字段请用 `apiKey`（而非某些旧模板里的 `env_key`），避免绑定错误的 provider 语义。

#### Grok CLI

编辑 `~/.grok/config.toml`，为每个模型添加一个指向本地代理的 `[model.<name>]` 配置。grok 默认使用 OpenAI Chat Completions 后端（`/v1/chat/completions`），本代理原生支持：

```toml
[models]
default = "hy3"   # 可选：设置默认模型

[model.hy3]
model = "hy3"                        # 发给代理的模型 ID
base_url = "http://127.0.0.1:8787/v1"
name = "HY3 Main"                    # 模型选择器里显示的名称
api_key = "noop"                     # 任意占位值即可

[model.dv4f]
model = "deepseek-v4-flash"
base_url = "http://127.0.0.1:8787/v1"
name = "DeepSeek V4 Flash"
api_key = "noop"
```

之后在 TUI 中用 `/model hy3` 切换（或 `Ctrl+M` 打开模型选择器），或命令行模式 `grok -m hy3 "你的任务"`。

> 说明：`base_url` 指向本地代理；`api_key` 填任意占位值即可（本地代理不校验密钥）。如需改用其他协议，可设置 `api_backend = "responses"` 走 `/v1/responses` 端点，或 `"messages"` 走 Anthropic 的 `/v1/messages` 端点。

#### 其他 OpenAI 兼容客户端

- Base URL: `http://127.0.0.1:8787/v1`
- API Key: 留空（或填你启动时用 `--api-key` 设置的值）
- 模型名: `glm-5.2` / `deepseek-v4-pro` / `kimi-k2.7` / `auto` 等

## 命令行参数

```bash
--host HOST              监听地址（默认 127.0.0.1）
--port PORT              监听端口（默认 8787）
--endpoint ENDPOINT      CodeBuddy 后端地址
--session-file PATH      会话文件路径（默认 ~/.codebuddy-session.json）
--log-file PATH          JSONL 日志文件（默认 ~/.workbuddy2api/codebuddy-proxy.jsonl）
--desensitize            启用脱敏处理（推荐）
--optimize-context       启用消息压缩优化（Codex CLI 推荐）
--login                  启动时执行浏览器登录
--no-browser             登录时不打开浏览器
--verbose-llm            log full LLM request/response content 
                        (default: summary only, saves 98% space)
--mock-dir DIR           使用 mock 数据（测试用）
```

### 环境变量

```bash
CODEBUDDY_PROXY_HOST      # 等同 --host
CODEBUDDY_PROXY_PORT      # 等同 --port
CODEBUDDY_ENDPOINT        # 等同 --endpoint
CODEBUDDY_PROXY_LOG_FILE  # 等同 --log-file
```

## 常见场景

### 首次使用（需要登录）

```bash
uv run --with workbuddy2api python -m codebuddy_proxy --login \
  --log-file "$HOME/.workbuddy2api/codebuddy-proxy.jsonl"
```

浏览器打开后登录，成功后 proxy 自动启动。

### 日常使用（自动读取登录态）

```bash
uv run --with workbuddy2api python -m codebuddy_proxy --desensitize \
  --log-file "$HOME/.workbuddy2api/codebuddy-proxy.jsonl"
```

### Codex CLI 场景（启用压缩优化）

```bash
uv run --with workbuddy2api python -m codebuddy_proxy --desensitize --optimize-context \
  --log-file "$HOME/.workbuddy2api/codebuddy-proxy.jsonl"
```

### 多账号切换

```bash
# 账号 1
uv run --with workbuddy2api python -m codebuddy_proxy --session-file ~/.codebuddy-work.json --login \
  --log-file "$HOME/.workbuddy2api/codebuddy-proxy.jsonl"

# 账号 2
uv run --with workbuddy2api python -m codebuddy_proxy --session-file ~/.codebuddy-personal.json --login \
  --log-file "$HOME/.workbuddy2api/codebuddy-proxy.jsonl"
```

### 监听所有网卡（局域网共享）

```bash
uv run --with workbuddy2api python -m codebuddy_proxy --host 0.0.0.0 --desensitize \
  --log-file "$HOME/.workbuddy2api/codebuddy-proxy.jsonl"
```

## API 接口

所有接口默认不需要在请求中额外携带 token，代理会使用本地 session 完成认证。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/health` | 查询本地服务和认证状态 |
| GET | `/v1/models` | 查询 CodeBuddy 模型列表 |
| POST | `/v1/chat/completions` | OpenAI Chat Completions，支持 tools 和流式响应 |
| POST | `/v1/responses` | Responses API，兼容 Codex CLI |
| POST | `/v1/messages` | Anthropic Messages API，兼容 Claude Code / CC Switch |

### `/health` - 健康检查

```bash
curl http://127.0.0.1:8787/health
```

返回示例：

```json
{
  "status": "ok",
  "uptime_seconds": 123.45,
  "authenticated": true,
  "token_valid": true
}
```

### `/v1/models` - 模型列表

```bash
curl http://127.0.0.1:8787/v1/models
```

返回 OpenAI 格式的模型列表，`data[].id` 就是后续请求中的 `model` 值（如 `deepseek-v4-flash`、`glm-5.2`）。

### `/v1/chat/completions` - OpenAI Chat

**非流式请求：**

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "deepseek-v4-flash",
    "messages": [{"role": "user", "content": "写一个快排"}]
  }'
```

**流式请求：**

```bash
curl -N http://127.0.0.1:8787/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "glm-5.2",
    "stream": true,
    "messages": [{"role": "user", "content": "hi"}]
  }'
```

支持 `tools`、`tool_choice`、`stream_options` 等完整 OpenAI 特性。

### `/v1/responses` - Responses API

用于兼容 Codex CLI：

```bash
curl http://127.0.0.1:8787/v1/responses \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "default",
    "input": "写一个快排"
  }'
```

支持 `instructions` (system prompt)、消息形式的 `input`、`tools`、`tool_choice` 和 `stream`。

**💡 提示：** 使用 `--optimize-context` 可大幅减少 Codex CLI 的 token 使用。

### `/v1/messages` - Anthropic Messages

用于兼容 Claude Code / CC Switch：

```bash
curl http://127.0.0.1:8787/v1/messages \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "deepseek-v4-pro",
    "max_tokens": 4096,
    "messages": [{"role": "user", "content": "hi"}]
  }'
```

设置 `"stream": true` 时返回 Anthropic SSE 事件流。

## 高级功能

### 脱敏处理 (`--desensitize`)

对 system 消息中的敏感词插入零宽空格（U+200B），打断后端关键词匹配，缓解合规模板被审核误拦。

#### 何时需要使用

**强烈推荐启用的场景：**

1. **对接 Claude Code / CC Switch**
   - Claude Code 的 system prompt 包含大量 Anthropic 品牌词和安全合规声明
   - 腾讯后端可能将竞争品牌词（"Claude"、"Anthropic"）视为敏感内容
   - 不启用脱敏时，几乎每次请求都会被审核拦截

2. **对接 Codex CLI / Oh My Posh 等 agentic 工具**
   - 这些工具的 system prompt 含有大量安全术语（DoS、exploit、credential testing 等）
   - 即使是合规的"拒绝有害请求"声明，也可能被关键词匹配误拦

3. **使用包含安全术语的自定义 system prompt**
   - 安全研究、渗透测试相关的合规对话
   - 需要讨论漏洞、攻击防御的技术文档生成

**典型错误信息：**
```json
{
  "error": {
    "message": "内容违规",
    "type": "content_policy_violation"
  }
}
```
或后端返回空响应、连接中断。

**不需要启用的场景：**
- ✅ 普通对话（无安全术语）
- ✅ 使用官方 CodeBuddy 客户端（已内置处理）
- ✅ 纯粹的代码生成（无品牌词/安全声明）

#### 典型使用案例


## 🔧 技术细节

### 工具调用兼容性

代理自动过滤不兼容的工具定义，确保上游 API 接受：

**过滤规则**：
- ❌ 非 `type: "function"` 的工具（如 `web_search`）
- ❌ `parameters` 为空对象 `{}` 的工具
- ❌ `parameters` 缺少 `type` 字段的工具
- ✅ 清理 `additionalProperties` 和 `strict` 字段（CodeBuddy 后端不支持）

**日志事件**：`tools_filtered` 记录过滤详情

### 流式响应保护

**超时配置**：
- **连接超时**：30 秒
- **读取超时**：300 秒（两次数据接收间隔）
- **总时长限制**：60 秒（防止流无限期运行）

**为什么需要总时长限制**：
- httpx 的 `read timeout` 只限制两次数据间隔，不限制总时长
- 上游持续发送数据时，流可能无限期运行（观察到 6+ 分钟，2000+ chunks 的异常流）
- 60 秒适合交互式对话，可根据场景调整（代码中修改 `MAX_STREAM_DURATION`）

**日志事件**：`stream_duration_exceeded` 记录超时截断

### DSML 解析

自动识别三种工具调用标记格式：

1. **DeepSeek DSML**：`<||DSML||tool_calls>` / `<||DSML||invoke name="...">`
2. **Claude 风格**：`<tool_call><invoke name="exec_command"><cmd>...</cmd></invoke></tool_call>`
3. **简化格式**：`<tool_call><toolName>bash</toolName>...</tool_call>`

解析后转换为标准 OpenAI `tool_calls` 格式，并从响应内容中清理标记。

### 协议适配

| 源协议 | 目标协议 | 转换器 | 说明 |
|--------|----------|--------|------|
| CodeBuddy Chat | OpenAI Chat | 直接透传 | 添加 DSML 解析 |
| CodeBuddy Chat | Responses API | `ResponsesStreamConverter` | 事件序列转换 |
| CodeBuddy Chat | Anthropic Messages | `AnthropicStreamConverter` | 流式事件映射 |

**流式事件映射**（Responses API）：
```
upstream chunk → response.output_text.delta
工具调用 → response.output_item.added (function_call)
完成 → response.completed
```

**案例 1: 对接 Claude Code**

```bash
# 必须启用 --desensitize，否则几乎每次都被拦截
uv run --with workbuddy2api python -m codebuddy_proxy --desensitize \
  --log-file "$HOME/.workbuddy2api/codebuddy-proxy.jsonl"

# 在 Claude Code / CC Switch 中配置
# Base URL: http://127.0.0.1:8787/v1/messages
```

**案例 2: 对接 Codex CLI**

```bash
# 同时启用脱敏和消息压缩（最佳配置）
uv run --with workbuddy2api python -m codebuddy_proxy --desensitize --optimize-context \
  --log-file "$HOME/.workbuddy2api/codebuddy-proxy.jsonl"

# 在 Codex CLI 配置文件中
# base_url: http://127.0.0.1:8787/v1/responses
```

**案例 3: 安全研究对话**

```bash
# 启用脱敏以避免合规术语被误拦
uv run --with workbuddy2api python -m codebuddy_proxy --desensitize \
  --log-file "$HOME/.workbuddy2api/codebuddy-proxy.jsonl"

# 示例请求
curl http://127.0.0.1:8787/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "deepseek-v4-pro",
    "messages": [
      {
        "role": "system",
        "content": "You are a security expert. Refuse requests for exploit development."
      },
      {
        "role": "user",
        "content": "解释 SQL injection 的防御措施"
      }
    ]
  }'
```

#### 工作原理

```python
# 原文
"Refuse requests for DoS attacks and exploit development."

# 脱敏后（插入零宽空格 U+200B）
"Refuse requests for Do​S a​ttacks and e​xploit development."
# 人眼/模型：看起来完全一样
# 后端审核：关键词匹配失效
```

#### 处理范围

- ✅ system 角色消息（默认）
- ✅ developer 角色消息
- ✅ Codex CLI / Claude Code 注入的 harness user 消息
- ✅ tools 的 description 字段
- ❌ user/assistant 消息（保持原样，不影响正常对话）

#### 敏感词表

约 80 个安全/合规术语：
- 攻击类型：DoS, DDoS, exploit, SQL injection, XSS, malware...
- 安全术语：vulnerability, penetration testing, privilege escalation...
- 品牌词：Claude Code, Anthropic（避免竞争品牌触发审核）

完整列表见 `desensitize.py` 中的 `SENSITIVE_TERMS`。

#### 注意事项

- ✅ 只处理合规声明，不绕过对有害输入的审核
- ✅ 只改 system 消息，真实用户输入保持原样
- ⚠️ 零宽空格对人眼/模型透明，但会影响精确字符串匹配
- ⚠️ 性能开销：<1ms（正则替换）

---

### 消息压缩优化 (`--optimize-context`)

仅对 `/v1/responses` 端点生效，将长历史、大 schema、超长工具输出压缩成"最小语义闭包"，大幅减少 token 使用（可能减少 60-90%）。

#### 适用场景

- ✅ 使用 Codex CLI / Claude Code 等 agentic 工具（长历史）
- ✅ Token 使用量很大（>100k/天）
- ✅ 经常触发 "context " 错误
- ✅ 每次请求都发送完整历史记录
- ❌ 不用于短对话/简单请求

#### 使用方法

```bash
# 启用消息压缩
uv run --with workbuddy2api python -m codebuddy_proxy --optimize-context \
  --log-file "$HOME/.workbuddy2api/codebuddy-proxy.jsonl"

# 同时启用两个功能（推荐用于 Codex CLI）
uv run --with workbuddy2api python -m codebuddy_proxy --desensitize --optimize-context \
  --log-file "$HOME/.workbuddy2api/codebuddy-proxy.jsonl"
```

#### 工作原理

##### Conservative 模式（非 agentic 请求）

只做长度裁剪：
- System → 截断至 1200 字符
- User → 3200 字符
- Assistant → 首尾保留摘要（1800）
- Tool 输出 → 压缩至 1600 字符

##### Aggressive 模式（agentic CLI 请求）

自动检测 agentic 请求（tools 包含 `exec_command`、`apply_patch` 等，或消息含 harness 标记），重构成最小语义闭包：

1. **丢弃 harness 消息** — 删除所有 Codex/Claude Code 注入的 system/user 消息
2. **保留最近上下文** — 从后往前保留 ≤8 条消息 / ≤7000 字符
3. **历史摘要化** — 更早的历史压缩为规则摘要（每条一行）
4. **Schema 收敛** — 只保留结构字段，删除 description（最占空间）
5. **Tool 输出/参数压缩** — 保留关键部分，其余省略

#### 效果示例

```
原始请求：
  - Messages: 50 条，120,000 字符
  - Tools: 15 个，45,000 字符
  - 总计：~165,000 字符（~40k tokens）

压缩后：
  - Messages: 12 条，18,000 字符
  - Tools: 15 个，8,000 字符
  - 总计：~26,000 字符（~6k tokens）

节省：~85% token
```

#### 日志验证

启用功能后，日志会记录压缩统计：

```bash
grep projection_applied "$HOME/.workbuddy2api/codebuddy-proxy.jsonl" | jq .
```

示例输出：

```json
{
  "event": "projection_applied",
  "protocol": "responses",
  "mode""aggressive",
  "original_messages": 50,
  "projected_messages": 12,
  "original_message_chars": 120000,
  "projected_message_chars": 18000,
  "dropped_harness_messages": 8
}
```

#### 注意事项

- ✅ 只用于 `/v1/responses`，不影响 chat/messages 端点
- ✅ 保留语义闭包，模型仍可推理
- ⚠️ 历史被摘要化，精确细节需重新运行工具获取
- ⚠️ Schema 被裁剪，description 等辅助信息丢失
- ⚠️ 性能开销：<10ms（遍历+压缩）

---

### 日志

日志包含：
- 文本日志：`$HOME/.workbuddy2api/proxy.log`（按天滚动，保留 30 天）
- 结构化日志：`$HOME/.workbuddy2api/codebuddy-proxy.jsonl`（按天滚动，保留 30 天，完整请求/响应）

JSONL 每条记录包含 `app_version`、`system_version`、`python_version` 和 `machine` 字段；启动时还会记录 `startup` 事件。

也可以指定日志文件的绝对路径：

```bash
uv run --with workbuddy2api python -m codebuddy_proxy \
  --desensitize \
  --log-file "$HOME/.workbuddy2api/codebuddy-proxy.jsonl"
```

查看日志：

```bash
# 实时查看
tail -f "$HOME/.workbuddy2api/codebuddy-proxy.jsonl"

# 查看流式事件
tail -100 "$HOME/.workbuddy2api/codebuddy-proxy.jsonl" | jq 'select(.event | startswith("stream"))'

# 统计超时
jq 'select(.event=="stream_timeout")' "$HOME/.workbuddy2api/codebuddy-proxy.jsonl" | wc -l

# 验证脱敏
grep desensitize_applied "$HOME/.workbuddy2api/codebuddy-proxy.jsonl"

# 验证压缩（查看统计数据）
grep projection_applied "$HOME/.workbuddy2api/codebuddy-proxy.jsonl" | jq .
```

### 找不到 session 文件

首次使用需要登录：

```bash
uv run --with workbuddy2api python -m codebuddy_proxy --login \
  --log-file "$HOME/.workbuddy2api/codebuddy-proxy.jsonl"
```

### 401 认证失败

Token 过期，重新登录：

```bash
uv run --with workbuddy2api python -m codebuddy_proxy --login \
  --log-file "$HOME/.workbuddy2api/codebuddy-proxy.jsonl"
```

### 审核拦截

启用脱敏：

```bash
uv run --with workbuddy2api python -m codebuddy_proxy --desensitize \
  --log-file "$HOME/.workbuddy2api/codebuddy-proxy.jsonl"
```

如果仍然被拦截，尝试压缩优化（仅 `/v1/responses`）：

```bash
uv run --with workbuddy2api python -m codebuddy_proxy --desensitize --optimize-context \
  --log-file "$HOME/.workbuddy2api/codebuddy-proxy.jsonl"
```

### 端口被占用

```bash
lsof -i :8787
uv run --with workbuddy2api python -m codebuddy_proxy --port 8788 \
  --log-file "$HOME/.workbuddy2api/codebuddy-proxy.jsonl"
```

### SOCKS proxy 错误

依赖已自动安装 `httpx[socks]`。如果仍有问题，检查环境变量：

```bash
env | grep -i proxy
```

临时禁用代理：

```bash
unset http_proxy https_proxy all_proxy
uv run --with workbuddy2api python -m codebuddy_proxy \
  --log-file "$HOME/.workbuddy2api/codebuddy-proxy.jsonl"
```

## 技术细节

- **架构**: FastAPI + httpx（异步）
- **并发**: 支持 1000+ 并发请求
- **超时**: 连接 10 秒，读取 30 秒
- **流式**: 完整的流式日志（started / progress / completed / timeout）

## 免责声明

**本项目仅供学习和研究使用。请遵守 CodeBuddy 的服务条款。**

- 本项目不提供任何形式的担保
- 使用本项目产生的任何后果由使用者自行承担
- 请勿将本项目用于任何违反 CodeBuddy 服务条款的用途
- 请勿将本项目用于商业用途
