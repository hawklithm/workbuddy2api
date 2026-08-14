# workbuddy2api

一个基于 Python 标准库实现的 CodeBuddy API 兼容代理。它复用了 `coding-copilot-latest.vsix` 中的登录认证流程，并将 CodeBuddy 的底层接口转换为常见的 OpenAI、Responses 和 Anthropic API 格式。

项目同时提供离线 mock 模式，并内置**脱敏处理**和**消息压缩优化**两个高级功能模块，用于缓解审核误拦和大幅降低 token 使用。

## 目录

- [环境要求](#环境要求)
- [快速开始](#快速开始)
- [API 接口](#api-接口)
- [高级功能](#高级功能)
  - [脱敏处理](#脱敏处理---desensitize)
  - [消息压缩优化](#消息压缩优化---optimize-context)
- [离线 mock 模式](#离线-mock-模式)
- [项目结构](#项目结构)
- [安全注意事项](#安全注意事项)
- [故障排除](#故障排除)

---

## 环境要求

- Python 3.10+
- 浏览器（首次登录时使用）
- 运行时无需安装第三方 Python 依赖

---

## 快速开始

### 1. 登录认证

首次使用需要登录 CodeBuddy：

```bash
python3 codebuddy_proxy.py --login
```

浏览器会打开 CodeBuddy 登录页面。登录完成后，认证信息会保存到 `~/.codebuddy-session.json`（权限 `0600`）。

### 2. 启动代理

```bash
# 基础启动
python3 codebuddy_proxy.py

# 启用高级功能（推荐用于 Codex CLI / Claude Code）
python3 codebuddy_proxy.py --desensitize --optimize-context
```

启动成功后，终端会显示：

```text
CodeBuddy proxy listening on http://127.0.0.1:8787
Endpoints: /v1/models /v1/chat/completions /v1/responses /v1/messages /health
```

### 3. 测试连接

```bash
# 健康检查
curl http://127.0.0.1:8787/health

# 查看可用模型
curl http://127.0.0.1:8787/v1/models

# 发送一条消息
curl http://127.0.0.1:8787/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "deepseek-v4-flash",
    "messages": [{"role": "user", "content": "hi"}]
  }'
```

### 配置选项

```bash
# 使用环境变量
export CODEBUDDY_ENDPOINT=https://copilot.tencent.com
export CODEBUDDY_PROXY_HOST=127.0.0.1
export CODEBUDDY_PROXY_PORT=8787

# 或使用命令行参数
python3 codebuddy_proxy.py \
  --host 127.0.0.1 \
  --port 8787 \
  --endpoint https://copilot.tencent.com \
  --session-file ~/.codebuddy-session.json
```

---

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
  "authenticated": true
}
```

### `/v1/models` - 模型列表

```bash
curl http://127.0.0.1:8787/v1/models
```

返回 OpenAI 格式的模型列表，`data[].id` 就是后续请求中的 `model` 值（如 `deepseek-v4-flash`）。

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
    "model": "default",
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
    "model": "default",
    "max_tokens": 256,
    "messages": [{"role": "user", "content": "hi"}]
  }'
```

设置 `"stream": true` 时返回 Anthropic SSE 事件流。

---

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

**案例 1: 对接 Claude Code**

```bash
# 必须启用 --desensitize，否则几乎每次都被拦截
python3 codebuddy_proxy.py --desensitize --port 8787

# 在 Claude Code / CC Switch 中配置
# Base URL: http://127.0.0.1:8787/v1
# API Key: (留空或任意值)
```

**案例 2: 对接 Codex CLI**

```bash
# 同时启用脱敏和消息压缩（最佳配置）
python3 codebuddy_proxy.py --desensitize --optimize-context

# 在 Codex CLI 配置文件中
# base_url: http://127.0.0.1:8787/v1/responses
```

**案例 3: 安全研究对话**

```bash
# 启用脱敏以避免合规术语被误拦
python3 codebuddy_proxy.py --desensitize

# 示例请求
curl http://127.0.0.1:8787/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "default",
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
保持原样，不影响正常对话）

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
- ✅ 经常触发 "contextoo long" 错误
- ✅ 每次请求都发送完整历史记录
- ❌ 不用于短对话/简单请求

#### 使用方法

```bash
# 启用消息压缩
python3 codebuddy_proxy.py --optimize-context

# 同时启用两个功能（推荐用于 Codex CLI）
python3 codebuddy_proxy.py --desensitize --optimize-context
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

1. **丢弃 harness 消息** — 删除所有 Codex/Claude Code 注入的 system/user**保留最近上下文** — 从后往前保留 ≤8 条消息 / ≤7000 字符
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
grep projection_applied logs/codebuddy-proxy.jsonl | jq .
```

示例输出：

```json
{
  "event": "projection_applied",
  "protocol": "responses",
  "mode": "aggressive",
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

## 离线 mock 模式

使用已记录的真实响应启动代理，完全离线运行：

```bash
python3 codebuddy_proxy.py --mock-dir fixtures/codebuddy-real
```

此模式下：

- `/v1/models` 读取 `fixtures/codebuddy-real/models.v3-config.json`
- `/v1/chat/completions`、`/v1/responses`、`/v1/messages` 读取 `fixtures/codebuddy-real/chat-hi.sse.json`
- 不读取本地认证状态，不执行登录或 token 刷新
- 不访问 CodeBuddy 或任何其他后端地址

### 运行测试

```bash
# 运行离线接口测试
python3 -m unittest -v test_codebuddy_proxy_mock.py

# 测试脱敏模块
python3 desensitize.py

# 测试高级功能（mock 模式）
python3 codebuddy_proxy.py --desensitize --optimize-context --mock-dir fixtures/codebuddy-real
```

### 记录真实响应

如果需要更新 mock 数据，确保已有有效登录 session 后运行：

```bash
python3 record_codebuddy_real_fixtures.py
```

---

## 项目结构

```text
核心代理:
  codebuddy_client_demo.py          登录、token 刷新和底层客户端
  codebuddy_proxy.py                本地 API 转发代理（主程序）

基础适配器:
  responses_adapter.py              Responses API → Chat Completions 转换
  anthropic_adapter.py              Anthropic Messages → Chat Completions 转换

高级功能模块（可选）:
  desensitize.py                    脱敏处理（缓解审核误拦）
  responses_projection.py           消息压缩优化（减少 token 使用）

测试与工具:
  test_codebuddy_proxy_mock.py      离线接口测试
  record_codebuddy_real_fixtures.py 记录真实响应
  fixtures/codebuddy-real/          mock 使用的真实响应

文档:
  README.md                         本文档
  ENDPOINTS.md                      端点处理逻辑完整梳理
```

### 模块依赖

两个高级功能模块都是**可选的**，主程序会自动检测：

```python
try:
    from desensitize import desensitize_body
    HAS_DESENSITIZE = True
except ImportError:
    HAS_DESENSITIZE = False  # 功能不可用但不会报错
```

模块文件已包含在项目中，无需额外安装：
```
desensitize.py           (17KB, 485行)
responses_projection.py  (22KB, 690行)
```

---

## 安全注意事项

- ❌ **不要提交** `~/.codebuddy-session.json`，其中包含 refresh token
- ❌ **不要提交** `logs/codebuddy-proxy.jsonl`，其中包含完整 prompt 和模型输出
- ❌ **不要将代理暴露到公网**；默认只监听本机地址 `127.0.0.1`
- ✅ `coding-copilot-latest.vsix` 仅用于本地分析，已在 `.gitignore` 中排除
- ✅ Mock 模式适合开发和测试，不能代表实时模型能力或实时模型列表
- ✅ 脱敏只处理合规声明，不绕过对有害输入的审核

---

## 故障排除

### Q1: 启动时提示 "未找到认证文件"

**A:** 首次使用需要登录：

```bash
python3 codebuddy_proxy.py --login
```

### Q2: 启用 `--desensitiz
**A:** 可能是用户输入触发审核（不被脱敏）。检查日志中的 `desensitize_applied` 事件：

```bash
grep desensitize_applied logs/codebuddy-proxy.jsonl
```

### Q3: 启用 `--optimize-context` 后响应质量下降

**A:** 尝试只用于 agentic 场景。普通对话不需要压缩。

### Q4: 如何验证高级功能生效？

**A:** 查看日志文件，搜索相关事件：

```bash
# 验证脱敏
grep desensitize_applied logs/codebuddy-proxy.jsonl

# 验证压缩（查看统计数据）
grep projection_applied logs/codebuddy-proxy.jsonl | jq .
```

### Q5: Token 过期后无法自动刷新

**A:** 检查 session 文件权限和 refresh token 是否有效：

```bash
ls -l ~/.codebuddy-session.json
# 应该是 -rw------- (0600)

# 如果失效，重新登录
python3 codebuddy_proxy.py --login
```

### Q6: 可以单独使用高级功能模块吗？

**A:** 可以！两个模块都是独立的，可以在其他项目中导入使用：

```python
from desensitize import desensitize_body
from responses_projection import project_responses_chat_body

# 脱敏处理
body = desensitize_body(
    body,
    roles=("system", "developer"),
    desensitize_harness_user=True,
    desensitize_tools=True,
)

# 消息压缩
projected_body, stats = project_responses_chat_body(body)
```

---

## 完整日志

代理默认将完整的请求/响应日志写入 `logs/codebuddy-proxy.jsonl`：

```bash
# 查看最近10条日志
tail -10 logs/codebuddy-proxy.jsonl | jq .

# 自定义日志文件
python3 codebuddy_proxy.py --log-file /tmp/proxy.jsonl
```

**⚠️ 日志包含完整 prompt 和模型输出，请妥善保护。** 认证头、refresh token 和 Cookie 等敏感信息不会写入日志。

---

## 参考资料

- **完整端点处理逻辑**: `ENDPOINTS.md`
- **脱敏词表**: `desensitize.py` 中的 `SENSITIVE_TERMS`
- **压缩参数配置**: `responses_projection.py` 中的常量（`MAX_TAIL_MESSAGES` 等）

---

## 许可证

本项目仅供学习和研究使用。请遵守 CodeBuddy 的服务条款。
