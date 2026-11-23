# Buddy Switch 请求上下文兼容迁移

> 状态：基础上下文与工具双向适配已实现，针对性离线测试通过；状态缓存未启用。
> 参考：Buddy Switch origin/main ebcaead17196c6127ed2cb85669443bfc981047f。
> 不代表真实上游验收，也不代表修复历史 Windows 进程异常退出。

## 边界与参考

迁移正常协议语义，不迁移身份替换、伪装官方客户端、拒绝后改写请求重试或规避限制。
不读取凭据、不发送真实上游请求、不改账号选择/认证/服务启动或进程诊断。
用户既存 account_manager/admin 相关五个文件及 docs/assets 未提交工作保持不动。

Buddy crates/buddy-switch-gateway/src/protocol/responses.rs 的参考点：
- input_to_messages（约 1056）：历史消息与并行 function_call 分组。
- parts_to_content（约 1186）：input_text/output_text/text 与图片转换。
- flatten_namespace_tools（约 136）及恢复身份：工具必须双向闭环。
- state_scope_for_session（约 1038）：短期历史需要作用域隔离。
本实现采用 Python 原生适配，不直接复制 Rust 代码或默认启用 Buddy 的其它策略。

## 已实现

### 历史与选择
- 保留 output_text 历史和文本/图片顺序；图片转 image_url，不下载图片。
- Responses 具名 tool_choice 不再退化为 required。
- parallel_tool_calls 包括 false 原样保留。
- 沿用现有角色映射与工具配对；未答复执行、冲突结果拒绝，绝不伪造执行成功。
- developer→system 的既有转换仍受旧协议表达能力限制，不是独立角色层级的完整支持。

### namespace 与 custom 双向闭环
新 responses_tools.py 提供逐请求映射，不用全局表：
- namespace 内 function/custom 声明展平为合法长度的稳定函数别名；
- 别名由完整身份摘要生成，保留显式反向映射，检测普通函数撞名和重复声明；
- 同步转换具名 tool_choice、function/custom 历史调用和结果；
- 流式 added/done/completed 与非流式 output 恢复原 name、namespace、type；
- 文本与并行工具按首次出现顺序分配稳定 output_index，completed 的 output 按同序排列；
- custom 用单个字符串 input 包装；分片 JSON 在完成前不冒充 raw input 下发；
- 完整包装解析后发原始 custom input delta/done，保留引号、反斜线、中文和空串；
- 包装无效则 response.failed，不给客户端伪造可执行输入。
历史工具已从当前声明移除时，仍从显式历史身份重建映射；不同请求不借用映射。

### 非流式兼容
- 修正 Responses stream:false 仍返回 SSE 的问题；客户端偏好独立于上游强制 SSE。
- function_call/custom_tool_call 放在顶层 output，带 call_id；不再塞入 message.content。
- 非流式聚合遇内嵌 error 或缺少 [DONE] 返回502，不报告成功。
- 正常完成时 flush DSML 尾部缓冲，避免尾部文字遗失。
旧测试的错误结构预期已同步改为正确线协议，不改变 Anthropic/OpenAI 的格式契约。

## 未实现及具体原因

### previous_response_id 状态缓存：明确拒绝，不能伪称支持
当前 /v1 middleware 只固定全局 active account，没有已验证的下游 principal。
admin 的本机保护不覆盖 /v1；监听地址可为0.0.0.0；429路径还可能切换实际上游账号。
任意 session header 或未经验证的 API key 哈希只能防误串，不能成为授权边界。
因此直接移植共享历史表会新增跨用户/跨账号读取历史的风险。
现在返回400 unsupported_context，要求客户端提交完整 input，不静默忽略引用。

安全启用的前置条件：
1. 接入已验证的下游主体或明确独立的单用户 capability 模式；不能信任自报身份。
2. principal + backend + endpoint + 实际执行账号 + client session 共同隔离。
3. 续接请求不能因429无提示换账号转发旧历史。
4. 随机响应ID、TTL、条目/单条/总字节限额、深拷贝、锁保护；缺失/跨scope统一错误。
5. 只写入完成结果；失败、取消、缺终止标记都不得缓存；不记录正文/ID。
本轮不擅自增加凭据管理或改变认证架构，避免将协议迁移变成安全设置变更。

### 其它限制
- 不解密 compaction，不伪造缺失的历史；仍需要可读摘要或完整历史。
- custom 支持缺省/text格式；grammar及其它constrained格式明确400拒绝，不静默移除约束。具体工具仍需客户端验证。
- function strict 与 additionalProperties 沿用现有适配器兼容策略，不能宣称提供原生严格schema约束。
- 不声称新支持服务端 web_search/file/audio 等内置工具。
- 上游模型是否支持图片、参数及实际风控由其契约决定，离线通过不是真实上游承诺。
- 流式继承现有完整函数名首片假设；不宣称修复所有乱序或畸形上游分片。
- 新增非流式规则要求 [DONE]；缺终止标记即使已产生部分文本也明确失败。

## 验收

Windows 现有 .venv，使用假上游/内存对象，不访问真实账号或外部模型。
最终合并针对性回归：143 passed，3.69秒。唯一警告为既有 Starlette/httpx 弃用。
独立复核重新实测grammar拒绝和并行custom/普通工具索引，另跑84项通过；复核未修改文件。
覆盖上下文、输入不变、角色、工具配对/长历史、普通及namespace custom多轮、
逐字符分片/中文/引号/空输入、碰撞、坏包装、grammar拒绝、多工具稳定索引、HTTP stream:false JSON、
流式错误、非流式error/EOF和协议返回契约。
CI 已加入新的context/tools与协议formatter回归。
这是针对性回归，不是全仓或线上验收。

## 交付与回滚

未提交、推送、发布或重启服务。只撤回本轮文件/hunks可回滚，不动用户原工作。
本轮文件：responses_adapter.py、responses_tools.py、protocol_response.py、__main__.py，
新context/tools测试，developer/formatter契约测试，CI，.gitignore的单文档例外及本文。
原CRLF保留；使用 git -c core.whitespace=cr-at-eol diff --check 验证格式。
