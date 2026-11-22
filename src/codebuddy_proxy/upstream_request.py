"""Build CodeBuddy chat requests without coupling gateway rules to HTTP streaming.

Resolve original session identity -> prompt mode -> basic role/tool rules ->
model reasoning -> optional fingerprint/harness rewrite -> cache/system ->
authenticated chat identity and correlation headers.

Gateway rules are small strategies rather than branches inside forward_chat().
The builder performs no I/O beyond the existing client auth/header methods.
"""

from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
import hashlib
from typing import Any, Callable, Protocol

from codebuddy_proxy.role_compat import adapt_developer_roles, uses_legacy_chat_roles
from codebuddy_proxy.session_context import SessionContext, prompt_cache_key
from codebuddy_proxy.workbuddy_compat import chat_headers, normalize_deepseek
from codebuddy_proxy.prompt_rewrite import PromptRewriter, sanitize_fingerprints

Diagnostic = Callable[..., None]


@dataclass(frozen=True)
class PreparedChatRequest:
    """Prepared immutable envelope; body and header dictionaries remain mutable."""

    url: str
    headers: dict[str, str]
    body: dict[str, Any]
    stream: bool
    sanitized: bool = False
    prompt_scope: str = ""


class MessagePolicy(Protocol):
    def apply(
        self, body: dict[str, Any], *, client: Any, url: str,
        protocol: str, diagnostic: Diagnostic,
    ) -> None: ...


class HeaderPolicy(Protocol):
    def apply(
        self, headers: dict[str, str], *, body: dict[str, Any],
        client: Any, url: str, diagnostic: Diagnostic,
    ) -> None: ...


class CodeBuddyDeveloperRolePolicy:
    """Scope the legacy-role workaround to the built-in CodeBuddy gateways."""

    def apply(
        self, body: dict[str, Any], *, client: Any, url: str,
        protocol: str, diagnostic: Diagnostic,
    ) -> None:
        messages = body.get("messages")
        if (uses_legacy_chat_roles(client.profile.key, url)
                and isinstance(messages, list)):
            compatible, count = adapt_developer_roles(messages)
            if count:
                body["messages"] = compatible
                diagnostic(
                    "upstream_role_compat", protocol=protocol,
                    backend=client.profile.key,
                    mapping="developer_to_system", count=count,
                )


class InternationalConversationHeaderPolicy:
    """Compatibility hook for callers composing their own header policies."""

    def apply(
        self, headers: dict[str, str], *, body: dict[str, Any],
        client: Any, url: str, diagnostic: Diagnostic,
    ) -> None:
        if uses_legacy_chat_roles(client.profile.key, url):
            headers.update(SessionContext.from_request(body).headers())


class ChatRequestPreparer:
    """Compose gateway strategies with the existing request transformations.

    Dependencies are injected to avoid importing the FastAPI entry point and
    to keep normalization/desensitization behavior under its existing owners.
    """

    def __init__(
        self,
        *,
        client: Any,
        enable_desensitize: bool,
        desensitize_body: Callable[..., dict[str, Any]],
        normalize_tool_choice: Callable[[Any], Any],
        diagnostic: Diagnostic,
        prompt_rewriter: PromptRewriter | None = None,
        session_context: SessionContext | None = None,
        model_efforts: dict[str, str] | None = None,
        message_policies: tuple[MessagePolicy, ...] = (CodeBuddyDeveloperRolePolicy(),),
        header_policies: tuple[HeaderPolicy, ...] = (),
    ) -> None:
        self.client = client
        self.enable_desensitize = enable_desensitize
        self.desensitize_body = desensitize_body
        self.normalize_tool_choice = normalize_tool_choice
        self.diagnostic = diagnostic
        self.prompt_rewriter = prompt_rewriter
        self.session_context = session_context
        self.model_efforts = model_efforts
        self.message_policies = message_policies
        self.header_policies = header_policies

    def prepare(self, body: dict[str, Any], protocol: str) -> PreparedChatRequest:
        stream = bool(body.get("stream"))
        upstream_body = deepcopy(body)
        context = self.session_context or SessionContext.from_request(body)
        url = self.client.build_url(self.client.profile.chat_path)
        compatible = uses_legacy_chat_roles(self.client.profile.key, url)
        auth = self.client.auth_headers()
        # Never use a raw credential as a stored scope key.
        identity = auth.get("X-User-Id") or hashlib.sha256(auth.get("Authorization", "").encode()).hexdigest()
        scope = f"{self.client.profile.key}|{url}|{identity}"
        if compatible and self.prompt_rewriter is not None:
            upstream_body = self.prompt_rewriter.apply(upstream_body, scope)

        if "tool_choice" in upstream_body:
            upstream_body["tool_choice"] = self.normalize_tool_choice(
                upstream_body["tool_choice"]
            )

        for policy in self.message_policies:
            policy.apply(
                upstream_body, client=self.client, url=url, protocol=protocol,
                diagnostic=self.diagnostic,
            )

        if compatible:
            normalize_deepseek(upstream_body, self.client.profile.key, self.model_efforts)

        sanitized = False
        if self.enable_desensitize:
            if compatible:
                upstream_body, sanitized = sanitize_fingerprints(upstream_body)
            upstream_body = self.desensitize_body(upstream_body, compact_harness=True)

        upstream_body["stream"] = True
        upstream_body.setdefault("stream_options", {"include_usage": True})

        if compatible:
            if not isinstance(upstream_body.get("prompt_cache_key"), str) or not upstream_body["prompt_cache_key"].strip():
                upstream_body["prompt_cache_key"] = prompt_cache_key(auth.get("X-User-Id", ""), context.conversation_id)
            if self.client.profile.key == "global":
                messages = upstream_body.get("messages")
                if isinstance(messages, list) and (not messages or not isinstance(messages[0], dict) or messages[0].get("role") != "system"):
                    messages.insert(0, {"role": "system", "content": "You are a helpful assistant."})

        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; Genie-IDE/1.0)",
            **auth,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        if compatible:
            headers = chat_headers(auth, self.client.profile.key)
        for policy in self.header_policies:
            policy.apply(
                headers, body=upstream_body, client=self.client, url=url,
                diagnostic=self.diagnostic,
            )
        if compatible:
            headers.update(context.headers())

        return PreparedChatRequest(url=url, headers=headers, body=upstream_body, stream=stream,
                                   sanitized=sanitized, prompt_scope=scope if compatible else "")
