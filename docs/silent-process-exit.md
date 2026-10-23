# 静默退出来源取证（Windows，可回滚）

现有外部监控已经证明 2026-10-23 19:41:31 venv launcher 最先以 FFFFFFFF 结束，解释器随后为 0、uv 为 FFFFFFFF；对临时 launcher 的强制终止复现了相同组合，但原事故触发者仍未知。此阶段增加 Windows Silent Process Exit，争取记录自行退出或跨进程 TerminateProcess 的来源和转储。

## 已验证的部署方式

本机完整路径过滤试验未生成预期报告，未用于实际部署。改为在原 `.venv\Scripts` 中创建 `workbuddy2api-evidence.exe`，内容与原 `python.exe` 的 SHA256 完全一致，沿用相同 pyvenv.cfg / site-packages。仅对这个专用映像名设置 IFEO GlobalFlag=0x200 和 SilentProcessExit；**不设置全机 python.exe，不修改原解释器、依赖或业务代码**。

该更名本身会改变一个调查变量：如果原终止触发者按 python.exe 名称匹配，可能不再复现。因此长时间稳定只能支持隔离结果，不能据此认定原故障已修复。

管理员脚本 `scripts/manage_silent_exit.ps1 -Action ValidateAndEnable -StateDirectory 绝对路径` 会：

1. 拒绝覆盖已有静默退出配置；先备份每个拟修改的值及类型，写入原子更新的 registry journal。
2. 保护本机证据目录 ACL，仅当前用户、SYSTEM、Administrators 访问。
3. 创建两个独立临时命名启动器，验证未配置的 control 不产生报告、配置的 probe 自退出产生 3000、由本次 PowerShell 终止产生 3001 并识别 PowerShell。
4. 等待 Windows 在按事故分组的子目录生成目标及发起者 mini dump；没有发起者转储则拒绝部署。所有 Kill 仅针对本次创建的精确 Process 对象。
5. 撤回临时测试注册表设置，再对通过 SHA256 校验的专用启动器启用取证，写入 result.json。任意失败尝试回滚并记录错误；若存在 rollback_errors 必须先处理，不能把失败当成已恢复。

ReportingMode=2（仅本地转储）、IgnoreSelfExits=0、DumpType=0（MINIDUMP_TYPE 的 MiniDumpNormal，非全内存）、MaxNumberOfDumpFiles=8。没有配置 Debugger、MonitorProcess 命令、自启动、自动恢复或停止真实服务。Windows 可能按事故创建转储子目录；应按整次事故归档并管理磁盘，文件数量上限不等于严格总字节配额。

## 本次实机状态（2026-10-23）

- 成功状态目录：`C:\Users\marcus\.workbuddy2api\silent-exit\20261023-causal-exit-04`，20:25:58 UTC+8 启用。
- 原 launcher 与专用副本 SHA256：`A294897BC1A4780C2B8615BE41FDEBA62B6B2B83B13C46A530B299D6C66D29E0`。
- 临时 self-exit、cross-process 事件及三份转储已取得；cross-process 报告指向本次测试 PowerShell，发起者 dump 可解析到线程栈和上下文。前一轮相同 dump 的离线检查有 28 个线程、28 份栈/上下文。
- `start_proxy_with_evidence.ps1 -CheckOnly` 返回 ready，未启动代理。配置只对未来使用专用启动器的进程生效，不附加/重启当前真实服务。
- 之前失败尝试因备份写入兼容、PowerShell 5.1 JSON 数组包裹、转储子目录检查而停止；回滚问题已修正并验证。路径过滤试验的临时配置也已清理。失败状态目录留作审计，不作为已启用实例。

## 下一次启动

```powershell
& 'D:\workspace\workbuddy2api-external-monitor\scripts\start_proxy_with_evidence.ps1'
```

脚本自动选择最近成功且未回滚的本机状态，检查注册表、launcher 原件/副本 SHA256，发现已有代理时拒绝重复启动，也不会结束它。随后在原项目目录，用原 venv Python 运行独立 `watch_processes.py --launch`，由它启动专用 launcher：`-m codebuddy_proxy --desensitize --host 0.0.0.0`。

这次没有经过 uv，目标无控制台、stdin=DEVNULL，不适合交互登录；应预先完成认证。启动命令、文件名、控制台和父进程均是隔离变量，必须与历史运行区别记录。原无控制台等待监控仍可观察实际 python.exe，新启动父监控负责专用 launcher 的完整句柄记录，不需要停止旧监控。

终端显示取证目录，原生 stdout/stderr 保存在该目录 `target-stdout.log` / `target-stderr.log`，不在终端实时显示，也不轮转。外部 JSONL 仍按既有规则轮转。用 `/health` 或日志确认服务成功启动，不能只看启动脚本是否返回。

仅检查，不启动：

```powershell
& 'D:\workspace\workbuddy2api-external-monitor\scripts\start_proxy_with_evidence.ps1' -CheckOnly
```

**继续使用旧 `uv run python -m codebuddy_proxy ...` 仅有原外部监控，不会获得本次专用映像取证。** 如果需要保留 uv 或原控制台，应另设计单变量启动实验，不能混同两种运行条件。

## 故障后查看与隐私

独立 JSONL、Windows Application 的 `Microsoft-Windows-ProcessExitMonitor` 3000/3001、上述状态目录 `dumps` 子目录共同保全。3001 的参数有目标路径、终止发起者路径、状态和目标创建时间；创建时间可与外部监控的 created_ticks 对齐，避免同名/重复 PID 误配。现有 Windows 事件采集助手已补充该 provider 的信息级事件，原运行中的监控器下一次调用助手即可读到，无需重启。

转储可能包含敏感内存（mini dump 也不能保证无凭证），只留本机受限目录，不上传 PR。发起者 PID/线程栈可从其转储进一步分析；报告中的退出码不替代调用来源证据。Windows 未承诺覆盖内核发起终止、所有 Job 回收路径、最后线程退出、断电等情况。没有报告不能直接排除外部终止。

## 回滚

管理员 PowerShell 执行（只恢复本工具记录的值，不停止任何进程）：

```powershell
& 'D:\workspace\workbuddy2api-external-monitor\scripts\manage_silent_exit.ps1' `
  -Action Rollback `
  -StateDirectory 'C:\Users\marcus\.workbuddy2api\silent-exit\20261023-causal-exit-04'
```

回滚检查当前值是否仍等于本工具写入值，发现后来其他工具修改时报告冲突；不覆盖未知更改，不递归删除注册表。仅删除本轮新建且已空的键。诊断 exe 副本和证据目录保留，不删除运行中的文件；不再用于诊断时可在确认无进程使用后由维护者归档。回滚后启动脚本会拒绝使用已撤销状态；原启动方式保持可用。

单元回归只在随机 HKCU 测试键检查备份连续更新、保留原值、回滚冲突、拒绝已有配置及拒绝共享 python.exe，从不在单元测试中写 HKLM。管理员实机验证负责上述原生报告/转储验收，两者分别记录。

最终回归 257 passed、1 skipped、1 deselected；排除项为原有 Windows 0600 文件权限断言。Review 重点为白名单映像范围、配置/回滚冲突保护、测试对象所有权、PowerShell 5.1 行为、信息级事件采集、目录权限与转储数据边界。

参考：[微软 Silent Process Exit](https://learn.microsoft.com/en-us/windows-hardware/drivers/debugger/registry-entries-for-silent-process-exit/)、[监控标志 0x200](https://learn.microsoft.com/en-us/windows-hardware/drivers/debugger/enable-silent-process-exit-monitoring)。
