# Issue #2 — C2 中文空格问题：诊断与验收记录

## 当前结论

- `main` 的代理处理链路没有 `normalize_cjk_spacing()`，也未找到主动在相邻中文字符之间插入空格的代码。
- HTTP/SSE 模拟上游测试已经验证：当上游内容为 `我 爱 中 文`（带空格）时，OpenAI Chat 和 Responses 代理输出保留原始空格，而不会无提示地改写。
- 这只能证明测试样本未被代理改写，**不能证明真实 DeepSeek 上游不会生成多余空格**。Issue #2 没有提供对应的原始上游响应与最终客户端输出的配对样本，因此 C2 仍需真实复现。

## 复现与定位方法

1. 用最短可稳定重现的中文提示在同一模型、同一参数下发送请求；尽量固定 `temperature`、`seed`（若模型支持）。
2. 在本地开发环境临时开启 `--verbose-llm`，记录一次完整上游 SSE 响应以及代理客户端可见 SSE。此选项可能记录用户指令、工具输出和凭证，请仅在受控环境使用；不要把原始日志公开到 Issue 或 PR。
3. 对齐同一请求的上游每个 `choices[].delta.content`（按顺序直接拼接）和代理下行 `choices[].delta.content` / `response.output_text.delta.delta`；注意按完整文本对比，**不要逐 chunk 单独 trim/strip**，否则跨 chunk 的空格判断会失真。
4. 对比时记录：模型名、协议、是否 `--desensitize`、是否 `--optimize-context`、发生异常的请求 ID、是否流式，以及空格出现位置的**脱敏片段**。无需提交完整会话。
5. 判断归属：
   - 上游已产生空格且下行忠实透传 → 模型生成行为，不应由通用代理擅自修改。
   - 上游无空格、下行有空格 → 检查 DSML 解析、SSE 增量拼接、Responses/Anthropic 转换器，补最小回归测试。
   - 下行无空格、客户端展示有空格 → 客户端拼接或渲染问题。

## 修复边界

没有明确样本前**不增加全局 `normalize_cjk_spacing()`**。全局删除中文间空格会改变模型原始输出、Markdown、代码、字符串常量等内容；如果将来确认是代理错误，应该针对出现问题的处理分支修复而非对最终文本盲目替换。

相关测试：`test_stream_error_propagation.py::test_chat_stream_keeps_exact_cjk_spacing`、`test_responses_success_stream_emits_completed_and_closes_connection`。
