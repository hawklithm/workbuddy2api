"""Upstream chat-role compatibility for the built-in CodeBuddy gateways.

The OpenAI Chat Completions input supports a developer role, but the built-in
domestic and international CodeBuddy chat gateways reject it. Map only this
unsupported role to system for those exact gateways, preserving content,
relative order, metadata and tool-call history.

This is a best-effort compatibility conversion: older upstreams cannot
represent the separate system > developer > user instruction hierarchy.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit


_LEGACY_CHAT_HOSTS = {
    "domestic": "copilot.tencent.com",
    "global": "www.codebuddy.ai",
}


def uses_legacy_chat_roles(backend_key: str, upstream_url: str) -> bool:
    """Return whether the selected built-in gateway needs developer mapping.

    Only exact backend/host pairs are matched; other hosts and mismatched
    backend/host pairs keep their original message roles.
    """
    try:
        host = (urlsplit(upstream_url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return _LEGACY_CHAT_HOSTS.get(backend_key) == host


def adapt_developer_roles(messages: list[Any]) -> tuple[list[Any], int]:
    """Return a copy-on-write messages list and the number of mapped roles.

    Do not merge/reorder messages. Merging can disrupt assistant tool-call /
    tool-result groups; copying only changed message dicts also guarantees the
    downstream request is not mutated.
    """
    converted: list[Any] | None = None
    mapped_count = 0

    for i, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") != "developer":
            continue
        if converted is None:
            converted = list(messages)
        converted[i] = {**message, "role": "system"}
        mapped_count += 1

    return (converted if converted is not None else messages), mapped_count
