# 账号管理改造验收记录

日期：2026-10-24。基线：`origin/main` / `83f60e90eccf5495356b97cb2def79533a38d9a5`。
分支：`codex/account-session-manager`。方案见 `ACCOUNT_MANAGER_PLAN.md`。

## 实现与委派

三个子 agent 均按用户指定使用 GPT-6 Luna / max，并在共享分支中按文件边界并行实现，没有代替用户提交或推送。

| 负责人 | 交付 | 交回验证 |
| --- | --- | --- |
| A | 独立账号库、两步登录、原子持久化、刷新与重新关联的并发保护、失败回滚 | 最终定向组 31 passed |
| B | 后台登录任务、取消和关闭、容量限制、仅本机管理 API、响应字段过滤 | 12 passed |
| C | 管理页面、双语文档、包资源、页面控制器模拟 | 页面测试 1 passed；wheel 构建及页面内容比对通过 |
| 主 agent | 启动迁移和选择规则、请求入口账号快照、三协议和 billing 集成、独立审阅 | 集成测试 15 passed；全量与打包结果见下 |

## 已确认行为

- `/admin` 与代理共用进程和端口；未关联账号也可启动，管理/模型查询可用，推理返回结构化 401。
- 国内与 Global 分区，身份包含区域、endpoint 和 uid；不同身份保存到独立文件。索引只包含展示字段，凭据留在每账号 session。
- 页面支持选择区域、打开独立认证窗口、查询登录进度、取消与重试、关联、切换及删除非当前账号。首次关联自动激活，后续关联保留当前选择。
- 继续复用原有 state → token → account 协议和认证头。只有同时得到 token 和含 uid 的账户才写入；超时、取消不提交正式账号。
- 管理请求校验实际 loopback 对端、Host、Origin 和端口；写操作要求专用请求头；管理响应 no-store，账号名以文本渲染。
- 默认恢复上次 active。显式区域/endpoint/session-file 选择匹配账号；无匹配可空账号启动。默认旧会话首次迁移且源文件保留，删除后不自动重新导入；显式 session-file 可重新关联已删除身份。
- CLI `--login` 在临时 session 中认证后导入，不会用另一个账号覆盖旧会话文件。
- 账号在请求进入时、读取请求体之前固定。切换只影响后续请求；旧流、请求重试和 billing 401 刷新不会跨账号或跨 endpoint。
- 单账号并发认证只刷新一次；旧刷新不能覆盖重新关联的凭据。删除后在途刷新不能重建已删除凭据文件。索引写入失败会回滚磁盘和缓存，回滚期间禁止其他 session writer 进入。

## 验证与证据

自动化使用模拟上游和临时账号目录，不调用真实 CodeBuddy 认证或推理。

```powershell
$env:PYTHONPATH='src'
$env:PYTHONUTF8='1'
.\.venv\Scripts\python.exe -m pytest -q --tb=short
uv build --wheel
git -c core.whitespace=cr-at-eol diff --check
```

集成阶段一次全量为 **324 passed / 1 skipped**。追加并发、回滚、页面和请求入口回归后的全量为 **332 passed / 2 failed / 1 skipped**；两项失败均来自未改动的 Windows 进程监控测试：CIM 查询超过 10 秒，以及退出时间与最后采样时间的顺序断言。

原始主干的独立 checkout 已复现 CIM 超时；退出时间测试在该 checkout 的单独复验通过。账号改造没有修改 `scripts/watch_processes.py` 或 `test_external_process_monitor.py`。此外修正了原有跨平台权限测试：只在 POSIX 断言 0600，Windows 验证可读写，不把 chmod 当作 Windows ACL 验收。

最终无并行构建复验为 **333 passed / 1 failed / 1 skipped**，仅剩同一项已在原始主干复现的 CIM 查询超时（`test_watch_discovers_real_temporary_module`）；退出时间断言本轮通过。没有通过删除或跳过测试来掩盖这项失败，也没有扩展修改进程监控模块。

新增的六个账号相关测试文件单独复验全部通过，共 **44 passed**：`test_account_store.py`、`test_client_login_management.py`、`test_account_manager.py`、`test_admin_api.py`、`test_admin_ui.py`、`test_account_integration.py`。

`uv build --wheel` 成功；wheel 包含 account_store/account_manager/admin_api 和 admin.html，页面内容与源码逐字节一致。保留既有 2.0.7 版本号，未发布安装包。

Edge 浏览器实际检查通过：页面加载、切换 Global、选择 Global 和国内区域、弹出独立窗口、模拟关联成功、账号列表刷新且当前选择保持。修复并重新检查成功态隐藏重试/取消按钮。浏览器验收服务器使用独立临时目录和合成账号，模拟登录工厂只访问本机，不修改用户真实账号。验收后停止该临时服务。

截图：`F:/Codex/visualizations/2026/10/24/01a16c22-2245-7651-89a9-06eed118ee27/account-manager-preview.png`。

## 429 限流与自动切换补充验收

新增默认关闭的自动切换开关、每账号冷却状态与管理页倒计时，独立运行状态文件保存开关和冷却截止时间。Retry-After 支持秒数、HTTP 日期、零秒；缺失或无效时使用默认 60 秒。重启恢复状态；冷却过期只解除标记，不主动切回。

同一次推理请求可在收到 HTTP 429 且响应尚未开始时，依次尝试同区域且同 endpoint 的其他未冷却账号，每账号最多一次，数量受请求开始时账号数限制。全部限流仍返回最后一次实际 429 与它的原始 Retry-After。401/503 不触发轮换；已开始输出的 SSE 不重放；晚到的旧账号 429 不覆盖手动切换。Billing 仅记录与选择后续账号，不重放签到操作。

主 agent 负责主转发和 billing 集成；GPT-6 Luna / max 子 agent B 实现持久化和管理 API，C 实现页面与文档，A 独立新增代理链路验收。新增代理组 test_rate_limit_proxy.py 为 **34 passed**；manager/API 为 **21 passed**；七个账号相关测试文件合跑 **87 passed**。这些用例全部使用临时账号与模拟上游，不消耗真实账号额度。

Edge 浏览器已检查默认关闭、开关保存，并通过隔离代理的实际本机 HTTP 请求触发：国内演示账号返回模拟 HTTP 429 后，同一区域备用账号自动接手并返回 HTTP 200；页面显示原账号冷却倒计时和备用账号当前状态。修复并复验了前台刷新与后台轮询竞争导致按钮持续禁用的问题；页面倒计时按本机代理的服务器时间校准，使用本地每秒更新，后台可见页 15 秒同步、隐藏页 60 秒同步。设置保存结果不确定时立即 GET 校准服务器状态。最终主 agent 独立全量复验为 **377 passed / 1 skipped**（7 条既有弃用或测试返回值警告）；本轮进程监控用例均通过。主 agent 重建 `dist/workbuddy2api-2.0.7-py3-none-any.whl`，逐字节核对 `__main__.py`、`billing_proxy.py`、`account_manager.py`、`admin_api.py`、`admin.html` 全部与当前源码一致；差异检查通过。截图位于 `F:/Codex/visualizations/2026/10/24/01a16c22-2245-7651-89a9-06eed118ee27/account-rate-limit-preview.png`。临时演示服务已停止，用户实际运行的代理未被替换。

## 每账号签到与后台自动调度补充验收

自动签到采用一个默认关闭、持久化的全局开关，覆盖全部已关联账号：国内与 Global、当前与非当前账号都独立使用自己的凭据，不改变代理的 active 账号。签到缓存只保存在内存；重启后重新向上游查询当天状态。

主 agent 继续将实现分给 GPT-6 Luna / max：A 实现签到服务、定时器与可控时钟测试；B 实现设置迁移、管理 API、签到响应过滤与故障回滚；C 实现每账号查询/签到按钮、开关、状态同步和双语说明。主 agent 独立集成进程生命周期、结构化与控制台日志，新增真实应用链路的模拟上游测试，并审阅最终实现。

- 每个账号可手动查询、手动签到；签到前必查，只有明确未签到才发送动作，动作后再次确认。状态未知、活动未开放或查询失败时不自动签到。
- 按北京时间（UTC+8）每日 00:00 检查所有账号；未签账号各自在当日 00:00–00:10 内随机计划一次复查，仍未签才执行。随机时间以午夜为基准，查询耗时不额外延长窗口。
- 从关闭切为开启、服务启动时已开启，均立即检查全部账号并按需补签。后台调度随代理运行，关闭管理页仍有效。
- 关闭开关取消等待任务，发出签到请求前再次检查开关、日期和账号存在性；已发送的请求不重复。取消后清除页面 busy 状态。手动操作不受自动开关控制。
- 页面自动行为只查询与同步，不自行发起签到 POST，避免绕过后台的随机时间和开关检查。只有手动按钮调用手动签到 API。
- 同账号串行、全局限制并发；明确拒绝后复查确认未签才允许后续重试；不确定结果保留防重复标记。签到遇到 429 只记录该账号冷却，不触发账号切换。
- 查询开始/结果、签到开始/确认/失败、跳过、随机计划均记录安全字段；日志不含 token、会话、原始上游响应。并发设置回调重读当前开关，防止过期通知覆盖最终值。
- 额外修复删除账号时冷却文件写入失败的一致性，并通过两个故障注入用例确认回滚行为。

签到状态适配参考仓库贡献者的实际接口记录：`data.active` 和严格布尔值 `data.today_checked_in`；已签到提示码 10001 仍需要状态接口确认。来源：[PR #7 的接口验证记录](https://github.com/hawklithm/workbuddy2api/pull/7#issuecomment-6210892410)。本次没有使用真实账号调用签到上游。

最终主 agent 独立全量：**413 passed / 1 skipped / 7 warnings**，耗时 118.61 秒；既有 Windows 进程监控用例本轮通过。最终页面稳定后追加 UI/API/应用签到集成组复验：**14 passed**。子 agent 最终签到相关组 **78 passed**，管理/API 组 **27 passed**，UI/API 组 **8 passed**。

主 agent 构建 wheel 成功，逐字节核对 `__main__.py`、`billing_proxy.py`、`account_store.py`、`account_manager.py`、`account_checkin.py`、`admin_api.py`、`admin.html` 全部与源码一致。差异检查通过。

Edge 浏览器使用独立临时目录、三个合成账号、模拟上游完成：初始状态查询；对非当前 Global 账号手动签到且当前国内账号不变；打开全局开关后后台逐账号检查，未签到账号补签、已签到账号跳过；刷新后三个账号均显示已签到，active 保持原值。实际检查 JSON 日志含 enable 检查、跳过、动作和确认事件。截图：`F:/Codex/visualizations/2026/10/24/01a16c22-2245-7651-89a9-06eed118ee27/account-checkin-preview.png`。临时验收服务在验收后停止，未替换用户实际代理。

## 边界

本次完成本地代码和离线/浏览器验收。真实国内与 Global 的账号认证需要实际用户完成登录，尚未作为上游验收；没有替换正在运行的用户代理，没有发布、提交或推送。

构建文件、运行日志和测试缓存保持忽略；所有代码、方案、验收文档均可在当前分支检查。原始工作区在改造前为干净状态。
