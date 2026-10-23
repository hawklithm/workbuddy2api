# workbuddy2api 异常退出排查 Handoff

> 创建日期：2026-10-23（Windows 本地时区 UTC+8）
>
> 工作目录：`D:\workspace\workbuddy2api`
>
> 状态：**根因尚未确认；进程已多次无退出记录地消失。不要把“没有 Python 异常”直接判定为“确定由外部 kill”。**
>
> 本文是交接记录，不代表已有修复 PR、已部署守护进程或已确认罪魁祸首。

> 续接更新：2026-10-23 09:28 UTC+8 已实现并启动独立只读等待监控，见第九节。前八节保留当时证据；不代表已确认历史退出根因。

## 一、当前任务与目标

用户反馈 workbuddy2api 多次“运行一段时间后自动退出”，希望获得根因和可稳定复现、观察进程死亡原因的方案。当前阶段已完成业务日志、Windows 事件和 Wave Terminal 宿主的只读检查；已开发 Python 进程内诊断能力，但一次新故障发生后仍未捕获退出码。

**下一位 Agent 的核心任务：**

1. 先确认当前代理是否运行，并保全最新现场；如已重启，区分新旧 PID / run_id。
2. 通过**独立于代理本身的父进程监控/进程审计**确定谁先退出、真实退出码是什么、是否收到控制台事件或被 TerminateProcess。
3. 根据新证据再做最小化代码/启动方式修复；不要先入为主改动协议、SSE、鉴权或发版。
4. 如果需要实施新方案，优先独立分支 + 回归测试 + PR，不直接扰动运行中的服务。

## 二、明确的既有证据

所有时间均为 **2026-10-23 UTC+8（Windows 本地）**。

| 时间 | 已观察事件 | 证据/意义 |
| --- | --- | --- |
| 00:26:28 | 旧版代理启动 | `codebuddy-proxy.jsonl` 的 startup |
| 02:35:35 | 最后一次记录的流式请求成功，HTTP 200 | 直到 07:38 没有后续业务事件；此时仅能确认日志中断，无法证明具体退出时刻 |
| 07:38:01 | 手动重启，运行包版本 2.0.6 | `codebuddy-proxy.jsonl` startup |
| 07:56:36 | 旧代理最后一次正常响应 HTTP 200 | 之后没有 Python traceback、stream_failed 等异常 |
| 08:18:30 | 手动重启，已经使用分支代码 **2.0.7** | `process-events.jsonl` 记录 `process_start`，PID **41968**，run_id **71315ab3318246198a16c42f7c296227** |
| 08:52:46 | 收到最后一批客户端请求 | `codebuddy-proxy.jsonl` |
| **08:53:10** | 最后一条业务事件：`stream_completed`，HTTP **200** | request_id **8bd493259c0b436f910a873993a515c4** |
| 08:55 左右 | 检查时 workbuddy2api 的 uv/Python 进程均不存在、`/health` 不可达 | 确认服务已不可用；并非推断自无请求 |
| 09:04 左右 | 再次只读检查，没有发现代理进程；三个诊断文件仍未更新 | **以实际后续检查为准，用户可能随后手动重启** |

重要：02:35、07:56、08:53 是 **最后一次有日志的时间**，不一定是进程死亡的真实时刻；除 08:55 主动检查外，缺少持续健康探测来标定准确时间。

### 08:18:30 这次运行的详细诊断

目录：`C:\Users\marcus\.workbuddy2api\`

- `process-events.jsonl`：文件约 165 字节，**只有一条** `process_start`（pid=41968）。无 `process_exit`、`process_signal_received`、`process_fatal_exception`、`asyncio_unhandled_exception`、`diagnostic_log_failure`。
- `console.log`：约 200 字节，仅记录正常启动文字（Local Models、监听 0.0.0.0:8787、Endpoint 列表），**无 traceback / Uvicorn 异常**。
- `crash.log`：**0 字节**。Python faulthandler 未记录致命错误；不能据此排除所有原生层异常或强制终止。
- `codebuddy-proxy.jsonl`：08:18:30 startup 标示 `app_version=2.0.7`；之后最后一次 `stream_completed` 是 08:53:10、状态 200，无对应失败事件。
- `proxy.log`：需保留完整原始文件以供授权本机进一步比对；先前只读检查未发现对应异常。
- `process-events.jsonl` 未出现退出事件，意味着代码未能执行相应钩子或诊断链路未完成写入。**仅凭此不能断言被外部 kill。**

### 操作系统与启动链路

运行方式（使用 Wave Terminal 中的 PowerShell）：

```powershell
uv run python -m codebuddy_proxy --desensitize --host 0.0.0.0
```

观察到的树形关系（事故之前，数值可能随重启变化）：

```text
Wave.exe
  └─ wavesrv.x64.exe
       └─ powershell.exe  (PID 209484; 2026-10-15 创建)
            └─ uv.exe
                 └─ python.exe (.venv)
                      └─ python.exe (实际运行解释器)
```

- 08:53 故障后，`uv.exe` 及其 Python 子进程均已不存在。
- Wave、wavesrv、对应 PowerShell 宿主**仍存在**，不支持“整个 Wave Terminal 已退出”的简单解释。
- Windows 最近开机时间为 2026-09-23 19:51:33，事故期间未重新开机。
- Windows Application/System 事件日志在 08:40~08:58 未见匹配的 Python/uv APPCRASH、WER 崩溃转储、内存耗尽或整机重启事件。
- 08:43 有 Windows Update/SideBySide 等系统事件，**无因果证据**，不要误判成元凶。
- `C:\ProgramData\Microsoft\Windows\WER\ReportArchive`、`ReportQueue` 和用户 `CrashDumps` 对该时段未发现对应记录。
- Wave 的 `waveapp.log` 在 08:53 前后仍有正常 flush、窗口焦点等事件，没有直接的 Python 退出记录。
- 读取 Security 事件日志 4688/4689 遇到无权限，**不能以此声称 Windows 没有进程终止审计记录**。
- 未找到项目生产源代码里直接调用 `os._exit`、`process.kill` 的明显路径；此为有限文本搜索，不等于完全排除第三方依赖或外部进程。
- 先前曾检查磁盘未耗尽；本事故没有进程退出前的内存趋势或实时采样，**不能排除瞬时资源问题**。

## 三、代码 / PR 状态

仓库：`hawklithm/workbuddy2api`

- 已合入 `main`：PR #15（国内 developer 角色兼容）、PR #16（请求构建/非流式协议格式化重构）。
- 当时远端 `main`：`eca6f70a1af75788949fd397e100c612a1c8e440`。
- 当前本地分支：`fix/process-diagnostics-2.0.7`。
- 当前本地 / 分支提交：`1f4ee45e114408c9d3326b04ddd8e43faa60b235`。
- **PR #17（进程诊断 + 版本 2.0.7）：** https://github.com/hawklithm/workbuddy2api/pull/17
- PR #17 以前确认处于 open、mergeable 状态，**未执行合并 / 2.0.7 Tag / Release**；接手时务必刷新远端状态。
- `pyproject.toml` 已从 2.0.6 更新到 `2.0.7`；FastAPI 的 version 也已设为 `2.0.7`。用户运行的 08:18 进程确认日志版本为 2.0.7，但不能由此断言已合并发布。

PR #17 主要改动：

- `src/codebuddy_proxy/process_diagnostics.py`：ProcessDiagnostics、ConsoleSink/TeeStream、EventJournal、faulthandler、退出原因/内存快照/asyncio/thread/unraisable 异常记录、缺失退出记录检测；
- `src/codebuddy_proxy/__main__.py`：主入口包裹诊断、asyncio 与信号观察、文件日志写入错误报告、版本元数据；
- `test_process_diagnostics.py`：22 项诊断测试；
- `docs/process-diagnostics-2.0.7.md`：诊断能力、路径、轮转、局限；
- `README.md`、`README_zh.md` 和 `pyproject.toml`。

既有测试：**254 passed, 1 skipped, 1 deselected**。排除项是已知 Windows/NTFS 文件权限 `0600` 断言，与这轮异常退出排查不是同一问题。**这些测试不等于已在生产环境复现或解决真实退出问题。**

### 现有诊断的盲区

1. Python 的 `try/finally` / `sys.excepthook` / 信号 handler 在 Windows `TerminateProcess`、`taskkill /F`、解释器直接退出、严重本地层故障等场景可能完全无法执行。
2. `TeeStream` 捕捉的是 Python 层 `sys.stdout/sys.stderr`；C 原生 fd 写入、子进程直接继承 OS handle 的输出可能绕过 Tee。
3. `faulthandler` 不能保证捕获所有 native faults。
4. `previous_run_exit_unobserved` 只有**下一次启动**才有机会生成。当前 08:18 那一轮仍没看到下一次启动事件，因此此事件目前还未出现；启动后即使出现，也只是“未观察到匹配退出”的证据，不会自动定位外部终止者。
5. 当前没有独立的子进程退出码记录、Windows 进程审计或持续端口探测。因此仍不知道 `uv` 和 Python **谁先退出、实际退出码是什么、退出时间是多少**。
6. 诊断日志可能包含客户请求/凭证等敏感数据；不要在公开 Issue 或 PR 中贴原始 `console.log`、`proxy.log`、完整 JSONL。

## 四、对根因的判断（分层，不要过度推断）

**确认：** 三次运行的最后业务请求均成功，至少 08:18~08:53 那轮的代理进程在 Python 内部**没有留下可识别的正常/异常退出事件**，随后确实失去服务。

**倾向排查、尚未确认：**

- 交互式终端或外部进程对子进程发送控制事件 / `TerminateProcess`；
- `uv run` 进程链路先退出，Python 随之被终止，或反之；
- Python/native 运行时意外终止、底层扩展故障；
- Windows 安全软件、资源保护或其他本地自动化脚本介入。

**目前没有证据：** 将原因归因于最后的 HTTP 200、角色兼容重构、PR #16、Windows 更新、Wave 窗口失焦、服务器主动关闭或上游 CodeBuddy 故障。

## 五、推荐后续实施顺序

### P0：留存现场和独立监控（最重要）

在**不重启、不 kill 现存代理**的前提下，建立与代理不同进程的轻量监控器，记录时间戳、每秒或数秒采样的 `uv` / Python PID 与 PPID、创建时间、工作集/私有内存、健康检查状态。以独立文件写入，避免伴随目标一起丢失日志。

如果需要捕获**真实退出码**，最好由独立父进程启动并 `WaitForExit` / 等待句柄，或使用受控服务管理器。仅通过外部枚举 PID 不能获取已经结束的任意进程退出码；请明确设计这一点。

**请先保存已有日志副本再切换启动方式**；用户目前使用 `uv run python -m codebuddy_proxy --desensitize --host 0.0.0.0`，切换启动方式属于行为变更，必须单独验证和说明。

### P1：Windows 退出链路取证

- 有管理员权限时检查并按需开启 Windows Security 4688 / 4689 进程创建/退出审计；确认是否已启用、是否能拿到退出状态。
- 评估 Sysmon Event ID 1（创建）/ 5（终止）等事件能否满足要求；**Sysmon 的事件不保证直接给出“是谁 kill 了谁”**，如需终止调用方证据，考虑 EDR、ETW、调试器或 Process Monitor 的适用事件/栈，并权衡采样开销。
- 检查 Wave Terminal 对子进程退出和 Windows 控制台信号传递的管理行为；不要直接将 Wave 的 flush/focus 日志当作进程退出证据。
- 评估 Windows Error Reporting / LocalDumps 或 ProcDump 的崩溃转储方案，注意保密、磁盘空间和性能。

### P2：诊断方法质量与服务稳定性

- 评审 PR #17 的 SIGTERM / Uvicorn 回放信号兼容路径，避免监控代码本身修改原有退出语义；
- 对新的外部监控器做人工隔离测试：正常退出、异常退出、`taskkill /F`、单独终止 `uv`、单独终止 Python、关闭终端；**仅对临时测试进程操作，绝不对用户运行中的服务进行 kill**；
- 只有拿到退出码/关键事件后才设计真正的修复；自动重启若要加，应在可靠采集异常后再启用，避免覆盖现场；
- 独立增加探活告警可以缩小服务失效时间窗口，但不能替代根因证据。

## 六、接手 Agent 的第一轮只读核验

在项目根目录查看 Git 状态：

```bash
git status --short --branch
git log -3 --oneline
```

Windows PowerShell 可先查看服务、进程、文件时间：

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match 'codebuddy_proxy' } |
  Select-Object ProcessId, ParentProcessId, CreationDate, Name, CommandLine

Test-Path "$HOME\.workbuddy2api\process-events.jsonl"
Get-Content "$HOME\.workbuddy2api\process-events.jsonl" -Tail 30
Get-Content "$HOME\.workbuddy2api\console.log" -Tail 80
Get-Item "$HOME\.workbuddy2api\crash.log"
```

如服务存在：

```powershell
Invoke-RestMethod "http://127.0.0.1:8787/health"
```

如重新启动过，应检查新的 `process_start` 后有无上一轮的 `previous_run_exit_unobserved`，并关联该条目的 previous_pid / previous_run_id；保留新旧 PID 记录。

**注意：本机默认服务监听 `0.0.0.0:8787`，对不可信网络暴露有安全风险；本次排查不修改服务监听范围。**

## 七、验收标准与交付期望

交接后续不是要求简单复述“外部 kill 的可能性”，而是产出：

1. **进程级事件时间线**：进程 A/进程 B 谁先终止，准确时间，退出码（如果可取得）；
2. **与业务日志关联**：最后一个 HTTP 请求何时完成，有无仍在处理中的请求，进程是否在空闲阶段退出；
3. **复现结果**：在受控临时进程上模拟可疑退出路径，说明能否由监控器观测；
4. **根因结论**：分“已证实 / 高概率 / 未排除”，并附对应日志证据；
5. **如需要代码修改**：新分支、独立测试、PR 和操作/恢复文档；不得静默重启用户真实服务。

## 八、当前交接结论

> 2.0.7 进程内诊断已在实际运行中生效，但最新一轮故障的 `process-events.jsonl` 仅有启动记录，`console.log` 没有 traceback，`crash.log` 为空；进程却已不可用。现有证据不能确认外部 kill。**下一步核心是用独立的进程监督和 Windows 级取证拿到终止过程与退出码。**

文档创建不会提交、合并、发布、停止或重启真实 workbuddy2api 服务。

## 九、2026-10-23 外部监控实施续接

### 最新现场与保全

- 09:13 左右只读核验：没有运行中的 uv/Python 代理，原日志仍止于 08:53:10；未取得新的历史退出码。没有启动、停止、重启或强制终止真实代理。
- 原始五个当前诊断文件已复制至本机 `C:\Users\marcus\.workbuddy2api\evidence\20261023-091513`，计算 SHA256；不上传原始日志。原文件未改。
- 刷新远端后 main 仍为 `eca6f70a1af75788949fd397e100c612a1c8e440`，PR #17 仍 open；本变更直接基于最新 main，不引入 #17 代码或版本升级。
- 原 checkout 保留 `fix/process-diagnostics-2.0.7`；独立 worktree 为 `D:\workspace\workbuddy2api-external-monitor`，分支 `fix/external-process-monitor`。最初创建的 `codex/external-process-monitor` 因远端已有 `codex` 分支而发生 Git directory/file 命名冲突，仅重命名本轮分支，未修改已有远端分支。

### 已实现

- 提交 PR：[#18 — independently capture Windows uv/Python process exit evidence](https://github.com/hawklithm/workbuddy2api/pull/18)，目标 main；尚未合并或发布。本次仅部署只读等待监控，未切换真实代理启动方式。

- `scripts/watch_processes.py`：独立进程、标准库 Win32 句柄取证；支持附加 PID、等待已有/将来的 `-m codebuddy_proxy`、显式启动新进程三种模式。
- 保留 uv/Python/后代句柄，以 `WaitForSingleObject` 确认终止，再读真实 DWORD 退出码和 `GetProcessTimes` 的创建/退出时间。记录 PID/PPID、创建身份、内存、探活、业务日志白名单尾部摘要；不根据写入顺序猜死亡顺序。
- `scripts/collect_windows_events.ps1`：后台查询 System/Application 错误/警告，以及可读 Security 4688/4689、Sysmon 1/5；按目标 PID 过滤审计事件，60 秒重叠，频道/record_id 可去重，记录截断、无权限、频道不存在和查询失败。不变更系统审计设置。
- 所有采样和退出记录独立写到 `.workbuddy2api\monitor\独立运行目录`，JSONL 轮转 8 MiB × 5；显式启动模式将原生 stdout/stderr 重定向到独立文件（这两份不轮转，须管理磁盘）。不自动重启，不申请目标终止权限，不安装服务/计划任务。
- 操作、恢复和边界详见 [external-process-monitor.md](external-process-monitor.md)，README 已补充入口。

### 当前已运行的仅观察实例

- 09:27:25 UTC+8 用无控制台 `pythonw.exe --watch-proxy` 启动，venv launcher PID **64468**，实际解释器/monitor_pid **246352**。这是监控器，不是代理。
- 输出：`C:\Users\marcus\.workbuddy2api\monitor\20261023-092726-fa6a27ab\events.jsonl`。
- 已核对 `monitor_start`、连续 `sample`/`health` 和 `windows_events`；当前目标列表为空、探活失败，与代理未运行一致。等待用户以后自行启动代理后附加，未切换其启动命令。
- 本机 System/Application 可读，Security 无权限，Sysmon Operational 不存在，已写入明确 unavailable，不能视作无退出事件。无新增审计策略/软件安装。
- 该实例非开机自启；不要删除其脚本 worktree。后续关机、注销、Job Object 或监控自身异常可能终止它，应检查心跳。未实测 Wave 关窗部署边界。

### 回归与 Review

- 新增 **20 项**测试全部通过（包含在全量回归中）：正常退出、未捕获异常、`os._exit`、立即退出、259、指定高位状态、强制终止、uv/Python 分别终止及退出顺序、停止监控后目标继续运行、等待/自动发现、身份校验、轮转、字段脱敏、系统频道状态。
- 所有强制终止只针对测试创建的精确对象/句柄；没有按名称批量 kill，也没有针对真实服务。高位状态由测试显式 ExitProcess 指定，不能称为真实 native crash 复现。
- 完整回归首次：**251 passed、1 skipped、1 failed**，唯一失败是原 `test_session_profiles_are_isolated_and_saved_with_metadata` 的 Windows 0600 权限断言。之后新增立即退出测试，并在最终代码上运行：**252 passed、1 skipped、1 deselected**；排除的仍是该原有断言，未修改它。
- 本轮实际 diff Review 已完成：检查只读句柄权限、259 判定、PID 重用、父子创建/退出时间、句柄释放、后台查询不阻塞、事件窗口/过滤/截断、白名单脱敏、轮转和监控退出语义。Review 后补上创建时间二次校验、父进程退出时间边界、事件查询重叠及小批量 XPath。
- 未改变代理内部的信号处理、协议、SSE、认证、版本或监听地址；不宣称已完成真实 Wave 关窗、断电/注销、原生崩溃或生产异常退出验收。

### 下一次故障的行动

先保留监控目录（含 `.1`~`.4`）、完整业务/进程内日志，再按 `(pid, created_ticks)` 关联并用 `exit_ticks` 比较 uv/Python 谁先退出。用 exit_code_hex、Windows 事件、探活边界和业务 request_id 交叉核对；业务尾部摘要不是完整在途请求清单。

等待模式有约 5 秒加 CIM 延迟的发现窗口，短命进程可能漏采；手动附加也不能恢复先前已死亡进程退出码。需要更强根进程覆盖时，由维护者在下次受控启动选择 `--launch`，不要为切换监控而现在重启真实服务。

**根因分层：历史服务消失且缺失内部退出事件已证实；真正退出机制、终止调用者、是否 native/资源问题仍未排除。退出码可缩小范围，不足以单独证明外部 kill。**

## 十、19:41:31 再次退出：已取得真实状态及隔离复现

用户 19:27 重新启动代理，确认故障时没有操作终端。监控记录：venv launcher 84896 于 **19:41:31.066082** 先退出、状态 **FFFFFFFF**；实际解释器 236188 于 **19:41:31.070364** 退出、状态 **0**；uv 189208 于 **19:41:31.070937** 退出、状态 **FFFFFFFF**。以上为 UTC+8，按 Windows exit_ticks 排序，不按事件行顺序。

本轮业务日志 5 个 stream_started 均有 HTTP 200 完成记录，最后 19:41:29；19:41:30.533 最近健康探测仍成功。Python 内部仍无本轮退出事件或 traceback，crash.log 空。原 PowerShell/Wave/wavesrv 仍在。System/Application 无已取得的匹配崩溃警告/错误，Security/Sysmon 缺口不变。内存采样未见持续增长，不能排除采样间隔内瞬时问题。

用相同 uv/venv 运行独立临时等待任务，以 readiness 文件验证工作解释器及其父 launcher 后，只强制终止该临时 launcher，复现 **launcher FFFFFFFF → interpreter 0 → uv FFFFFFFF**；直接强制终止临时解释器则三层均 FFFFFFFF，组合不同。未操作真实服务。最初探索性枚举误收 uv 探测进程，已改为 readiness 精确识别，并仅使用最终复现结果。

强线索是 launcher 层异常终止后连带实际解释器，尚无原事故终止调用方或 Job/控制台事件证据；不能认定 Wave、安全软件或外部 kill 已证实。code=0 也不能解释成实际解释器正常收尾。下一步重点取得 launcher 终止来源，而非先改业务协议。

已保全六份代理/监控日志、Wave 日志、SHA256、完整流式请求配对和复现脚本/结果于本机 `C:\Users\marcus\.workbuddy2api\evidence\20261023-194131`，详见该目录 `ANALYSIS.md`；敏感原日志不上传。本次没有启动、停止或重启真实代理。监控器继续等待。

## 十一、Silent Process Exit 已验证及部署（20:25:58 UTC+8）

用户授权实施下一层取证后，经 Windows UAC 运行了隔离验证。为避免全机 python.exe 影响，生产配置仅针对原 venv 内新增的 **workbuddy2api-evidence.exe**；它与原 python.exe 字节及 SHA256 完全相同，沿用同一 pyvenv.cfg 和 site-packages。原 python.exe 的 IFEO/SilentProcessExit 均未配置。完整路径过滤试验未拿到报告，未用于生产；临时设置已清理。

最终验证拿到 self-exit 3000、cross-process 3001，后者明确指向本次测试 PowerShell，且生成临时目标与发起者两方 mini dump（含线程栈/上下文）。所有 Kill 仅对测试创建的精确 Process 对象；没有启动、停止或重启真实代理。原独立监控继续运行，采集助手已补充 ProcessExitMonitor 信息级事件并核实可读。

- 管理/回滚脚本：`scripts/manage_silent_exit.ps1`，有每值原始状态备份、冲突保护和失败回滚；现有静默退出配置会拒绝覆盖。
- 成功状态：`C:\Users\marcus\.workbuddy2api\silent-exit\20261023-causal-exit-04\result.json`，status=enabled_for_future_starts。转储在同目录 dumps 的事故子目录；仅当前用户/SYSTEM/Administrators 访问。
- 参数：GlobalFlag 0x200、ReportingMode 2、IgnoreSelfExits 0、DumpType 0（mini）、MaxNumberOfDumpFiles 8；没有 Debugger、MonitorProcess 回调、自启或自动重启设置。
- 启动前检查已返回 ready；用专用映像运行的临时 import 测试确认 sys.prefix 为原 `.venv`、FastAPI 来自原 site-packages。此正常退出测试可能留下自退出报告/转储，不能当作真实代理事故。
- 前期试验暴露了 PowerShell 原子备份、JSON 数组反序列化和 Windows 转储子目录检查问题，已修正；失败状态目录保留但不作为已启用实例。当前临时测试 IFEO/SPE 键均不存在。

**下次运行需使用新入口：**

```powershell
& 'D:\workspace\workbuddy2api-external-monitor\scripts\start_proxy_with_evidence.ps1'
```

它先校验配置和哈希，拒绝重复启动现有代理，然后由外部监控父进程启动专用 launcher。原生 stdout/stderr 保存在显示的取证目录，不实时回显。此方式不经过 uv、无交互 stdin；更名/父进程/控制台都是隔离变量，不能据稳定性直接认定根因。

旧 `uv run python -m codebuddy_proxy ...` 仍可运行，但只有上一层退出码监控，**不会启用专用映像的静默退出来源取证**。完整操作/回滚/边界见 [silent-process-exit.md](silent-process-exit.md)。此机制不覆盖所有内核/Job/最后线程退出路径，也不证明过去故障的发起者。

最终回归：**257 passed、1 skipped、1 deselected**，排除的仍是原 Windows 0600 断言。新增 5 个配置/回滚单元测试只用随机 HKCU 测试键；管理员实机验证另行记录。原自动发现测试只有 3 秒观察窗口，短于 CIM 查询 10 秒超时，负载下先结束；将该临时观察窗口修正为 12 秒后回归通过，没有改变真实代理或监控器的运行逻辑。代码 Review 完成，并核实生产两处 python.exe 注册表键未创建、临时 probe 配置已清理、专用映像配置可读、启动前检查 ready。

## 十二、20:38:18 再次退出：已取证，实际解释器来源仍有缺口

用户确认使用新入口后再次自动退出，对当时是否有其他 AI/清理脚本运行表示不确定。本轮只读调查进程及事件、离线解析保存的 dump；没有启动、停止、重启代理，也没有强制终止测试或更改系统取证配置。

持久 pythonw 监控保存的 Windows exit_ticks 表明：实际代理 Python PID 33180 在 20:38:18.253396 最先结束；启动监控实际 Python PID 39968 在 .290291 结束；其 venv launcher PID 123156 在 .297212 结束；专用代理 launcher PID 124092 在 .854554 结束。四者退出码均为 FFFFFFFF。原 pythonw 监控仍存活；新启动监控自身未能写出退出事件。

Windows ProcessExitMonitor 3000（RecordId 146658）及 16601 字节、单线程的转储只对应专用 launcher PID 124092。离线栈包含 RtlExitUserProcess，支持它随后自行退出；不对应最先结束的实际解释器，不能识别后者的终止来源。没有本轮 3001。20:25 的跨进程 3001 属于隔离测试，不能当成本轮证据。

上一版配置只覆盖专用 launcher，实际运行 Python 仍是共享 Anaconda python.exe，未启用退出来源取证。这是已确认的覆盖缺口。多 Python 进程在约 37 ms 内结束而 pythonw 存活，是排查外部清理/终止行为的线索，不足以认定按名批量终止或归因某个客户端。19:41 的 launcher 先退出顺序也不能套用于本轮。

完整本机证据及分析：C:\Users\marcus\.workbuddy2api\evidence\20261023-203818\ANALYSIS.md。原始日志和 dump 仅保留本机，不上传公开 PR。下一阶段需设计实际解释器及监控进程的来源取证，明确共享 python.exe 配置的影响、限量和回滚后实施；保留独立 pythonw 观察器。历史及本轮根因均未确认。

## 十三、21:02 再次退出及实际解释器取证补齐

21:02:24.833883 启动监控实际解释器 PID 170704 最先退出，venv 父 PID 201900 于 .840553 退出；代理实际解释器 PID 258404 于 .912141 退出；专用 launcher PID 159076 于 21:02:25.333252 退出，均 FFFFFFFF。原独立 pythonw 保存了时间线，Application 3000 RecordId 146661 仍只对应旧 launcher。其他项目原先存在的 Python 进程在后续快照中也不见，但缺少它们的精确退出时间/来源，不能据此认定同一原因。本轮证据保存在 C:\Users\marcus\.workbuddy2api\evidence\20261023-210225。

已改为专用真实 CPython 映像运行，覆盖代理 runtime 和无控制台 observer，保留原 pythonw。bootstrap 使用原依赖和源码；sys.prefix 改为诊断 runtime，映像名称/父进程/控制台也是变量。没有修改全机共享 python.exe/pythonw.exe 的 IFEO/SPE。旧启动命令保持，但入口现在拒绝 launcher-only 状态，并自动选择已验证的 native runtime。

有效 state：C:\Users\marcus\.workbuddy2api\silent-exit\20261023-native-runtime-04，21:11:10 enabled。临时 normal PID 136100、abrupt self PID 233756、cross termination PID 19876；已知临时发起者是管理员测试 PowerShell PID 246860。已取得对应 3000/3001、双方 mini dump，生产两映像的临时 import/identity PID 142044、193460 均验证同进程运行且取得 3000。所有强制终止只针对独立创建的测试 Process 对象；未启动、停止或重启真实代理。

额外常驻观察器 PID 36748 于 21:12:24 启动，不启动代理；日志 C:\Users\marcus\.workbuddy2api\monitor\20261023-211224-5ed62f15。原 pythonw 64468/246352 保持运行；没有开机自启或自动重启。状态应现场重查。

完整回归 267 passed、1 skipped、1 deselected，排除原 Windows 0600 权限断言。Review 修正了最小 PATH 下缺少 Anaconda zlib 的初始化失败、跨 PowerShell 版本 Get-FileHash 模块解析、旧监控命令被误识别为代理、源运行时变化拒绝及已有 IFEO filter/debugger 冲突保护。退出测试事件按实际映像路径和退出码筛选，转储检查目标/发起者精确 PID；不得混入真实事故。配置、启动、回滚与限制详见 docs/silent-process-exit.md。

结论仍是根因未知；这轮交付补齐实际解释器取证，不宣称业务故障已修复，也不保证覆盖所有内核/Job 退出路径。原始日志/转储仅保留本机，PR 不包含原始内存或业务日志。

## 十四、用户手动启动验收与输出修正（21:18–21:22）

用户于 21:18:39 通过入口启动，本轮 observer PID 92096、实际 runtime PID 120080，健康日志至 21:22 连续 HTTP 200，说明新入口已实际启动代理且探活成功。日志目录 C:\Users\marcus\.workbuddy2api\monitor\20261023-211839-daa868e0。启动结果 health_verified=false 仅表示启动脚本本身未等待健康验收。

PowerShell 5.1 的 Get-Content 字符串包含 PSPath/PSDrive 等扩展属性，直接送进 ConvertTo-Json 导致 monitor_output 变成庞大对象。已在启动输出处转换并 Trim 成普通字符串，使用当前打开的重定向日志实测序列化只含目录。保持 Get-Content 的共享读取方式；不使用可能与重定向写入冲突的 File.ReadAllText。此次未重启任何进程。

21:21 快照中此前 64468/246352 的 pythonw 及额外 observer 36748 已不在，36748 最后日志为 21:18:17，未取得其确切退出来源。不能继续声称原两套常驻观察器仍存活；当前确认存活的是用户此次启动的 observer 92096 与代理 120080。此状态变动需保留为后续独立调查项。
