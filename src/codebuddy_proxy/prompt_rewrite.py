"""Optional prompt rewriting and bounded, account-scoped degradation.

Behavior reference: Buddy Switch ebcaead outbound/{prompt,sanitize}. Python
implementation composes with the proxy's existing harness compaction.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
from importlib.resources import files
import re
import threading
import time

# Buddy's default prompt is attributed there to the MIT workbuddy2api reference.
DEFAULT_PROMPT = files("codebuddy_proxy").joinpath("default_prompt.md").read_text(encoding="utf-8")
DEGRADED_PROMPT = "You are a helpful assistant. Respond in the user's language, follow the user's instructions, and be direct and concise."
REWRITES = (
    ("You are Claude Code, Anthropic's official CLI for Claude", "You are Claude Code, Anthropic's official CLI tool for Claude"),
    ("Main branch (you will usually use this for PRs)", "Default branch (you will usually use this for PRs)"),
    ("You are a coding agent running in the Codex CLI, a terminal-based coding assistant.", "You are a coding agent running in the Codex CLI tool, a terminal-based coding assistant."),
    ("To give feedback, users should report the issue at https://github.com/anthropics/claude-code/issues", "To provide feedback, users should report the issue at https://github.com/anthropics/claude-code/issues"),
    ("11128", "11-128"),
)
IDENTITY = re.compile(r"you are an ai agent powered by deepseek harness", re.I)
BILLING = re.compile(r"x-anthropic-billing-header:[^;\n]*;?\s*", re.I)
BARE_BILLING = re.compile(r"x-anthropic-billing-header", re.I)
KV = re.compile(r"\bcc_[a-z0-9_]+=[^;\n]*;?\s*", re.I)


def sanitize_text(value: str) -> str:
    if not (IDENTITY.search(value) or BARE_BILLING.search(value)
            or any(marker in value for marker in ("cc_entrypoint=", "You are Claude Code", "Main branch (", "You are a coding agent running in the Codex CLI", "github.com/anthropics/", "11128"))):
        return value
    for old, new in REWRITES:
        value = value.replace(old, new)
    value = IDENTITY.sub("You are an AI agent powered by the DeepSeek Harness", value)
    value = BILLING.sub("", value)
    previous = None
    while previous != value:
        previous, value = value, KV.sub("", value)
    return BARE_BILLING.sub("x-anthropic-billing-hdr", value).strip()


def sanitize_fingerprints(body: dict) -> tuple[dict, bool]:
    result = deepcopy(body)
    changed = False
    for message in result.get("messages") or []:
        if not isinstance(message, dict):
            continue
        for field in ("content", "reasoning_content"):
            value = message.get(field)
            if isinstance(value, str):
                message[field] = sanitize_text(value)
            elif field == "content" and isinstance(value, list):
                for part in value:
                    if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                        part["text"] = sanitize_text(part["text"])
        for call in message.get("tool_calls") or []:
            fn = call.get("function") if isinstance(call, dict) else None
            if isinstance(fn, dict):
                if isinstance(fn.get("arguments"), str):
                    try:
                        arguments = json.loads(fn["arguments"])
                    except (ValueError, TypeError):
                        # Preserve malformed input for the upstream validator.
                        continue
                    cleaned = _sanitize_argument(arguments)
                    if cleaned != arguments:
                        fn["arguments"] = json.dumps(cleaned, ensure_ascii=False, separators=(",", ":"))
    changed = result != body
    return result, changed


def _sanitize_argument(value):
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, list):
        return [_sanitize_argument(item) for item in value]
    if isinstance(value, dict):
        return {key: _sanitize_argument(item) for key, item in value.items()}
    return value


class PromptRewriter:
    def __init__(self, mode: str = "passthrough", path: str | Path | None = None):
        if mode not in ("passthrough", "custom"):
            raise ValueError("prompt mode must be passthrough or custom")
        if path and mode != "custom":
            raise ValueError("--prompt-file requires --prompt-mode custom")
        self.mode = mode
        self.text = Path(path).read_text(encoding="utf-8") if path else DEFAULT_PROMPT
        if mode == "custom" and not self.text.strip():
            raise ValueError("custom prompt must not be empty")
        self._until: dict[str, float] = {}
        self._lock = threading.Lock()

    def active(self, scope: str, now: float | None = None) -> bool:
        with self._lock:
            return self._until.get(scope, 0) > (time.time() if now is None else now)

    def trigger(self, scope: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._lock:
            self._until = {key: until for key, until in self._until.items() if until > now}
            if self._until.get(scope, 0) > now:
                return False
            midnight = (datetime.fromtimestamp(now, timezone(timedelta(hours=8))) + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            self._until[scope] = midnight.timestamp()
            return True

    def apply(self, body: dict, scope: str) -> dict:
        replacement = self.text if self.mode == "custom" else (DEGRADED_PROMPT if self.active(scope) else None)
        if replacement is None:
            return body
        result = deepcopy(body)
        result["messages"] = [{"role": "system", "content": replacement}] + [
            message for message in (result.get("messages") or [])
            if not isinstance(message, dict) or message.get("role") not in ("system", "developer")
        ]
        return result


def should_degrade(status: int, message: str, sanitized: bool) -> bool:
    lower = message.lower()
    channel = any(marker in lower for marker in ("conversation id missing", "channel verification", "unapproved channel", "渠道校验"))
    content = not channel and ((status == 400 and ("11128" in message or "content_blocked" in message))
                               or "content policy" in lower or "内容审核" in message)
    return content or (status == 400 and sanitized and channel)
