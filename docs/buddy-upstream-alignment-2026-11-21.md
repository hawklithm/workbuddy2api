# WorkBuddy 出站与 reasoning 对齐

参考 Buddy Switch `ebcaead17196c6127ed2cb85669443bfc981047f`，按当前工作区增量实现。

## 契约

- 国际版默认 LLM 后端切换到 `www.workbuddy.ai`，显式 endpoint 与已保存账号 endpoint 保持有效；旧 `www.codebuddy.ai` 继续兼容。
- 仅内置且 backend/host 匹配的网关应用 WorkBuddy 身份、首条 system、模型参数和缓存键规则，避免污染自定义网关。登录、刷新、账单认证仍使用原认证流程。
- 会话在提示词改写前从原协议请求及选定 header 提取；同一请求的重试复用关联上下文，每次发送有独立 message ID。不新增历史缓存或改变账号选择策略。
- DeepSeek thinking、区域档位表、历史 reasoning 回填、账号缓存键与 Buddy 对齐。调用方缓存键保留。
- 保留 `--desensitize` 开关，加入 Buddy 指纹处理；增加 passthrough/custom 提示词模式、显式文件与内容拦截降级。降级仅在响应交付前一次，按 backend/endpoint/账号隔离至次日北京时间零点；不把渠道错误一概当成内容拦截。
- 共享 reasoning 差分层供三协议流式及非流式使用，保留原生工具与 DSML 路径。Responses 输出 reasoning summary，Anthropic 输出 thinking 块；不伪造签名。
- 保留当前未提交的 Responses namespace/custom、断流检测和账号管理改动。

## 验证

使用离线假上游验证真实 HTTP headers/body 和三协议输出，覆盖多轮/多会话、重试关联、区域模型档位、disabled thinking、缓存账号隔离、提示词开关与降级、中文快照/真增量/工具混流、终止标记与取消。运行现有协议、认证、后端与账号回归。真实账号、真实模型、服务重启与部署不在本轮本地验收范围。

## 状态

已完成当前源码实现，未提交或重启服务。

- [x] 国际版默认 `www.workbuddy.ai`；显式旧 endpoint 保持可用配置。
- [x] chat 专用 WorkBuddy UA 与身份头；版本按显式环境配置、macOS 已安装 bundle、Buddy 保存版本、内置值解析。
- [x] 原始请求的会话身份、完整关联头、同请求重试稳定关联、独立 message ID。
- [x] DeepSeek thinking/禁用语义、区域档位表、历史 reasoning 回填；按模型配置默认档位。
- [x] 自动缓存键绑定实际账号；客户端显式键保留；国际版补首条最小 system。
- [x] 指纹净化接入既有 `--desensitize`；passthrough/custom、UTF-8 文件及内置工程提示词。
- [x] 内容拦截降级按 backend/endpoint/账号隔离至北京时间零点；同请求最多重试一次，custom 模式不降级。
- [x] reasoning 逐 choice 快照差分、空占位清理、角色兜底、usage/finish/error 保留。
- [x] 三协议流式/非流式 reasoning 输出；Responses summary 索引与 custom 工具闭环。

## 验收结果

- 新增 `test_buddy_alignment.py`：38 项通过。假上游覆盖三协议 × 流式/非流式 × 按字节 UTF-8/CRLF 分片、快照差分、独立 choice、custom 工具混流、prompt 模式和拒绝边界、原请求不变、投影前身份、缓存键和思考档位。
- 合并协议、上下文、工具、账号、登录、请求头、连接重试、429、模型和 DSML 离线回归：368 passed，57.44 秒。5 个警告来自既有依赖弃用和旧测试返回值。
- 最后针对会话与 429 换号补充缓存键/关联头断言后复核：89 passed。实际换号时 Conversation-ID/Request-ID 保持，Message-ID 与账号缓存键改变。
- `uv build --wheel` 成功；解压后的 wheel 在独立解释器中导入成功，验证新模块、CLI 参数和内置提示词资源齐全。
- `git diff --check` 通过。原有 account_manager/admin 相关五文件和 docs/assets 未作本轮编辑，既有 Responses namespace/custom 改动保留。

## 对齐取舍与运行边界

- 指纹净化仍由现有 opt-in 开关控制；自定义网关不应用 WorkBuddy 专用身份/模型策略。
- 相比直接字符串替换工具参数，本实现先解析 JSON，仅改字符串值，保留数值和工具名，避免把数字 `11128` 改成非法 JSON。多模态图片地址保持原值。
- 提示词降级按账号隔离，避免没有下游 principal 时跨账号共享降级状态。会话关联不建立任何历史授权或 Responses 缓存。
- 本轮增强到 Anthropic thinking 的映射，不伪造模型签名；真实 Claude 客户端和上游模型兼容性仍需实测。
- 账号选号和登录/刷新流程沿用现有实现；不将旧国际站凭据自动迁移到新域名。已保存账号的 endpoint 不变，显式旧站可用 `--global --endpoint https://www.codebuddy.ai` 选择。
- Windows/Linux 与参考版本一样使用保存版本或内置 UA 版本；可用 `WORKBUDDY_VERSION` / `WORKBUDDY_AI_VERSION` 明确设置。没有声称验证这些平台的安装位置探测。
- 未读取真实账号用于调用，未发送真实模型请求，未重启、部署、发布。HTTP 200 业务错误信封治理、账号池策略和 Responses 历史缓存不在本轮功能范围。

## 后续操作：已有账号迁移

用户随后明确要求切换已有账号。使用新增 `scripts/migrate_workbuddy_global_endpoint.py` 先 dry-run，再显式执行：

- 实际迁移 1 个 Global 账号：`https://www.codebuddy.ai` → `https://www.workbuddy.ai`。
- 同步重算账号 ID、更新 session endpoint/旧 auth.domain、迁移冷却状态键；凭据值不变，2 个国内账号及当前选中账号保持不变。
- 原始索引、session 和运行状态已备份到用户账号目录下 `endpoint-backups/20261121T010921Z-15aadbd6/`；备份含凭据，不纳入仓库。
- 迁移后全部 3 个账号通过 AccountStore 身份校验，AccountManager 状态可正常加载。
- 使用迁移账号实际调用 WorkBuddy 只读额度查询 `/v2/billing/meter/get-user-resource`，得到 HTTP 200、业务 `code=0`。未发送模型生成请求，也未刷新 token。
- 当时未发现运行中的代理或 8787 监听；无需重启进程。
- 新增迁移测试覆盖 dry-run、账号/冷却状态重建、原凭据和国内账号保留、重复执行、写失败回滚和目标冲突拒绝；与账号存储测试合计 14 passed。

脚本默认 dry-run，必须停代理后使用 `--execute` 执行；失败会恢复原始文件，不会覆盖已经存在的目标账号。
