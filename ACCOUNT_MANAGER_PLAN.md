# 账号与登录态管理升级方案

基线：2026-10-24，origin/main `83f60e90eccf5495356b97cb2def79533a38d9a5`。
工作分支：`codex/account-session-manager`。

## 目标与用户流程

代理启动时同时提供 `/admin` 管理页；未关联账号也能启动。页面展示账号名、国内 / Global、当前生效状态，允许关联、切换、删除非当前账号。关联时先选择区域，在点击动作中打开独立浏览器窗口，然后跳转到原有登录协议返回的认证地址。代理继续使用原有 state → token → account 轮询协议，成功后保存独立会话；首次关联自动生效，已有账号时关联成功不隐式切换。

同一 uid 在不同区域或不同 endpoint 中是不同身份。同身份重新关联更新自身凭据，其他账号不会覆盖。取消、超时、失败均不落入正式账号库。账号切换持久保存，下一次启动恢复；显式 `--global` / endpoint / session-file 用于选择或导入相符账号，不能把不匹配区域的凭据发往另一地址。

## 数据与模块

1. 新增 `account_store.py`：默认目录 `~/.workbuddy2api/accounts`，可用 `--accounts-dir` 覆盖。版本化索引保存脱敏展示元数据与 active id，每账号独立会话文件。身份由 backend、规范化 endpoint、uid 共同确定；只使用受控 ID 生成路径。原子替换、线程锁、文件最小权限；损坏索引明确报错，绝不静默覆盖。旧会话仅在有效且身份可识别时复制导入，保留源文件。
2. 在 `CodeBuddyClient` 抽出 `begin_login()` 与 `complete_login(challenge, timeout=300, cancel_event=None)`；`login()` 组合复用二者，保留 CLI 行为。等待 token 和 account 都受总超时、取消控制，account 必须含 uid。增加 `ensure_authenticated(..., allow_login=True)`，管理模式请求只允许刷新，不自动打开登录浏览器。会话写入使用原子替换。
3. 新增 `account_manager.py`：持有 AccountStore、当前 client，后台登录任务有 pending / succeeded / failed / cancelled 状态；返回 job id、auth_url、简洁错误，绝不返回 token、完整 session、上游原文异常。限制并发登录并回收临时会话和终态任务；关闭时取消任务。切换一次替换整个 client。
4. 新增 `admin_api.py` 与包内 `admin.html`：同端口服务页面和管理 API，无前端构建依赖。只允许 loopback 访问，并校验 Host / Origin；写操作要求专用请求头，拒绝跨站请求。页面使用 textContent 渲染账号名，管理响应 no-store，不输出凭据。
5. 主入口集成：启动账号库、导入历史 domestic/global 和显式 session；保留 mock 模式。每次代理请求捕获一个 client 快照，已发送和进行中的流继续使用原账号，新请求使用新账号；billing 的 401 refresh 也锁定原 client。模型列表、health 随当前账号更新。无账号时模型/管理可用，推理返回结构化 401。

## 子任务与接口约定

- A（存储和认证，GPT-6 Luna / max）：负责 account_store.py、codebuddy_client_demo.py、对应测试。AccountStore(root)，list_accounts() -> list[dict]（id, backend, endpoint, uid, nickname, active），active_account_id -> str | None，import_client(client, activate=False) -> str，get_client(id) -> CodeBuddyClient，set_active(id) -> CodeBuddyClient，active_client() -> client | None，delete_account(id)。begin_login() -> dict（auth_url, state）；complete_login 接受该 dict，成功写入 client 自己的独立 session，异常为 CodeBuddyError。import_client 不修改源文件，第一次导入自动生效。
- B（后台任务与 API，GPT-6 Luna / max）：负责 account_manager.py、admin_api.py、对应测试。AccountManager(store)，client -> 当前 client | None，snapshot() -> {accounts, active_account_id}，activate(id)，delete(id)，start_login(backend) -> job dict，get_login(id) -> job dict，cancel_login(id)，close()。router 工厂 create_admin_router(get_manager)，管理前缀 `/admin/api`：GET /accounts，POST /accounts/{id}/activate，DELETE /accounts/{id}，POST /logins {backend}，GET/DELETE /logins/{id}；页面 GET /admin（兼容 /admin/）。写操作 `X-Account-Manager: 1`。job: id, backend, status, auth_url, error, account_id；后台准备登录地址，GET job 用于轮询。
- C（页面和用户文档，GPT-6 Luna / max）：负责 admin.html、pyproject.toml 包资源、README.md、README_zh.md。页面只依赖上述协议；先打开窗口，再 start_login 并轮询 auth_url，成功刷新列表，可取消和重试，失败给出清楚状态，弹窗被拦截提供链接；不展示 session 或 token。
- 主 agent：负责 __main__.py / billing_proxy.py 集成、启动兼容、跨模块测试、独立审阅和最终验收。子 agent 不提交，不改别人负责的文件，各自运行必要测试直到通过，并反馈命令、结果和未验证边界。

## 429 限流与自动切换补充（2026-10-24）

管理页按“账号 + 模型”展示独立的限流标记和冷却倒计时，以及默认关闭的“遇 429 自动切换账号”开关。同账号某模型冷却不影响其他模型。开关和冷却截止时间写入独立版本化运行状态文件，不改账号索引或凭据结构。冷却优先读取 Retry-After（整数秒或 HTTP 日期）；缺失或无效时采用 60 秒并注明默认估算。过期解除限流标记，不主动切回旧账号。

三种推理协议在上游真正返回 HTTP 429、客户端响应尚未开始时，可按开关切换同区域且同 endpoint、该请求模型未冷却且有可用凭据的账号，再尝试当前请求。每账号最多一次，尝试次数不超过请求开始时账号数量。候选用尽仍返回实际的 429 与对应 Retry-After。迟到的旧请求只能标记其原始账号，不覆盖期间的手动切换。已开始输出的流不重放。

签到等 billing 操作透传 Retry-After；这类请求没有模型维度，不记录推理模型冷却，也不切换账号或重放业务动作。运行状态沿用原文件路径，版本升级到 v2，按账号/模型嵌套保存。v1 开关继续保留；旧冷却因没有模型信息，在严格校验后不再沿用，不扩展为全账号限制。日志包含实际请求账号与模型。

分工沿用 GPT-6 Luna / max 子 agent：B 负责状态、持久化和 API；C 负责页面、文档、交互测试；A 负责独立代理链路测试；主 agent 实现 HTTP 集成并独立审阅和验收。

## 每账号每日签到与自动调度补充（2026-10-24）

全局自动签到开关控制所有关联账号，不限当前生效账号。列表显示各账号今日状态，提供状态查询与签到按钮；每次使用指定账号自身 client 调用既有 billing 底层接口，不更换当前账号。状态查询/签到复用 POST checkin-activity-status / daily-checkin 和 401 刷新逻辑。根据原贡献 PR #7 的上游记录，以 code=0 且 data.today_checked_in 严格布尔值判断；未知、失败和活动未开放均不得当作未签到直接发动作。

自动签到默认关闭，持久化开关。北京时间（UTC+8）每日 00:00 对启用功能下的所有账号检查；未签到者在该午夜后 0–600 秒内安排独立随机时刻，再查未签到才执行签到。关闭开关取消待执行任务；实际 POST 前重检日期、账号是否仍存在和开关。开关从关变开立即检查并补签；服务启动时开关已开也立即检查并补签。

服务随代理进程启动和停止，不使用 Codex 应用提醒。每账号互斥并限制总并发，防止页面、启动、开关和定时任务重复执行。跨日状态不能当成今天；每个动作和结果、跳过、失败、调度时间记录到现有普通日志、JSONL 和控制台，只记录账号 ID/区域/日期等安全字段。

A 子 agent 负责签到 service/调度及单元测试，B 负责管理器设置迁移与 API，C 负责列表/开关/文档和交互测试；均沿用 GPT-6 Luna / max。主 agent 负责生命周期、日志接入、HTTP 集成测试、独立审阅和最终验收。

## 管理页中英文切换补充（2026-10-24）

管理页顶部提供中文/English 选择，保存到当前浏览器 localStorage；有效保存值优先，其次跟随浏览器语言，存储不可用时仍可切换。页面标题、语言属性、无障碍标签、静态介绍、动态账号/签到/模型冷却状态、设置提示、登录进度和已知后端错误覆盖中英文。账号名、模型 ID、UID 和服务地址保留原值，以文本安全渲染。

语言切换只重新渲染展示，不刷新页面、不额外请求、不变更账号或开关，不取消或重复登录与签到任务；进行中的 busy 状态与操作结果按当前语言显示。页面内部显示明确的北京时间，不因显示语言改变后台调度时区。C 实现页面，A 独立扩充控制器回归，B 只读复核错误映射和并发操作；主 agent 负责文档、独立浏览器/测试/安装包验收。

## 验收

离线自动化覆盖：多账号不覆盖、两区域同 uid 隔离、原有会话导入、重启恢复、损坏数据、原子写入；登录 token/account 延迟、取消、超时及失败不写入；API 不泄漏凭据、跨站和非本机管理请求被拒；无账号启动、切换后 health / models / 三种协议新请求使用新账号、在途请求不串号、billing 刷新不串号。全量现有测试需与基线区分；包构建验证页面被包含。浏览器检查管理页加载、关联窗口、状态和布局。真实国内/国际交互登录依赖用户亲自认证，离线模拟通过不能代替真实上游验收。
