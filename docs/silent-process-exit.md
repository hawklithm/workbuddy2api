# Windows 实际解释器退出取证

2026-10-23 20:38 现场证实旧配置只覆盖 venv launcher，真正的 Anaconda python.exe 先退出，转储无法说明它的来源。21:02 再次出现启动监控解释器与代理解释器相近退出，顺序与此前不同。尚未确认根因。

## 当前方案

`scripts/prepare_native_runtime.py` 从 `sys.base_prefix` 复制真实 CPython console/windowed 映像及 Python、VC runtime、Anaconda zlib DLL，生成独立 runtime。不是复制 venv redirector。专用映像名为 `workbuddy2api-runtime.exe` 和 `workbuddy2api-observer.exe`，只对这两个名称配置 Silent Process Exit，不修改共享 python.exe/pythonw.exe 的设置，也不改原 venv 文件。

bootstrap 保留 DLL 搜索目录句柄，通过 site.addsitedir 使用原 venv 依赖及 editable 源码。sys.prefix 指向诊断 runtime，sys.base_prefix 保持原解释器；这是一项明确的环境差异。原 venv 及基础 Python 仍须保留。复制文件、基础映像/DLL 和原 pyvenv.cfg 的 SHA256 在启动前校验；升级 Python 后需重新验证。

`scripts/manage_native_runtime.ps1` 的管理员验证要求：

- OS 映像、sys.executable 和实际 workload PID 相符，拒绝再次落入 redirector 子进程；确认 FastAPI、SSL、SQLite 和原代理源码可导入。
- 临时真实解释器正常退出 23、自行 os._exit(29)，分别取得对应 3000。
- 只终止本脚本创建且 readiness PID 匹配的临时 Process 对象，取得 3001、已知 PowerShell 发起者和目标/发起者两方 mini dump。
- 临时 probe 配置回滚后，为两个生产映像设置 0x200、ReportingMode=2、IgnoreSelfExits=0、DumpType=0、MaxNumberOfDumpFiles=8；再对两个生产映像执行临时 import/identity 检查，分别确认 3000。这些测试事件不能当作真实事故。
- 任何失败均尝试按每值 journal 回滚并保留错误。拒绝覆盖已有 SPE 设置或已启用 IFEO filter/debugger；不递归删除注册表，不停止真实代理。

日志/转储保留在仅当前用户、SYSTEM、Administrators 可访问的 state 目录；mini dump 仍可能包含敏感信息，不上传公开 PR。数量限制不等于总字节配额；启动监控的 stdout/stderr 也没有字节轮转。

## 已部署与下次启动

本机有效状态：`C:\Users\marcus\.workbuddy2api\silent-exit\20261023-native-runtime-04\result.json`，2026-10-23 21:11:10 UTC+8，status=native_runtime_enabled。01/02 为已回滚失败尝试（缺少 Anaconda zlib 导致进程初始化失败）；03 验证成功后已回滚，由含源文件哈希及双生产映像验证的 04 取代。

原 pythonw 观察器保持运行。另有专用观察器 PID 36748 于 21:12:24 启动，只观察，不启动代理，日志在 `C:\Users\marcus\.workbuddy2api\monitor\20261023-211224-5ed62f15`。PID/存活状态应现场核对；没有开机自启或自动重启设置。

下次手动启动（命令保持一致）：

```powershell
& 'D:\workspace\workbuddy2api-external-monitor\scripts\start_proxy_with_evidence.ps1'
```

只检查配置，不启动：

```powershell
& 'D:\workspace\workbuddy2api-external-monitor\scripts\start_proxy_with_evidence.ps1' -CheckOnly
```

入口只接受 native_runtime_enabled，拒绝旧 launcher-only 状态；检查哈希及两套 Windows 配置，拒绝已有代理。以无控制台 observer 映像启动独立监控，再启动真实 runtime 映像，不经过 uv 或 venv redirector。入口返回 observer PID、监控目录及启动日志；这只是进程创建确认，健康状态需查看日志。关闭启动终端不会主动执行清理代理，但 Windows Job/会话策略仍可能影响进程。

取证配置供未来启动生效。此次未由维护者启动/停止/重启真实代理，也没有把仍在运行的进程热切换到新解释器。

## 配置与回滚

在 64 位管理员 Windows PowerShell 中，新机器/重新验证使用全新 state 目录：

```powershell
& 'D:\workspace\workbuddy2api-external-monitor\scripts\manage_native_runtime.ps1' -Action ValidateAndEnable -StateDirectory 'C:\Users\marcus\.workbuddy2api\silent-exit\NEW-UNUSED-DIRECTORY'
```

已有本工具配置应先使用其 state 回滚，不覆盖。当前配置回滚：

```powershell
& 'D:\workspace\workbuddy2api-external-monitor\scripts\manage_native_runtime.ps1' -Action Rollback -StateDirectory 'C:\Users\marcus\.workbuddy2api\silent-exit\20261023-native-runtime-04'
```

回滚只恢复设置，保留文件及证据，不停止任何代理/观察器。旧 launcher-only 配置仍作为历史独立 state 保留；需要撤销时用旧 manage_silent_exit.ps1 配合原 state，不能拿新脚本混用旧 journal。

## 判断边界及验证

3000 表示受支持路径上的自行退出；3001 可提供跨进程终止发起者，结合目标创建时间、句柄时间线及双方转储分析。未捕获事件不能排除内核终止、部分 Job 清理、最后线程退出、断电等未覆盖路径。改变映像名称可能改变按名称清理行为，即使不再复现也不能单凭稳定性确认根因。

回归：267 passed、1 skipped、1 deselected；排除项仍是原 Windows POSIX 0600 权限断言。新增测试验证真实 OS 映像/PID、最小 PATH 下依赖解析、无控制台 observer/临时子进程的完整退出记录、参数转义、文件变更拒绝、HKCU 私有测试键配置/回滚与冲突保护。原生管理员事件/转储验收另行完成；未做真实代理启动验收。Review 重点为范围、进程所有权、可回滚性、依赖一致性及证据关联。

参考：[Microsoft Silent Process Exit](https://learn.microsoft.com/en-us/windows-hardware/drivers/debugger/registry-entries-for-silent-process-exit/)、[Python DLL 搜索目录](https://docs.python.org/3.10/library/os.html#os.add_dll_directory)。
