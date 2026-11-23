"""WorkBuddy wire compatibility, based on Buddy Switch ebcaead.

Pure payload rules and a chat-only identity builder. Auth/login headers are
deliberately owned by CodeBuddyClient and are not changed by this module.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import plistlib
import re
import sys

EFFORTS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
CN_EFFORTS = {
    "deepseek-v4-flash": ("low", "high", "max"),
    "deepseek-v4.1-flash": ("low", "high", "max"),
    "deepseek-v4-pro": ("low", "high", "xhigh"),
    "hy4-preview": ("high",), "hy4-preview-x": ("high",),
    "hy3": ("low", "high"), "hy3-x": ("low", "high"),
    "glm-5.3": ("low", "high", "max"), "glm-5.3-flash": ("low", "high", "max"),
    "glm-5.2": ("high", "xhigh"), "glm-5.1": ("medium",),
    "glm-5v-turbo": ("medium",), "kimi-k3-1": ("medium",),
    "kimi-k2.7": ("medium",), "kimi-k2.6": ("medium",), "minimax-m3": ("medium",),
}
GLOBAL_EFFORTS = {
    "fast-model": ("medium",), "balanced-model": ("medium",), "primary-model": ("high",),
    "hy4-preview-f": ("high",), "hy3": ("low", "high"), "deepseek-v4.1-flash": ("high",),
    **{m: ("low", "medium", "high", "xhigh", "max") for m in
       ("gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna")},
    **{m: ("low", "medium", "high", "xhigh") for m in ("gpt-5.5", "gpt-5.4")},
    "gpt-5.3-codex": ("medium",), "gemini-3.5-flash": ("medium",),
    "glm-5.3": ("low", "high", "max"), "glm-5.2": ("high", "xhigh"),
    "kimi-k3": ("medium",), "kimi-k2.6": ("medium",),
}


def normalize_deepseek(body: dict, backend: str, defaults: dict[str, str] | None = None) -> None:
    model = str(body.get("model") or "").strip()
    if not model.lower().startswith("deepseek"):
        configured = (defaults or {}).get(model)
        table = GLOBAL_EFFORTS if backend == "global" else CN_EFFORTS
        if configured and model in table and "reasoning_effort" not in body and "reasoningEffort" not in body:
            rank = EFFORTS.index(configured.strip()) if configured.strip() in EFFORTS else None
            if rank is not None:
                lower = [effort for effort in table[model] if EFFORTS.index(effort) <= rank]
                body["reasoning_effort"] = max(lower, key=EFFORTS.index) if lower else min(table[model], key=EFFORTS.index)
        return
    thinking = body.get("thinking")
    kind = str(thinking.get("type") or "").strip() if isinstance(thinking, dict) else ""
    if kind == "disabled":
        body.pop("reasoning_effort", None)
        body.pop("reasoningEffort", None)
    else:
        if not kind:
            body["thinking"] = {"type": "enabled"}
        if "reasoning_effort" not in body and "reasoningEffort" not in body:
            body["reasoning_effort"] = (defaults or {}).get(model, "high") or "high"
        key = "reasoning_effort" if isinstance(body.get("reasoning_effort"), str) else "reasoningEffort"
        requested = body.get(key)
        supported = (GLOBAL_EFFORTS if backend == "global" else CN_EFFORTS).get(model, ())
        if isinstance(requested, str) and requested.strip() in EFFORTS and supported:
            rank = EFFORTS.index(requested.strip())
            lower = [effort for effort in supported if EFFORTS.index(effort) <= rank]
            body[key] = max(lower, key=EFFORTS.index) if lower else min(supported, key=EFFORTS.index)
    messages = body.get("messages") or []
    if any(isinstance(m, dict) and ("reasoning_content" in m or isinstance(m.get("reasoning"), str)
                                  and bool(m["reasoning"])) for m in messages):
        for message in messages:
            if isinstance(message, dict) and message.get("role") == "assistant" and "reasoning_content" not in message:
                message["reasoning_content"] = message.get("reasoning") if isinstance(message.get("reasoning"), str) else ""


def resolve_identity(backend: str) -> tuple[str, str | None]:
    """Installed macOS bundle, saved region version, then Buddy's fallback.

    As in Buddy, Windows/Linux installation discovery is not assumed. Explicit
    env versions also support packaged/headless deployments.
    """
    version = None
    cli = None
    pattern = r"\d+\.\d+\.\d+(?:\.\d+)?"
    override = os.getenv("WORKBUDDY_AI_VERSION" if backend == "global" else "WORKBUDDY_VERSION")
    if override:
        if not re.fullmatch(pattern, override):
            raise ValueError("Invalid WorkBuddy version")
        version = override
    if not version and sys.platform == "darwin":
        for root in (Path("/Applications"), Path.home() / "Applications"):
            bundle = root / ("WorkBuddy AI.app" if backend == "global" else "WorkBuddy.app")
            try:
                with (bundle / "Contents/Info.plist").open("rb") as handle:
                    installed = plistlib.load(handle).get("CFBundleShortVersionString", "")
                if not re.fullmatch(pattern, installed):
                    continue
                version = installed
                try:
                    package = json.loads((bundle / "Contents/Resources/app.asar.unpacked/cli/package.json").read_text())
                    candidate = package.get("version")
                    if not candidate or candidate == "0.0.0":
                        candidate = package.get("publishConfig", {}).get("customPackage", {}).get("version")
                    if isinstance(candidate, str) and re.fullmatch(r"\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?", candidate):
                        cli = candidate
                except (OSError, ValueError, TypeError, AttributeError):
                    pass
                break
            except (OSError, ValueError, TypeError):
                continue
    if not version:
        region = "global" if backend == "global" else "cn"
        try:
            saved = json.loads((Path.home() / ".buddy-switch" / f"app_version.{region}.json").read_text()).get("version")
            if isinstance(saved, str) and re.fullmatch(pattern, saved):
                version = saved
        except (OSError, ValueError, AttributeError):
            pass
    return version or ("5.5.2" if backend == "global" else "5.5.6"), cli


def chat_headers(auth: dict[str, str], backend: str) -> dict[str, str]:
    version, cli = resolve_identity(backend)
    product = "WorkBuddy AI" if backend == "global" else "WorkBuddy"
    ua = f"WorkBuddy/{version} {product}/{version}" + (f" CLI/{cli}" if cli else "")
    origin = "https://www.workbuddy.ai" if backend == "global" else "https://www.codebuddy.cn"
    headers = {
        "User-Agent": ua, "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Accept-Language": "en-US" if backend == "global" else "zh-CN",
        "Origin": origin, "Referer": origin + "/",
        "X-CodeBuddy-Request": "1", "X-Agent-Purpose": "conversation",
        "X-Requested-With": "XMLHttpRequest", "X-Product": "SaaS",
        "Authorization": auth.get("Authorization", "Bearer "),
    }
    for field, missing in (("X-User-Id", "X-No-User-Id"), ("X-Enterprise-Id", "X-No-Enterprise-Id"),
                           ("X-Domain", "X-No-Department-Info")):
        headers[field if auth.get(field) else missing] = auth.get(field) or "1"
    return headers
