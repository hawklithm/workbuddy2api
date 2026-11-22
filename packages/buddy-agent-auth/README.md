# buddy-agent-auth

多品牌 Agent 账号与认证库，源代码与 workbuddy2api 同仓库、独立发行，不依赖 WorkBuddy2API 或 DailyCheckin。品牌协议按 provider 隔离：

- `buddy_agent_auth.providers.codebuddy`：WorkBuddy/CodeBuddy 账号与 token 管理。
- `buddy_agent_auth.providers.trae`：Trae 原生登录态、凭据解密、凭据文件校验与 token 生命周期。使用 Trae 功能时安装 `buddy-agent-auth[trae]`。

各 provider 实现轻量的 `SessionProvider` 接口，返回品牌自己的会话类型；未来添加新品牌时新增 provider，不把不同平台的 token 或设备协议混为一谈。现有顶层 WorkBuddy 导入保持兼容。Trae 的独立浏览器 OAuth 登录、设备注册、硬件配置和账号设备绑定由 Trae provider 提供；不依赖 DailyCheckin。默认设备数据位于 `~/.traeapi`，可用 `TRAE2API_HOME` 指定。独立登录可调用 `buddy_agent_auth.providers.trae.login.TraeLoginFlow` 或 `run_login`。

```python
from buddy_agent_auth.providers.trae import TraeNativeProvider

identity = TraeNativeProvider(device_id_source="get-device-id").load()
identity.ensure_current()  # 登录态发生变化时抛出异常
```


```python
from buddy_agent_auth import AccountStore

store = AccountStore()  # 默认 ~/.workbuddy2api/accounts，兼容现有账号数据
accounts = store.list_accounts()
client = store.get_client(accounts[0]['id'])
client.ensure_authenticated(open_browser=False, allow_login=False)
headers = client.auth_headers()
```

登录可通过 `CodeBuddyClient.login()` 完成，再通过 `AccountStore.import_client()` 导入；后台操作不应自动启动浏览器。账号目录事务使用跨进程锁，token 刷新使用每个 session 的独立跨进程锁，刷新前重新读取磁盘，保存时检查旧状态。进程中发生重关联或删除时，较早的刷新响应不会覆盖新数据。为兼容仍在运行的旧版客户端，账号目录内的锁文件保留旧名称 `.workbuddy-auth.lock`；账号文件路径和格式不变。

在仓库开发时：`python -m pip install -e packages/buddy-agent-auth`。发行前需先发布该包及 DailyCheckin 的匹配版本；目前没有执行任何公开发布。

### 独立账号登录任务

`buddy_agent_auth.providers.codebuddy.login_jobs.WorkBuddyLoginManager` 为 `AccountStore` 提供有并发上限、可取消的后台登录任务；`buddy_agent_auth.providers.trae.accounts.TraeAccountManager` 管理独立 Trae 账号和设备身份。它们均不依赖 WorkBuddy2API 或 DailyCheckin。Trae 登录流提供 `cancel()` 和 `on_event`（授权地址、交换、账号信息阶段）供宿主接入；宿主不必读取内部事件或解析日志。Trae 账号仍保存在宿主账号目录的 `trae-cn` 子目录中。
