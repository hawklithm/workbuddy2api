"""Upstream chat-role compatibility for legacy CodeBuddy gateways.

The OpenAI Chat Completions input supports a developer role, but the domestic
CodeBuddy chat gateway currently rejects its presence with HTTP 400/code 11128.
Map only this unsupported role to system for that specific upstream, preserving
content, relative order, metadata and tool-call history.

This is a best-effort compatibility conversion: older upstreams cannot
represent the separate system > developer > user instruction hierarchy.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit


def uses_legacy_chat_roles(backend_key: str, upstream_url: str) -> bool:
    """Only the verified domestic upstream needs developer -> system mapping.

    In particular, do not force this behavior on custom endpoints or the
    international CodeBuddy gateway without independent compatibility tests.
    """
    try:
        host = (urlsplit(upstream_url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return backend_key == "domestic" and host == "copilot.tencent.com"


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
