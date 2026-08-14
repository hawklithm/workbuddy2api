# CodeBuddy client demo

这是根据 `coding-copilot-latest.vsix` 中 `external-link-v2` 认证流程整理的最小 Python 客户端，不依赖第三方包。

## 使用

```bash
python3 codebuddy_client_demo.py "请介绍一下这个项目"
```

首次运行会请求登录状态、打开 CodeBuddy SSO 页面，并轮询登录结果。session 默认保存在：

```text
~/.codebuddy-session.json
```

该文件创建为 `0600` 权限。后续运行会优先复用 session；存在 `refreshToken` 且 access token 过期时，会调用：

```text
POST /v2/plugin/auth/token/refresh
```

强制重新登录：

```bash
python3 codebuddy_client_demo.py --login "测试消息"
```

切换 staging：

```bash
CODEBUDDY_ENDPOINT=https://staging-copilot.tencent.com \
python3 codebuddy_client_demo.py "测试消息"
```

## 注意

- 这是协议演示，不是官方 SDK。
- `model=default` 是扩展中的默认模型标识；如果服务端要求具体模型，可使用 `--model` 或 `CODEBUDDY_MODEL` 修改。
- 不要把 `~/.codebuddy-session.json`、access token 或 refresh token 提交到 Git。
- demo 没有使用 VSIX 内 `.env` 中的任何密钥。
