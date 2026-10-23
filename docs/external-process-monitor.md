# Windows 独立进程取证监控

此工具用于回答「uv 和 Python 谁先退出、Windows 记录的退出码/退出时间是什么」。它不判断根因、不自动重启、不导入代理代码；兼容 main 2.0.6 和 PR #17 的 2.0.7。仅依赖 Python 3.10+ 标准库和 Windows PowerShell。

需要进一步捕获终止发起者时，参见[Silent Process Exit 取证和诊断启动命令](silent-process-exit.md)。本机已验证并配置专用诊断映像；旧 `uv run python ...` 不会触发这层 Windows 取证。

## 三种模式

| 模式 | 行为 |
| --- | --- |
| `--watch-proxy` | 每 5 秒后台发现以 `-m codebuddy_proxy` 运行的 uv/Python，附加并发现后代；消失后继续等待下一轮，不启动代理 |
| `--pid PID` | 立即附加指定活进程和后代；所有已捕获进程退出后结束 |
| `--launch EXE ARGS...` | 明确启动新进程并保留句柄；所有已捕获进程退出后结束，供以后手动切换 |

监控句柄只请求查询/同步权限，没有终止/重启目标的接口。`--duration SECONDS` 或 Ctrl+C 只结束监控，目标继续运行。等待模式匹配本机可访问的同名模块，不能按工作目录区分实例；console-script 启动、改名解释器等用 `--pid` 精确附加。

## 先保全，再观察

先确认实际进程和 `/health`，不能因业务日志没更新就推断进程死亡。已有活进程应立即附加，而不是等待扫描。

把 `.workbuddy2api` 下当前 `process-events.jsonl`、`console.log`、`crash.log`、`codebuddy-proxy.jsonl`、`proxy.log` 复制到时间戳目录，保存 SHA256，不改原件。证据可能包含凭证和请求，只留本机，不上传 PR。

## 独立启动监控器（不启动代理）

本次 worktree 的示例（合并后可将 `$monitorRepo` 改成主目录）。解释器借用原项目环境，不安装/修改依赖。

```powershell
$monitorRepo = 'D:\workspace\workbuddy2api-external-monitor'
$monitorPython = 'D:\workspace\workbuddy2api\.venv\Scripts\pythonw.exe'
$monitorArgs = '"' + $monitorRepo + '\scripts\watch_processes.py" --watch-proxy'
$monitor = Start-Process -FilePath $monitorPython -ArgumentList $monitorArgs `
  -WorkingDirectory $monitorRepo -WindowStyle Hidden -PassThru
$monitor | Select-Object Id, StartTime
```

`pythonw.exe` 无交互式控制台，监控器和代理分别运行。不能把监控工作放进代理线程，也不要在要测试关闭的代理控制台里运行它。本工具不安装服务/计划任务、不设置开机自启。如果宿主用 Job Object 连带终止后代，独立控制台仍不保证逃离 Job；持久部署可由维护者在独立计划任务/服务中启动相同命令。尚未在 Wave 关窗场景验证此边界。

默认目录 `$HOME\.workbuddy2api\monitor\YYYYMMDD-HHMMSS-随机标识\`。先确认 `events.jsonl` 有 `monitor_start`，随后 `sample` / `health` 持续更新。等待时 `sample.processes` 可为空。无日志或出现 `monitor_error` 表示监控未生效。

```powershell
$latest = Get-ChildItem "$HOME\.workbuddy2api\monitor" -Directory |
  Sort-Object LastWriteTime -Descending | Select-Object -First 1
Get-Content (Join-Path $latest.FullName 'events.jsonl') -Tail 12
```

若代理已运行，以 `Get-CimInstance Win32_Process` 找到 uv 根 PID，把启动参数换成 `--pid 实际PID`，不要复用历史 PID。不存在/无权限会明确失败。避免重复监控相同范围。

## 将来的启动模式

**本次实施不执行下面的真实代理命令，也不停止现有代理。** 将来由维护者选择切换时，在主项目目录执行：

```powershell
Set-Location 'D:\workspace\workbuddy2api'
.\.venv\Scripts\python.exe ..\workbuddy2api-external-monitor\scripts\watch_processes.py `
  --launch uv run python -m codebuddy_proxy --desensitize --host 0.0.0.0
```

`--launch` 之后全是目标参数；监控选项放在前面。目标继承工作目录、环境和原参数，以 `CREATE_NO_WINDOW` 运行，stdin=DEVNULL，原生 stdout/stderr 写入该次目录 `target-stdout.log` / `target-stderr.log`，覆盖绕过 Python Tee 的输出。控制台和交互输入行为已改变，先完成认证；已有服务占用端口时不要再启动一份。

OS 输出文件不轮转，避免目标持有句柄时截断证据，长时间启动模式需另行管理磁盘空间。监控 JSONL 轮转 8 MiB × 当前文件加 4 个备份；多个运行目录另行归档。取证事件 flush+fsync，普通 sample 仅 flush。磁盘满会使监控失败，不能保证断电或磁盘故障仍有完整证据。

## 记录与判读

默认每 250 ms 枚举后代/检查退出，每 2 秒记录内存/心跳和 localhost:8787/health（超时 1 秒），每 30 秒查询 Windows 事件（后台执行，总超时 15 秒）。UTC 时间、monotonic_ns、seq 用于关联；**seq 是发现顺序，退出顺序须看 exit_ticks**。

| 事件 | 内容 |
| --- | --- |
| `process_observed` | pid、ppid、image、created_ticks / created_at；句柄已保留，不代表此刻才启动 |
| `process_exit` | Windows 无符号 exit_code / exit_code_hex、GetProcessTimes 的 exit_ticks / exited_at、最后存活采样、cause=unknown |
| `sample` | PID+创建时间、working_set_bytes / private_bytes；权限不足写 memory_error |
| `health` | HTTP 状态/是否成功/耗时；不读取正文、不走环境代理、不跟随重定向 |
| `business_log_tail` | 文件长度/更新时间，末尾最多 64 KiB / 32 条中的 timestamp/event/request_id/status/run_id/pid 白名单；不复制 headers/body |
| `windows_event_query` / `windows_events` | 查询窗口、System/Application 警告/错误/严重事件，以及可读 Security 4688/4689、Sysmon 1/5，后两者按目标 PID 过滤 |
| `*_failed` / `process_open_failed` / `monitor_error` | 采集失败，不能解释成“没有发生事件” |
| `monitor_stop` | 停止原因、still_tracked、targets_stopped=false；心跳中断且无此记录，监控器本身也可能消失 |

先用 `WaitForSingleObject(handle, 0)` 判断终止再读 `GetExitCodeProcess`，可区分仍运行与真正退出码 259。原始 exit_ticks 保留 Windows 100ns 值，格式化时间精度为微秒。句柄和创建时间用于防止 PID 重用误关联。

事件查询重叠 60 秒，按 channel+record_id 去重，每频道最多 200 条，超出标 truncated。延迟超过窗口、日志被清空仍可能漏采。超时/无权限/频道不存在明确记录；不修改审计策略、不安装 Sysmon。频道可读但无匹配事件，不代表已开启进程审计；4688/4689 需管理员核实策略。4689 的 Subject 账户不是终止调用方进程 ID。

复盘步骤：

1. 按 `(pid, created_ticks)` 分组，把 uv、venv Python、解释器的 `process_exit` 按 `exit_ticks` 排序。相等时不能确定先后；只捕获一层时不能推断其他层。
2. 对齐最后成功/首次失败的探活，以及业务 request_id 完成事件。尾部摘要不完整，**不能证明没有在途请求或处于空闲**；完整原日志按 request_id 配对，并统一数字/字符串 timestamp 的时区。
3. 检查系统事件、truncated 和采集失败；不能把时间邻近的更新/SideBySide 错误直接视作原因。
4. 保留运行目录、轮转文件、原业务日志，再根据状态选择 WER/ProcDump/ETW/EDR。退出码只是状态证据，程序/外部调用者都可指定相同值；本工具不识别 TerminateProcess 调用方。

## 观测边界

不能恢复已死亡且未留句柄的历史进程退出码。等待模式有约 5 秒加 CIM 查询延迟的窗口，后代枚举有 250 ms 窗口，短命进程可能漏采；启动模式持有根进程，但不保证捕获所有短命后代。不能从新监控倒推 08:53 故障根因。

记录监控器自己的可处理 SIGINT/SIGTERM，不拦截/注入目标信号，不能证明目标收到 Ctrl+C/CTRL_CLOSE。监控器/系统一起退出、断电可能同时丢证据。终端关闭、Job Object、真实 native crash 和资源耗尽仍待现场验证。

## 测试和 Review

```powershell
$env:PYTHONUTF8 = '1'
$env:PYTHONPATH = 'src;.'
D:\workspace\workbuddy2api\.venv\Scripts\python.exe -m pytest test_external_process_monitor.py -q
D:\workspace\workbuddy2api\.venv\Scripts\python.exe -m pytest -q --tb=short `
  -k 'not test_session_profiles_are_isolated_and_saved_with_metadata'
```

测试仅对独立创建的临时进程精确 Popen 对象/句柄强制终止，绝不按名称批量结束。覆盖正常/异常/绕过 Python 钩子的退出、259、高位状态、强制终止、uv/Python 分别退出和顺序、监控停止后目标存活、发现、轮转、脱敏、PID 身份及频道权限。高位状态测试调用 ExitProcess 指定值，不是真实访问冲突。原 Windows 0600 权限断言失败单独报告，未改原测试。

Review 检查最小权限、无终止/重启接口、句柄释放、PID 重用、259、非阻塞采样、事件过滤/上限/失败、采集延迟和隐私。真实 Wave 关窗、注销/断电、终止者归因尚未验收，不能以隔离测试代替生产复现。

参考：[进程句柄/ID](https://learn.microsoft.com/en-us/windows/win32/procthread/process-handles-and-identifiers)、[WaitForSingleObject](https://learn.microsoft.com/en-us/windows/win32/api/synchapi/nf-synchapi-waitforsingleobject)、[4689](https://learn.microsoft.com/en-us/previous-versions/windows/it-pro/windows-10/security/threat-protection/auditing/event-4689)、[Sysmon](https://learn.microsoft.com/en-us/windows/security/operating-system-security/sysmon/sysmon-events)。

当前 Windows 实际解释器取证入口及回滚以 [silent-process-exit.md](silent-process-exit.md) 为准。2026-10-23 20:38/21:02 的再次退出证实 venv launcher 取证不足；新入口使用两个专用真实解释器映像，原 pythonw 观察器与额外独立 observer 保留。最新回归 267 passed、1 skipped、1 deselected。
