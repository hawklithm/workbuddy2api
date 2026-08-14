# codebuddy2api 端点处理逻辑梳理

> 项目：`/Users/adazhao/workspace/codebuddy2api`（CodeBuddy → OpenAI/Anthropic 兼容网关，直连腾讯后端 `https://copilot.tencent.com`）
>
> 本文梳理每个对外端点对 **请求内容** 与 **返回内容** 的完整处理链路。代码基准：`converter.py`（入口）、`anthropic_adapter.py`、`responses_adapter.py`、`responses_projection.py`、`desensitize.py`。

---

## 0. 全局机制（所有端点共用）

### 0.1 鉴权 `_check_auth`（`converter.py`）

- 仅当启动时设置了 `--api-key`（或环境变量 `CODEBUDDY2OPENAI_KEY`）才校验。
- 客户端需携带 `Authorization: Bearer <key>` 或 `X-Api-Key: <key>`，不匹配返回 `401 {"error": {"type": "auth_error"}}`。
- 未设置 key 时**不校验**，任何请求放行。

### 0.2 凭据 `CredentialManager`

- 从桌面端 CodeBuddy 登录目录读取 `*.info` 凭据文件（macOS / Windows / Linux 各有路径，也支持 `CODEBUDDY_AUTH_DIR` 覆盖）。
- 每次请求通过 `get_headers()` 取后端 headers：`Authorization: Bearer <accessToken>`、`X-User-Id`、`X-Enterprise-Id`、`X-Tenant-Id`、`X-Domain`、`User-Agent: codebuddy2openai/2.0`。
- token 距过期 **<60s** 时自动调用 `POST /v2/plugin/auth/token/refresh` 刷新，并原子写回 auth 文件。
- 未找到凭据 → `503`。

### 0.3 脱敏模块 `desensitize`（可选，`--desensitize` 开启，默认关）

对所有走转换的端点（chat / responses / messages）**在发往后端前**统一应用，`converter.py` 中三处调用参数一致：

```
roles=("system","developer")
desensitize_harness_user=True
desensitize_tools=True
compact_harness = not CONFIG["no_compact"]
strip_tool_metadata=True
```

处理内容（详见 `desensitize.py`）：

| 处理 | 作用对象 | 行为 |
|---|---|---|
| 零宽空格插入 | system/developer 文本 + 命中 `SENSITIVE_TERMS` 词表（约 80 词：`DoS`/`exploit`/`credential testing`/`SQL injection`/`Claude Code`/`Anthropic` 等） | `DoS → Do\u200bS`，打断后端关键词匹配，缓解合规模板被审核误拦 |
| harness 压缩 | 识别为 Codex/Claude Code 注入的 system 或 user 消息 | 整段替换为一句摘要（如 `"You are Claude Code"` → 通用助手描述） |
| 运行时块替换 | `<environment_context>`、`<permissions instructions>`、`<skills_instructions>`、`<system-reminder>` 等块 | 替换为一句摘要（`--no-compact` 时仍做） |
| 运行时尾部裁剪 | `The following deferred tools…`、`## MCP Server Instructions` 等 | 截断并追加一句总括 |
| tool 元数据剥离 | tools 的 `description`/`title` 字段 | **直接删除**该字段（`strip_tool_metadata=True`） |

- `--no-compact` 时跳过"压缩为摘要"，仅做零宽脱敏 + 运行时块替换。
- 真实用户输入（非 harness 注入的 user 消息）**保持原样**。

### 0.4 公共请求改写（三个 LLM 端点）

- `model` 缺省补 `"auto"`；`stream` 恒置 `True`（后端只支持流式）；`stream_options` 缺省补 `{"include_usage": True}`。
- 后端 URL 均为 `POST https://copilot.tencent.com/v2/chat/completions`。
- 日志（`--log` 开启时）记录：请求摘要、完整请求体（脱敏后）、响应摘要、完整响应/原始 SSE。

### 0.5 响应错误处理（三个 LLM 端点）

- 后端非 200 → 按各协议格式包装错误返回（chat: OpenAI error chunk / JSON；responses: `{"type":"error"}` 事件；anthropic: `event: error`）。
- 后端网络异常 → `502`。
- 内容审核拦截特征：`content-filter` / `content_filter` / `敏感` / `审核` / `无法响应您的请求`（`_looks_like_content_filter_text`）。

---

## 1. `GET /v1/models` — 模型列表

### 请求处理
- `_check_auth` 鉴权。

### 返回内容
- 固定返回 `DEFAULT_MODELS` 列表（`glm-5.2`、`glm-5.1`、`glm-5v-turbo`、`kimi-k2.7`、`kimi-k2.6`、`kimi-k2.5`、`deepseek-v4-pro`、`deepseek-v4-flash`、`minimax-m3-pay`、`hy3-preview-agent`、`auto`）。
- 每个模型 `{"id", "object": "model", "created": 1700000000, "owned_by": "codebuddy"}`，包在 `{"object":"list","data":[...]}`。
- **不做**任何请求体处理（无 body）、不查后端、不脱敏。

---

## 2. `POST /v1/chat/completions` — OpenAI Chat Completions（原生透传）

### 2.1 请求处理（`chat_completions`）

1. 鉴权 → 读原始 JSON body（`payload`）。
2. **脱敏**（若 `CONFIG["desensitize"]`）：`desensitize_body`，参数见 0.3。作用于 system/developer 消息、harness user 消息、tools。这是该端点唯一的请求改写（`body` 即 `payload`）。
3. 补默认值：`model="auto"`、`stream=True`、`stream_options.include_usage`（`client_wants_stream` 在脱敏**前**从 `payload.get("stream", True)` 读取，用户是否要求流式决定走 2.3 还是 2.4）。
4. 其余字段原样透传（`PASSTHROUGH_BODY_KEYS` 白名单内的字段若客户端给出则保留：`messages/tools/tool_choice/temperature/max_tokens/max_completion_tokens/top_p/stop/presence_penalty/frequency_penalty/n/response_format/seed/user/reasoning_effort/verbosity/reasoning_summary` 等）。
5. **协议转换：无**（chat→chat 直通，无字段重映射）。

### 2.2 响应处理

**流式（默认）** — `_stream_upstream`：

- 后端 SSE **原样字节转发**给客户端，不修改内容。
- 边转发边解析统计：`finish_reason`、tool 名称、`usage`、是否命中内容审核特征，用于日志。
- 非 200：产出 OpenAI 格式 SSE 错误 chunk `data: {"error":{...}}`。
- 网络错误：`502` 错误 chunk。
- 日志末尾落盘完整原始 SSE。

**非流式**（`stream=false`）— `_collect_stream` 聚合：

- 消费后端 SSE 到 `[DONE]`，按 `choices[].delta` 拼接 `content`；`tool_calls` 按 `index` 累积并拼接分片的 `arguments` 字符串。
- 组装单个 `chat.completion` 对象：新生成 `id`（`chatcmpl-` + 12 字节随机 hex）、`object: "chat.completion"`、`created`、`model`、`finish_reason`（取最后一个非空值，`tool_calls` 存在且无 finish 时兜底 `"tool_calls"`）、`usage`（缺失时补全 0）。
- **注意**：聚合后的 `content` 可能为 `None`（无文本时）；`tool_calls.arguments` 保持 JSON 字符串原样。

---

## 3. `POST /v1/responses` — OpenAI Responses API（Codex CLI 兼容）

### 3.1 请求处理（`create_response`）

1. 鉴权 → 读原始 JSON body。
2. `responses_request_to_chat(payload)`：Responses → Chat 转换（`responses_adapter.py`）：

   - `instructions` → 置顶 `system` 消息。
   - `input` → `messages`：
     - `input` 为字符串 → 单条 user 消息。
     - 列表逐项转换：`user/system/developer` 消息（含 `type:"message"` 变体）→ 对应 Chat 消息（`developer` 映射为 `system`）；`assistant` 消息提取 `output_text` → assistant；`function_call` **合并进前一条 assistant 消息**成为 `tool_calls`；`function_call_output` → `tool` 消息（`tool_call_id=call_id`）；未知类型带 role 则按普通消息处理。
     - 相邻的 `assistant + function_call` 会被 flush 合并为一条带 `tool_calls` 的 assistant 消息。
   - `max_output_tokens` → `max_tokens`（优先），否则透传 `max_tokens`。
   - `tools`：Responses 扁平格式 `{type,name,description,parameters,strict}` → Chat 嵌套格式 `{type:"function", function:{...}}`（`_convert_tools_for_chat`）。
   - 透传：`temperature/top_p/stop/seed/presence_penalty/frequency_penalty/response_format/reasoning_effort/tool_choice`。

3. **投影压缩** `project_responses_chat_body(chat_body)`（`responses_projection.py`）—— 该端点特有的最大处理环节：

   **检测** `_looks_like_agentic_cli`：tools 名与 `AGENTIC_TOOL_NAMES`（`exec_command`/`write_stdin`/`update_plan`/`apply_patch`/`tool_search_tool` 等 10 个）交集，或消息含 harness 标记 → 判定为 agentic CLI 请求。

   - **非 agentic（conservative 模式）**：逐条 `_project_conversation_message`，只做长度裁剪：
     - system → 截断至 1200 字符；user → 3200；assistant → 首尾保留式摘要（1800，`_summarize_free_text`）；tool 输出 → `_summarize_tool_output`（1600）；assistant 的 tool_calls 逐条投影 arguments。
   - **agentic（aggressive 模式）**：重构成最小语义闭包：
     - 丢弃所有 harness system / harness user 消息（`dropped_harness_messages`）。
     - 非 harness system 保留（截断至 1200，最多 2 条合并为 `Additional instructions:`）。
     - 最近尾部保留：从后往前累计 ≤ `MAX_TAIL_MESSAGES=8` 条 且 ≤ `MAX_TAIL_CHARS=7000` 字符（`_choose_tail_start`）；若尾部含 tool 消息，向前扩展包含其对应的 assistant `tool_calls`（`_expand_tail_for_tool_context`）。
     - 最新一条 user 消息若在裁剪区外，单独保留为 anchor（`anchor_user`）。
     - 更早的历史（裁剪区内、除 anchor 外的消息）压缩为规则摘要 `_build_history_summary`：每条约一行（user → `User asked: …`；assistant → `Assistant replied/called tools: …`；tool → `Tool X returned: …` 内联摘要 ≤220 字符），上限 10 条 / 2200 字符，剩余计为 `N earlier messages… further condensed`。
     - 最终结构：`[BASE_SYSTEM_PROMPT, guidance…, history_summary…, anchor_user?, tail…]`。
   - **tools 投影** `_project_tools`（两种模式都执行）：只保留 `name` + `parameters` + `strict`，**丢弃 `description`**；schema 递归裁剪（`_project_schema`）只保留 `SCHEMA_KEEP_KEYS` 白名单键（`type/properties/required/items/enum/oneOf/anyOf/allOf/additionalProperties/format/minimum/maximum/minItems/maxItems/minLength/maxLength/nullable`），深度 ≥6 截为 `{"type":"object"}`，`properties` 逐个递归、`oneOf/anyOf/allOf` 每项递归、列表项 ≤6。
   - **tool 消息/参数压缩**：
     - tool 输出 `_summarize_tool_output`：≤1600 字符且 ≤24 行则原样；否则提取 `Process exited with code` 行 + 前 10 行 + 后 6 行，跳过 `Chunk ID:/Wall time:/Original token count:` 等噪声行，中间计为 omitted。
     - tool arguments `_summarize_tool_arguments`：≤900 字符原样；`apply_patch` 大 payload 直接替换为摘要；JSON 递归收缩（`_shrink_json_value`：深度 ≥4 → `<omitted>`；dict 键 >12 截断；list >6 截断；字符串按 key 截断 120/240）。
     - assistant 文本 `_summarize_free_text`：保留头部一半 + 尾部 1/3，中间 `… [N chars omitted] …`。
   - 返回 `(projected_body, projection_stats)`，stats 含 mode / 消息数与字符数前后对比 / 丢弃条数 / 摘要条数等，写入日志。

4. 脱敏（若开启，同 0.3）→ 补默认值（`model/stream/stream_options`，见 0.4）。

### 3.2 响应处理

**流式** — `_stream_responses` + `ResponsesStreamConverter`：

- 先 `_post_backend_with_filter_retry` 一次性取回后端响应：
  - 若后端返回 200 且内容命中审核特征，且 `desensitize` 开启 + `--no-compact` 模式 → **自动重试**：用 `force_compact=True` 重新脱敏（强制压缩 harness），重发一次，成功则用重试结果。
  - 非 200 → 产出 `data: {"type":"error","error":{...}}` 事件后终止。
- 逐行 `feed_line` 转成 Responses 事件流：
  - 首 chunk → `response.created` + `response.in_progress`（`status: "in_progress"`）。
  - 首个 content delta → `response.output_item.added`（message item）+ `response.content_part.added` + 后续每个 delta → `response.output_text.delta`。
  - tool_calls delta → `response.output_item.added`（function_call item，`call_id` 为后端 id，item 自带新生成的 `fc_` id）+ `response.function_call_arguments.delta`（分片透传）。
  - `finish()` 收尾：`response.output_text.done`、`response.content_part.done`、`response.output_item.done`（message + 各 function_call）、`response.completed`（内含完整 `response` 对象）。
  - usage 映射：`prompt_tokens/completion_tokens/total_tokens` → `input_tokens/output_tokens/total_tokens`，附 `*_tokens_details` 占位 0。

**非流式** — 聚合模式：

- 同样经 `_post_backend_with_filter_retry`（含审核重试），把返回 SSE 全量喂给同一 converter，最后 `get_nonstream_response()` 产出单个 `response` 对象返回。
- `output` 数组：message item（`output_text`）+ function_call items；`parallel_tool_calls: True`；`usage` 同上。

---

## 4. `POST /v1/messages` — Anthropic Messages API（Claude Code / CC Switch 兼容）

### 4.1 请求处理（`create_message`）

1. 鉴权 → 读原始 JSON body；`messages` 为空 → 400。
2. `anthropic_request_to_chat(payload)`（`anthropic_adapter.py`）—— Anthropic → Chat 转换：

   - `system`（字符串或 `[{type:"text",text}]` 数组）→ 首条 `system` 消息（数组时用 `\n` 连接各 text 块）。
   - `messages` 逐条 `_convert_anthropic_message`：
     - `content` 为字符串 → 原样一条。
     - 空 content → 丢弃。
     - `role=user` + content blocks：`text` 块合并 → 一条 user 消息；`tool_result` 块 → 各自生成 `{"role":"tool","tool_call_id","content"}` 消息（content 若是块数组则提取 text 拼接）。
     - `role=assistant` + blocks：`text` 块合并 → content；`tool_use` 块 → `tool_calls`（`id` 缺省生成 `call_` + 随机 hex，`arguments` 为 `input` 的 JSON 字符串）；无文本时 content 置 `None`。
     - 其他角色：提取 text 块合并，为空则丢弃。
   - `tools`：Anthropic 格式 `{name, description, input_schema}` → Chat `{type:"function", function:{name, description, parameters: input_schema}}`；已是 Chat 格式（含 `function` 键）则原样保留。
   - `tool_choice`：dict → `{"type": ..., "function": {"name": ...}}`；字符串 `none/auto/required` 原样，其他字符串包成 function 对象。
   - 透传：`model / max_tokens / temperature / top_p / stop / top_k`。
   - **丢弃**：`metadata`、`thinking` 及未列出的其他字段。

3. 脱敏（若开启，同 0.3）。
4. 补默认值（`model="auto"`、`stream=True`、`stream_options`）。
5. **无投影/压缩层**（区别于 /v1/responses）——除脱敏外无其他裁剪；`anthropic_msgs` 与转换后 `chat_messages` 数量记入日志。

### 4.2 响应处理（`_stream_anthropic` + `AnthropicStreamConverter`）

- 后端非 200 → `event: error` + `data: {"type":"error","error":{...,"type":"api_error"}}`；网络错误 → 502 同格式。
- 逐行 `feed_line` 转 Anthropic SSE 事件流：
  - 首 chunk → `message_start`（`message` 含 `msg_` + 随机 id、`content: []`、usage 占位 0）。
  - content delta → `content_block_start`（`{"type":"text","text":""}`）+ 每个 delta → `content_block_delta`（`{"type":"text_delta","text":…}`），按需开/关块。
  - tool_calls delta → `content_block_start`（`{"type":"tool_use","id","name","input":{}}`）+ `content_block_delta`（`{"type":"input_json_delta","partial_json":…}` 分片透传）。
  - `finish()` 收尾：关闭未关的 text/tool_use 块 → `content_block_stop`；`message_delta`（`stop_reason` 映射：`stop→end_turn`、`tool_calls→tool_use`、`length→max_tokens`、其他→`end_turn`；usage 映射 `prompt_tokens→input_tokens`、`completion_tokens→output_tokens`）→ `message_stop`。
- 非流式：本项目 `/v1/messages` **恒为流式**（`stream` 强制 `True`），无聚合返回路径；`get_nonstream_response()` 存在于 converter 类中（拼装 content blocks：text + tool_use，`tool_use.input` 尝试 JSON.parse arguments，失败则保留字符串），但**当前端点未使用**。

---

## 5. `POST /v1/messages/count_tokens` — Token 计数（stub）

- 鉴权后**直接返回** `{"input_tokens": 0}`，不做任何解析/转换/计数，不触达后端。

---

## 6. `GET /health` — 健康检查

- 返回：`status`、平台、Python 版本、auth 文件路径、模式说明；已加载凭据时附 `credential` 摘要（uid / 昵称 / 企业 / token 过期时间与是否已过期）。
- 无鉴权、无请求体处理。

---

## 7. 各端点处理逻辑对比总表

| 端点 | 请求协议转换 | 请求脱敏(可选) | 请求投影/压缩 | 响应转换 | 响应审核重试 |
|---|---|---|---|---|---|
| `/v1/models` | 无 | 无 | 无 | 静态列表 | 无 |
| `/v1/chat/completions` | 无（原生 chat） | ✅ | 无 | 流式原样转发 / 非流式聚合 | 无 |
| `/v1/responses` | Responses → Chat | ✅ | ✅ 强（投影 + 历史摘要 + schema 收敛 + 工具输出/参数压缩） | Chat SSE → Responses 事件流 | ✅（`--desensitize` + `--no-compact` 时自动重试） |
| `/v1/messages` | Anthropic → Chat | ✅ | 无（仅转换 + 脱敏） | Chat SSE → Anthropic 事件流 | 无 |
| `/v1/messages/count_tokens` | 无 | 无 | 无 | 恒返回 0 | 无 |
| `/health` | 无 | 无 | 无 | 状态信息 | 无 |

### 关键差异点

1. **`/v1/responses` 是处理最重的端点**：独有的投影压缩层（丢弃 harness 消息、历史摘要化、schema 白名单裁剪、工具输出/参数压缩），且是唯一带"审核拦截自动重试"的端点。另外两个 LLM 端点无投影、无重试。
2. **`/v1/chat/completions` 响应是"原样透传"**：流式时后端 SSE 字节级直通，仅做日志统计；非流式时聚合（会丢失流内中间 `finish_reason` 之外的状态，如 reasoning 类 delta 会被丢弃）。
3. **`/v1/messages` 恒流式**：`stream` 被强制为 `True`，客户端传 `stream:false` 也会收到 SSE 事件流；非流式聚合路径未接。
4. **脱敏三件套**（零宽空格 / harness 压缩 / tool 元数据剥离）是三个转换端点共用的可选层，开关统一为 `--desensitize`（配合 `--no-compact`）。
5. **所有后端请求都被强制 `stream=True`** 并补 `stream_options.include_usage`，因为后端只接受流式。
