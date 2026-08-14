# 真实后端响应 fixture

使用 `record_codebuddy_real_fixtures.py` 绕过本地代理，直接调用 CodeBuddy 后端并记录原始响应：

```bash
python3 record_codebuddy_real_fixtures.py
```

默认生成：

- `fixtures/codebuddy-real/models.v3-config.json`：`GET /v3/config` 完整响应
- `fixtures/codebuddy-real/chat-hi.sse.json`：`POST /v2/chat/completions`，消息为 `hi`，完整 SSE 响应

fixture 同时保存 UTF-8 文本和 Base64 原始 body，避免 SSE 或非 UTF-8 内容被破坏。认证头、refresh token、Cookie 等敏感值会被脱敏；响应中的模型和 LLM 内容保持原样。

脚本只执行两个数据请求，不经过 `codebuddy_proxy.py`，也不调用本地 `/v1/*` 接口。
