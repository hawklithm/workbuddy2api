"""Build CodeBuddy chat requests without coupling gateway rules to HTTP streaming.

The preparation pipeline is intentionally ordered:
normalize tool choice -> gateway message rules -> optional desensitization ->
stream flags -> authenticated headers -> gateway header rules.

Gateway rules are small strategies rather than branches inside forward_chat().
The builder performs no I/O beyond the existing client auth/header methods.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

from codebuddy_proxy.role_compat import adapt_developer_roles, uses_legacy_chat_roles

Diagnostic = Callable[..., None]


@dataclass(frozen=True)
class PreparedChatRequest:
    """Prepared immutable envelope; body and header dictionaries remain mutable."""

    url: str
    headers: dict[str, str]
    body: dict[str, Any]
    stream: bool


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


class DomesticDeveloperRolePolicy:
    """Scope the legacy-role workaround to the verified domestic gateway."""

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
    """Preserve the existing URL guard and best-effort header injection.

    Deliberately retain the original 'codebuddy.ai' substring check to avoid
    changing which configured gateways receive X-Conversation-* headers.
    """

    def apply(
        self, headers: dict[str, str], *, body: dict[str, Any],
        client: Any, url: str, diagnostic: Diagnostic,
    ) -> None:
        if "codebuddy.ai" in url:
            try:
                headers.update(client.conversation_headers(body))
                diagnostic(
                    "intl_conversation_headers",
                    conversation_id=headers.get("X-Conversation-ID", "")[:12] + "...",
                )
            except Exception as exc:  # A header failure never blocks forwarding.
                diagnostic("intl_conversation_headers_failed", error=str(exc)[:200])


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
        message_policies: tuple[MessagePolicy, ...] = (DomesticDeveloperRolePolicy(),),
        header_policies: tuple[HeaderPolicy, ...] = (InternationalConversationHeaderPolicy(),),
    ) -> None:
        self.client = client
        self.enable_desensitize = enable_desensitize
        self.desensitize_body = desensitize_body
        self.normalize_tool_choice = normalize_tool_choice
        self.diagnostic = diagnostic
        self.message_policies = message_policies
        self.header_policies = header_policies

    def prepare(self, body: dict[str, Any], protocol: str) -> PreparedChatRequest:
        stream = bool(body.get("stream"))
        upstream_body = dict(body)

        if "tool_choice" in upstream_body:
            upstream_body["tool_choice"] = self.normalize_tool_choice(
                upstream_body["tool_choice"]
            )

        url = self.client.build_url(self.client.profile.chat_path)
        for policy in self.message_policies:
            policy.apply(
                upstream_body, client=self.client, url=url, protocol=protocol,
                diagnostic=self.diagnostic,
            )

        if self.enable_desensitize:
            upstream_body = self.desensitize_body(upstream_body, compact_harness=True)

        upstream_body["stream"] = True
        upstream_body.setdefault("stream_options", {"include_usage": True})

        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; Genie-IDE/1.0)",
            **self.client.auth_headers(),
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        for policy in self.header_policies:
            policy.apply(
                headers, body=upstream_body, client=self.client, url=url,
                diagnostic=self.diagnostic,
            )

        return PreparedChatRequest(url=url, headers=headers, body=upstream_body, stream=stream)
