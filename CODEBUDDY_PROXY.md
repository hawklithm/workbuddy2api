# CodeBuddy 本地请求转发代理

代理使用 VSIX 中的 `external-link-v2` 登录流程：启动时可打开浏览器完成 SSO，轮询 `/v2/plugin/auth/token`，查询 `/v2/plugin/login/account`，并将 session 保存到 `~/.codebuddy-session.json`。后续请求在 token 即将过期时自动刷新。

本轮只完成代码和本地验证，没有向 CodeBuddy 后端发起请求。

## 启动

```bash
python3 codebuddy_proxy.py --login
```

使用已记录的真实响应进行离线 mock 测试，不会访问 CodeBuddy 后端：

```bash
python3 codebuddy_proxy.py --mock-dir fixtures/codebuddy-real
python3 -m unittest -v test_codebuddy_proxy_mock.py
```

如果已有 session，只需：

```bash
python3 codebuddy_proxy.py
```

默认监听 `127.0.0.1:8787`。可用 `--host`、`--port`、`--endpoint` 或对应环境变量调整。

## 兼容接口

- `GET /v1/models`：转发到插件使用的 `/v2/config`，读取产品配置中的 `models` 并映射为 OpenAI 模型列表。
- `POST /v1/chat/completions`：转发 CodeBuddy `/v2/chat/completions`，保留 `tools`、`tool_choice`、`tool_calls` 和流式 SSE。
- `POST /v1/responses`：将 Responses 输入转换为 chat messages，非流式返回 Responses 风格结果，流式返回 `response.output_text.delta` 事件。
- `POST /v1/messages`：将 Anthropic messages 转换为 chat messages，返回 Anthropic 风格结果；流式返回文本 delta。
- `GET /health`：仅检查本地代理状态和 session 是否存在，不请求后端。

## 模型列表分析

插件源码中 `ModelManagerImpl.getModels()` 实际返回 `ProductManager.waitConfiguration()` 得到的 `configuration.models`。`ProductEndpointHttpInterceptor` 对 `/v2/config` 设置产品 endpoint，因此代理采用 `/v2/config` 作为模型配置来源，而不是猜测不存在的 `/v2/models`。

## 示例

```bash
curl http://127.0.0.1:8787/health
curl http://127.0.0.1:8787/v1/models
curl http://127.0.0.1:8787/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"default","messages":[{"role":"user","content":"hello"}],"stream":true}'
```

代理只监听本机地址，不应直接暴露到公网。session 文件含有 refresh token，请勿提交到 Git 或发送给他人。
