# CodeBuddy API Proxy

> A lightweight API proxy service that converts CodeBuddy's underlying interface into standard OpenAI, Anthropic, and Responses protocol formats.

> **中文版文档见 [README_zh.md](README_zh.md).**

## ✨ Core Features

- **Protocol conversion** - Supports three standard formats: OpenAI Chat Completions, Anthropic Messages API, and Responses
- **Desensitization** - Built-in smart desensitization module that automatically filters sensitive information (accounts, passwords, keys, brand terms, paths, etc.) to mitigate review-based false blocks
- **Message compression** - Intelligently compresses historical messages to dramatically reduce token usage (ideal for long-context scenarios such as Codex CLI)
- **Tool call support** - Full support for function calling and tool use, with automatic filtering of invalid tool definitions
- **DSML parsing** - Automatically detects and converts DeepSeek Markup Language (DSML) tool calls
- **Streaming responses** - SSE streaming output, returning generated content in real time, with built-in 60-second timeout protection
- **Multi-account management** - Supports isolation of multiple login states for easy switching between work/personal accounts

---

## Installation

Recommended to run from PyPI using [uv](https://docs.astral.sh/uv/):

```bash
# Install uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# Run the latest available version (uv automatically creates the environment and installs dependencies)
uv run --with workbuddy2api python -m codebuddy_proxy \
  --log-file "$HOME/.workbuddy2api/codebuddy-proxy.jsonl"

# Force refresh the cache and run the latest version
uv run --refresh-package workbuddy2api --with workbuddy2api \
  python -m codebuddy_proxy \
  --log-file "$HOME/.workbuddy2api/codebuddy-proxy.jsonl"
```

No need to manually activate the virtual environment on subsequent starts; just repeat the `uv run` command above.

### Running from local source

Run the following commands from the project root to use the workspace source code instead of the published PyPI version:

```bash
# Sync local project dependencies
uv sync

# Start the local source
uv run python -m codebuddy_proxy --desensitize
```

On first use, when login is required:

```bash
uv run python -m codebuddy_proxy --login --desensitize
```

## Quick Start

### 1. Start the proxy

```bash
# Use the latest version (recommended)
uv run --with workbuddy2api python -m codebuddy_proxy --desensitize

# First use: log in and start
uv run --with workbuddy2api python -m codebuddy_proxy --login --desensitize
```

Listens on `http://127.0.0.1:8787` by default.

### 2. Verify

```bash
curl http://127.0.0.1:8787/health
curl http://127.0.0.1:8787/v1/models
```

### 3. Connect clients

#### Codex CLI

Edit `~/.codex/config.toml`:

```toml
[model_providers.codebuddy]
name = "CodeBuddy (via local proxy)"
base_url = "http://127.0.0.1:8787/v1"
wire_api = "responses"

[profiles.codebuddy]
model = "glm-5.2"
model_provider = "codebuddy"
```

Usage:

```bash
codex --profile codebuddy "your task"
```

#### Claude Code + CC Switch

Add the following to your CC Switch configuration:

```json
{
  "DeepSeek-V4": {
    "base_url": "http://127.0.0.1:8787/v1/messages",
    "api_key": "",
    "model": "deepseek-v4-pro"
  }
}
```

#### OpenCode

Edit `opencode.json` in the project root:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "model": "codebuddy/glm-5.2",
  "providers": {
    "codebuddy": {
      "name": "CodeBuddy (via local proxy)",
      "package": "@opencode-ai/ai/providers/openai-compatible",
      "settings": {
        "baseURL": "http://127.0.0.1:8787/v1",
        "apiKey": "noop"
      },
      "models": {
        "glm-5.2": { "modelID": "glm-5.2", "name": "GLM-5.2" },
        "deepseek-v4-pro": { "modelID": "deepseek-v4-pro", "name": "DeepSeek V4 Pro" },
        "kimi-k2.7": { "modelID": "kimi-k2.7", "name": "Kimi K2.7" }
      }
    }
  }
}
```

After launching opencode, use the `/models` command to select a model under the `codebuddy` provider (e.g. `codebuddy/glm-5.2`).

> Note: `baseURL` points to the local proxy; `apiKey` can be any placeholder value (the local proxy does not validate keys). The `models` keys are the model IDs used inside OpenCode (for selection), while `modelID` is the actual model name sent to the proxy. Use `apiKey` for the key field (not `env_key` from some older templates), to avoid binding the wrong provider semantics.

#### Grok CLI

Edit `~/.grok/config.toml` and add a `[model.<name>]` entry per model that points at the local proxy. Grok uses the OpenAI Chat Completions backend (`/v1/chat/completions`) by default, which this proxy supports:

```toml
[models]
default = "hy3"   # optional: set your default model

[model.hy3]
model = "hy3"                        # model id sent to the proxy
base_url = "http://127.0.0.1:8787/v1"
name = "HY3 Main"                    # shown in the model picker
api_key = "noop"                     # any placeholder value works

[model.dv4f]
model = "deepseek-v4-flash"
base_url = "http://127.0.0.1:8787/v1"
name = "DeepSeek V4 Flash"
api_key = "noop"
```

Then switch to the proxy model in the TUI with `/model hy3` (or `Ctrl+M` model picker), or run headless with `grok -m hy3 "your task"`.

> Note: `base_url` points to the local proxy; `api_key` can be any placeholder value (the local proxy does not validate keys). You can also set `api_backend = "responses"` to use the `/v1/responses` endpoint, or `"messages"` for the Anthropic `/v1/messages` endpoint, depending on your needs.

#### Other OpenAI-compatible clients

- Base URL: `http://127.0.0.1:8787/v1`
- API Key: leave blank (or use the value you set with `--api-key` at startup)
- Model name: `glm-5.2` / `deepseek-v4-pro` / `kimi-k2.7` / `auto`, etc.

## Command-line arguments

```bash
--host HOST              Bind address (default 127.0.0.1)
--port PORT              Bind port (default 8787)
--endpoint ENDPOINT      CodeBuddy backend address
--session-file PATH      Session file path (default ~/.codebuddy-session.json)
--log-file PATH          JSONL log file (default ~/.workbuddy2api/codebuddy-proxy.jsonl)
--desensitize            Enable desensitization (recommended)
--optimize-context       Enable message compression (recommended for Codex CLI)
--login                  Perform browser login at startup
--no-browser             Do not open the browser on login
--verbose-llm            Log full LLM request/response content
                         (default: summary only, saves 98% space)
--mock-dir DIR           Use mock data (for testing)
```

### Environment variables

```bash
CODEBUDDY_PROXY_HOST      # Same as --host
CODEBUDDY_PROXY_PORT      # Same as --port
CODEBUDDY_ENDPOINT        # Same as --endpoint
CODEBUDDY_PROXY_LOG_FILE  # Same as --log-file
```

## Common scenarios

### First use (login required)

```bash
uv run --with workbuddy2api python -m codebuddy_proxy --login \
  --log-file "$HOME/.workbuddy2api/codebuddy-proxy.jsonl"
```

After the browser opens and you log in, the proxy starts automatically.

### Daily use (automatically reads the login state)

```bash
uv run --with workbuddy2api python -m codebuddy_proxy --desensitize \
  --log-file "$HOME/.workbuddy2api/codebuddy-proxy.jsonl"
```

### Codex CLI scenario (with compression enabled)

```bash
uv run --with workbuddy2api python -m codebuddy_proxy --desensitize --optimize-context \
  --log-file "$HOME/.workbuddy2api/codebuddy-proxy.jsonl"
```

### Multi-account switching

```bash
# Account 1
uv run --with workbuddy2api python -m codebuddy_proxy --session-file ~/.codebuddy-work.json --login \
  --log-file "$HOME/.workbuddy2api/codebuddy-proxy.jsonl"

# Account 2
uv run --with workbuddy2api python -m codebuddy_proxy --session-file ~/.codebuddy-personal.json --login \
  --log-file "$HOME/.workbuddy2api/codebuddy-proxy.jsonl"
```

### Listening on all interfaces (LAN sharing)

```bash
uv run --with workbuddy2api python -m codebuddy_proxy --host 0.0.0.0 --desensitize \
  --log-file "$HOME/.workbuddy2api/codebuddy-proxy.jsonl"
```

## API endpoints

All endpoints do not require an extra token in the request by default; the proxy authenticates using the local session.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | Query local service and auth status |
| GET | `/v1/models` | Query the CodeBuddy model list |
| POST | `/v1/chat/completions` | OpenAI Chat Completions, supports tools and streaming |
| POST | `/v1/responses` | Responses API, compatible with Codex CLI |
| POST | `/v1/messages` | Anthropic Messages API, compatible with Claude Code / CC Switch |

### `/health` - health check

```bash
curl http://127.0.0.1:8787/health
```

Example response:

```json
{
  "status": "ok",
  "uptime_seconds": 123.45,
  "authenticated": true,
  "token_valid": true
}
```

### `/v1/models` - model list

```bash
curl http://127.0.0.1:8787/v1/models
```

Returns a model list in OpenAI format; `data[].id` is the `model` value used in subsequent requests (e.g. `deepseek-v4-flash`, `glm-5.2`).

### `/v1/chat/completions` - OpenAI Chat

**Non-streaming request:**

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "deepseek-v4-flash",
    "messages": [{"role": "user", "content": "Write a quicksort"}]
  }'
```

**Streaming request:**

```bash
curl -N http://127.0.0.1:8787/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "glm-5.2",
    "stream": true,
    "messages": [{"role": "user", "content": "hi"}]
  }'
```

Supports the full set of OpenAI features, including `tools`, `tool_choice`, and `stream_options`.

### `/v1/responses` - Responses API

Used for Codex CLI compatibility:

```bash
curl http://127.0.0.1:8787/v1/responses \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "default",
    "input": "Write a quicksort"
  }'
```

Supports `instructions` (system prompt), message-form `input`, `tools`, `tool_choice`, and `stream`.

**💡 Tip:** Using `--optimize-context` dramatically reduces token usage for Codex CLI.

### `/v1/messages` - Anthropic Messages

Used for Claude Code / CC Switch compatibility:

```bash
curl http://127.0.0.1:8787/v1/messages \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "deepseek-v4-pro",
    "max_tokens": 4096,
    "messages": [{"role": "user", "content": "hi"}]
  }'
```

Setting `"stream": true` returns an Anthropic SSE event stream.

## Advanced features

### Desensitization (`--desensitize`)

Inserts zero-width spaces (U+200B) into sensitive words in system messages, breaking the backend's keyword matching and mitigating compliance templates being falsely blocked by review.

#### When to use it

**Scenarios where enabling is strongly recommended:**

1. **Integrating with Claude Code / CC Switch**
   - Claude Code's system prompt contains many Anthropic brand terms and security compliance statements
   - The Tencent backend may treat competing brand terms ("Claude", "Anthropic") as sensitive content
   - Without desensitization, almost every request gets blocked by review

2. **Integrating with agentic tools such as Codex CLI / Oh My Posh**
   - These tools' system prompts contain a large number of security terms (DoS, exploit, credential testing, etc.)
   - Even compliant "refuse harmful requests" statements can be falsely blocked by keyword matching

3. **Using custom system prompts that contain security terms**
   - Compliance conversations related to security research and penetration testing
   - Generating technical documentation that needs to discuss vulnerabilities and attack defenses

**Typical error message:**
```json
{
  "error": {
    "message": "内容违规",
    "type": "content_policy_violation"
  }
}
```
Or the backend returns an empty response / connection drops.

**Scenarios where you don't need it:**
- ✅ Normal conversation (no security terms)
- ✅ Using the official CodeBuddy client (handling is built in)
- ✅ Pure code generation (no brand terms / security statements)

#### Typical use cases

**Case 1: Integrating with Claude Code**

```bash
# --desensitize is required, otherwise almost every request is blocked
uv run --with workbuddy2api python -m codebuddy_proxy --desensitize \
  --log-file "$HOME/.workbuddy2api/codebuddy-proxy.jsonl"

# Configure in Claude Code / CC Switch
# Base URL: http://127.0.0.1:8787/v1/messages
```

**Case 2: Integrating with Codex CLI**

```bash
# Enable both desensitization and message compression (best configuration)
uv run --with workbuddy2api python -m codebuddy_proxy --desensitize --optimize-context \
  --log-file "$HOME/.workbuddy2api/codebuddy-proxy.jsonl"

# In the Codex CLI config file
# base_url: http://127.0.0.1:8787/v1/responses
```

**Case 3: Security research conversation**

```bash
# Enable desensitization to avoid compliance terms being falsely blocked
uv run --with workbuddy2api python -m codebuddy_proxy --desensitize \
  --log-file "$HOME/.workbuddy2api/codebuddy-proxy.jsonl"

# Example request
curl http://127.0.0.1:8787/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "deepseek-v4-pro",
    "messages": [
      {
        "role": "system",
        "content": "You are a security expert. Refuse requests for exploit development."
      },
      {
        "role": "user",
        "content": "Explain defenses against SQL injection"
      }
    ]
  }'
```

#### How it works

```python
# Original
"Refuse requests for DoS attacks and exploit development."

# Desensitized (zero-width space U+200B inserted)
"Refuse requests for Do​S a​ttacks and e​xploit development."
# Human/model: looks exactly the same
# Backend review: keyword matching fails
```

#### Scope of processing

- ✅ `system` role messages (default)
- ✅ `developer` role messages
- ✅ Harness user messages injected by Codex CLI / Claude Code
- ✅ `description` field of `tools`
- ❌ `user`/`assistant` messages (kept as-is, so normal conversation is unaffected)

#### Sensitive word list

Roughly 80 security/compliance terms:
- Attack types: DoS, DDoS, exploit, SQL injection, XSS, malware...
- Security terms: vulnerability, penetration testing, privilege escalation...
- Brand terms: Claude Code, Anthropic (to avoid competing brands triggering review)

The full list is in `SENSITIVE_TERMS` in `desensitize.py`.

#### Notes

- ✅ Only processes compliance statements; does not bypass review of harmful input
- ✅ Only modifies system messages; real user input is kept as-is
- ⚠️ Zero-width spaces are transparent to humans/models but affect exact string matching
- ⚠️ Performance cost: <1ms (regex replacement)

---

### Message compression (`--optimize-context`)

Only applies to the `/v1/responses` endpoint, compressing long histories, large schemas, and oversized tool outputs into a "minimal semantic closure", dramatically reducing token usage (possibly 60-90%).

#### When to use it

- ✅ Using agentic tools such as Codex CLI / Claude Code (long histories)
- ✅ High token usage (>100k/day)
- ✅ Frequently hitting "context" errors
- ✅ Sending the full history on every request
- ❌ Not for short conversations / simple requests

#### Usage

```bash
# Enable message compression
uv run --with workbuddy2api python -m codebuddy_proxy --optimize-context \
  --log-file "$HOME/.workbuddy2api/codebuddy-proxy.jsonl"

# Enable both features (recommended for Codex CLI)
uv run --with workbuddy2api python -m codebuddy_proxy --desensitize --optimize-context \
  --log-file "$HOME/.workbuddy2api/codebuddy-proxy.jsonl"
```

#### How it works

##### Conservative mode (non-agentic requests)

Only length trimming:
- System → truncated to 1200 characters
- User → 3200 characters
- Assistant → keep a head/tail summary (1800)
- Tool output → compressed to 1600 characters

##### Aggressive mode (agentic CLI requests)

Automatically detects agentic requests (tools containing `exec_command`, `apply_patch`, etc., or messages containing harness markers) and reconstructs them into a minimal semantic closure:

1. **Drop harness messages** — remove all Codex/Claude Code injected system/user messages
2. **Keep recent context** — keep ≤8 messages / ≤7000 chars from the tail
3. **Summarize history** — compress earlier history into rule summaries (one line each)
4. **Schema convergence** — keep only structural fields, drop descriptions (the biggest space consumers)
5. **Compress tool output/arguments** — keep key parts, omit the rest

#### Example effect

```
Original request:
  - Messages: 50, 120,000 characters
  - Tools: 15, 45,000 characters
  - Total: ~165,000 characters (~40k tokens)

After compression:
  - Messages: 12, 18,000 characters
  - Tools: 15, 8,000 characters
  - Total: ~26,000 characters (~6k tokens)

Savings: ~85% tokens
```

#### Log verification

Once enabled, the log records compression statistics:

```bash
grep projection_applied "$HOME/.workbuddy2api/codebuddy-proxy.jsonl" | jq .
```

Example output:

```json
{
  "event": "projection_applied",
  "protocol": "responses",
  "mode": "aggressive",
  "original_messages": 50,
  "projected_messages": 12,
  "original_message_chars": 120000,
  "projected_message_chars": 18000,
  "dropped_harness_messages": 8
}
```

#### Notes

- ✅ Only used for `/v1/responses`; does not affect the chat/messages endpoints
- ✅ Preserves the semantic closure; the model can still reason
- ⚠️ History is summarized; precise details require re-running tools to retrieve
- ⚠️ Schema is trimmed; auxiliary info such as descriptions is lost
- ⚠️ Performance cost: <10ms (traversal + compression)

---

### Logging

Logs include:
- Text log: `$HOME/.workbuddy2api/proxy.log` (rotated daily, retained 30 days)
- Structured log: `$HOME/.workbuddy2api/codebuddy-proxy.jsonl` (rotated daily, retained 30 days, full request/response)

Each JSONL record contains `app_version`, `system_version`, `python_version`, and `machine` fields; a `startup` event is also recorded at launch.

You can also specify an absolute path for the log file:

```bash
uv run --with workbuddy2api python -m codebuddy_proxy \
  --desensitize \
  --log-file "$HOME/.workbuddy2api/codebuddy-proxy.jsonl"
```

Viewing logs:

```bash
# Follow in real time
tail -f "$HOME/.workbuddy2api/codebuddy-proxy.jsonl"

# View streaming events
tail -100 "$HOME/.workbuddy2api/codebuddy-proxy.jsonl" | jq 'select(.event | startswith("stream"))'

# Count timeouts
jq 'select(.event=="stream_timeout")' "$HOME/.workbuddy2api/codebuddy-proxy.jsonl" | wc -l

# Verify desensitization
grep desensitize_applied "$HOME/.workbuddy2api/codebuddy-proxy.jsonl"

# Verify compression (view statistics)
grep projection_applied "$HOME/.workbuddy2api/codebuddy-proxy.jsonl" | jq .
```

## Troubleshooting

### Session file not found

First use requires login:

```bash
uv run --with workbuddy2api python -m codebuddy_proxy --login \
  --log-file "$HOME/.workbuddy2api/codebuddy-proxy.jsonl"
```

### 401 authentication failure

Token expired; log in again:

```bash
uv run --with workbuddy2api python -m codebuddy_proxy --login \
  --log-file "$HOME/.workbuddy2api/codebuddy-proxy.jsonl"
```

### Review blocking

Enable desensitization:

```bash
uv run --with workbuddy2api python -m codebuddy_proxy --desensitize \
  --log-file "$HOME/.workbuddy2api/codebuddy-proxy.jsonl"
```

If still blocked, try compression (`/v1/responses` only):

```bash
uv run --with workbuddy2api python -m codebuddy_proxy --desensitize --optimize-context \
  --log-file "$HOME/.workbuddy2api/codebuddy-proxy.jsonl"
```

### Port already in use

```bash
lsof -i :8787
uv run --with workbuddy2api python -m codebuddy_proxy --port 8788 \
  --log-file "$HOME/.workbuddy2api/codebuddy-proxy.jsonl"
```

### SOCKS proxy errors

The `httpx[socks]` dependency is installed automatically. If problems persist, check the environment variables:

```bash
env | grep -i proxy
```

Temporarily disable the proxy:

```bash
unset http_proxy https_proxy all_proxy
uv run --with workbuddy2api python -m codebuddy_proxy \
  --log-file "$HOME/.workbuddy2api/codebuddy-proxy.jsonl"
```

## Technical details

- **Architecture**: FastAPI + httpx (async)
- **Concurrency**: supports 1000+ concurrent requests
- **Timeouts**: connect 10 seconds, read 30 seconds
- **Streaming**: full streaming logs (started / progress / completed / timeout)

## Disclaimer

**This project is for learning and research purposes only. Please comply with CodeBuddy's Terms of Service.**

- This project provides no warranty of any kind
- Any consequences arising from the use of this project are the sole responsibility of the user
- Do not use this project for any purpose that violates CodeBuddy's Terms of Service
- Do not use this project for commercial purposes
