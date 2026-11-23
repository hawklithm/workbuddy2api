"""Per-request, per-choice reasoning snapshot-to-delta normalization.

Buddy Switch ebcaead protocol/reasoning.rs defines prefix snapshots and true
deltas. No fuzzy substring matching: an unrelated fragment is real output.
"""
from __future__ import annotations
from copy import deepcopy


class ReasoningNormalizer:
    def __init__(self):
        self.sent: dict[int, str] = {}
        self.roles: set[int] = set()

    def normalize(self, chunk: dict) -> dict | None:
        if chunk.get("error") or "choices" not in chunk:
            return chunk
        result = deepcopy(chunk)
        meaningful = result.get("usage") is not None
        for position, choice in enumerate(result.get("choices") or []):
            index = choice.get("index", position)
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                meaningful = meaningful or bool(choice.get("finish_reason"))
                continue
            if not delta.get("tool_calls"):
                delta.pop("tool_calls", None)
            for key in ("extra_fields", "function_call"):
                if delta.get(key) is None:
                    delta.pop(key, None)
            if delta.get("refusal") == "":
                delta.pop("refusal")
            if "reasoning_content" not in delta and isinstance(delta.get("reasoning"), str):
                delta["reasoning_content"] = delta.pop("reasoning")
            fragment = delta.get("reasoning_content")
            if isinstance(fragment, str):
                sent = self.sent.get(index, "")
                added = fragment[len(sent):] if fragment.startswith(sent) else ("" if sent.startswith(fragment) else fragment)
                if added:
                    self.sent[index] = sent + added
                    delta["reasoning_content"] = added
                    if delta.get("content") == "":
                        delta.pop("content")
                else:
                    delta.pop("reasoning_content", None)
            useful = any(delta.get(key) for key in ("role", "content", "reasoning_content", "tool_calls", "function_call", "refusal"))
            if useful and index not in self.roles and not delta.get("role"):
                delta["role"] = "assistant"
            if delta.get("role"):
                self.roles.add(index)
            meaningful = meaningful or useful or bool(choice.get("finish_reason"))
        return result if meaningful else None


def reasoning_tokens(usage: dict | None) -> int:
    usage = usage or {}
    return ((usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
            or usage.get("completion_thinking_tokens") or 0)
