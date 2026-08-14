# workbuddy2api

一个基于 Python 标准库实现的 CodeBuddy API 兼容代理。它复用了 `coding-copilot-latest.vsix` 中的登录认证流程，并将 CodeBuddy 的底层接口转换为常见的 OpenAI、Responses 和 Anthropic API 格式。

项目同时提供离线 mock 模式。mock 模式只读取仓库中的 fixture，**不会登录、刷新 token 或请求任何后端 LLM 接口**。

## 环境要求

- Python 3.10+
- 浏览器（首次登录时使用）
- 运行时无需安装第三方 Python 依赖

## 目录说明

```text
codebuddy_client_demo.py       登录、token 刷新和底层客户端
codebuddy_proxy.py             本地 API 转发代理
record_codebuddy_real_fixtures.py  记录真实后端响应
test_codebuddy_proxy_mock.py   离线接口测试
fixtures/codebuddy-real/       mock 使用的真实响应
```

## 登录认证

首次登录可以运行：

```bash
python3 codebuddy_client_demo.py --login
```

脚本会打开 CodeBuddy 登录页面。登录完成后，认证信息会保存到：

```text
~/.codebuddy-session.json
```

该文件权限为 `0600`，后续运行会复用 session；access token 过期时会自动使用 refresh token 刷新。也可以通过环境变量或参数指定服务地址和 session 文件：

```bash
CODEBUDDY_ENDPOINT=https://copilot.tencent.com \
python3 codebuddy_client_demo.py --session-file /path/to/session.json
```

直接发送一条底层 chat 请求：

```bash
python3 codebuddy_client_demo.py "hi"
```

## 启动真实代理

真实代理模式不要传 `--mock-dir`，请求会经过本地代理转发到 CodeBuddy 后端。

启动时执行浏览器登录：

```bash
python3 codebuddy_proxy.py --login
```

命令启动后，浏览器会打开 CodeBuddy 登录页面。完成登录后，代理会继续启动并监听本地端口。

已有有效 session 时可直接启动：

```bash
python3 codebuddy_proxy.py
```

如果需要手动打开登录地址，可以使用：

```bash
python3 codebuddy_proxy.py --login --no-browser
```

默认监听 `127.0.0.1:8787`。常用参数：

```bash
python3 codebuddy_proxy.py \
  --host 127.0.0.1 \
  --port 8787 \
  --endpoint https://copilot.tencent.com \
  --session-file ~/.codebuddy-session.json
```

也可以使用环境变量 `CODEBUDDY_PROXY_HOST`、`CODEBUDDY_PROXY_PORT`、`CODEBUDDY_ENDPOINT` 配置。

启动成功后，终端会显示：

```text
CodeBuddy proxy listening on http://127.0.0.1:8787
```

停止服务使用 `Ctrl-C`。

## API 接口

所有接口默认不需要在请求中额外携带 token，代理会使用登录产生的本地 session 完成认证。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/health` | 查询本地服务和认证状态 |
| GET | `/v1/models` | 查询 CodeBuddy 模型列表 |
| POST | `/v1/chat/completions` | OpenAI Chat Completions，支持 tools 和流式响应 |
| POST | `/v1/responses` | Responses API，兼容 Codex CLI |
| POST | `/v1/messages` | Anthropic Messages API，兼容 Claude Code / CC Switch |

### 健康检查

```bash
curl http://127.0.0.1:8787/health
```

### 模型列表

```bash
curl http://127.0.0.1:8787/v1/models
```

该接口读取 CodeBuddy 插件使用的 `/v3/config`，并将其中的模型转换为 OpenAI 模型列表格式。
返回的 `data[].id` 就是后续请求中的 `model` 值，例如 `deepseek-v4-flash`。

### OpenAI Chat Completions

非流式请求：

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "deepseek-v4-flash",
    "messages": [{"role": "user", "content": "hi"}]
  }'
```

流式请求、工具调用和 `tool_choice` 会按 OpenAI 格式返回：

```bash
curl -N http://127.0.0.1:8787/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "default",
    "stream": true,
    "messages": [{"role": "user", "content": "hi"}]
  }'
```

### Responses API

用于兼容 Codex CLI 等 Responses API 客户端：

```bash
curl http://127.0.0.1:8787/v1/responses \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "default",
    "input": "hi"
  }'
```

支持 `instructions`、消息形式的 `input`、`tools`、`tool_choice` 以及 `stream`。

### Anthropic Messages API

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

## 完全离线 mock 模式

使用已记录的真实响应启动代理：

```bash
python3 codebuddy_proxy.py --mock-dir fixtures/codebuddy-real
```

此模式下：

- `/v1/models` 读取 `fixtures/codebuddy-real/models.v3-config.json`
- `/v1/chat/completions`、`/v1/responses`、`/v1/messages` 读取 `fixtures/codebuddy-real/chat-hi.sse.json`
- 不读取本地认证状态，不执行登录或 token 刷新
- 不访问 CodeBuddy 或任何其他后端地址

运行离线测试：

```bash
python3 -m unittest -v test_codebuddy_proxy_mock.py
```

测试覆盖 `/health`、`/v1/models`、三种 POST 接口，以及流式和非流式返回。

## 记录真实响应

如果需要更新 mock 数据，确保已有有效登录 session 后运行：

```bash
python3 record_codebuddy_real_fixtures.py
```

脚本只直接调用两次真实后端接口：模型配置接口和内容为 `hi` 的 chat 接口。响应 body 会同时保存为 UTF-8 文本和 Base64 原始数据；认证头、refresh token 和 Cookie 会脱敏。

## 安全注意事项

- 不要提交 `~/.codebuddy-session.json`，其中包含 refresh token。
- 不要将代理暴露到公网；默认只监听本机地址。
- `coding-copilot-latest.vsix` 仅用于本地分析，已在 `.gitignore` 中排除。
- mock 模式适合开发和测试，不能代表实时模型能力或实时模型列表。
