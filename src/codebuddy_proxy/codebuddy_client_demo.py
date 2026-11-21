#!/usr/bin/env python3
"""Compatibility entry point and chat demo backed by buddy-agent-auth."""
import argparse
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request
from typing import Any, Iterator

from buddy_agent_auth.client import CodeBuddyClient as _AuthClient, CodeBuddyError
from buddy_agent_auth.backend_profile import resolve_backend_from_args


class _ChatMixin:
    def conversation_headers(self, body: dict[str, Any] | None = None) -> dict[str, str]:
        """Build the shared conversation/trace family without an HTTP context.

        The proxy captures explicit inbound session headers before rewriting;
        standalone callers use body metadata or the initial-turn fingerprint.
        """
        from codebuddy_proxy.session_context import SessionContext
        return SessionContext.from_request(body or {}).headers()

    def stream_chat(
        self,
        prompt: str,
        *,
        model: str = "default",
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> Iterator[str]:
        self.ensure_authenticated()
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        request = urllib.request.Request(
            self.build_url(self.profile.chat_path),
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; Genie-IDE/1.0)",
                "X-Product-Code": "codebuddy",
                **self.auth_headers(),
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            method="POST",
        )
        try:
            response = urllib.request.urlopen(request, timeout=180)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if exc.code == 401 and self.refresh():
                yield from self.stream_chat(
                    prompt, model=model, temperature=temperature, max_tokens=max_tokens
                )
                return
            raise CodeBuddyError(f"聊天请求 HTTP {exc.code}: {detail[:1000]}") from exc
        with response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                for choice in chunk.get("choices", []):
                    delta = choice.get("delta") or {}
                    content = delta.get("content")
                    if content:
                        yield str(content)


# Preserve the original public class identity for existing integrations.
_AuthClient.stream_chat = _ChatMixin.stream_chat
_AuthClient.conversation_headers = _ChatMixin.conversation_headers
CodeBuddyClient = _AuthClient


def main() -> int:
    parser = argparse.ArgumentParser(description="CodeBuddy login/refresh/chat demo")
    parser.add_argument("prompt", nargs="?", help="要发送的问题")
    parser.add_argument("--global", dest="global_mode", action="store_true",
                        help="使用国际版 CodeBuddy（默认使用国内版）")
    parser.add_argument("--endpoint", default=None,
                        help="CodeBuddy 后端地址；显式值优先于 profile 默认值和 CODEBUDDY_ENDPOINT")
    parser.add_argument("--model", default=os.getenv("CODEBUDDY_MODEL", "default"))
    parser.add_argument("--session-file", type=pathlib.Path)
    parser.add_argument("--no-browser", action="store_true", help="只打印登录 URL，不自动打开浏览器")
    parser.add_argument("--login", action="store_true", help="强制重新登录")
    args = parser.parse_args()
    try:
        client = CodeBuddyClient(backend=resolve_backend_from_args(args))
        if args.login:
            client.login(open_browser=not args.no_browser)
        prompt = args.prompt or input("Prompt: ")
        for text in client.stream_chat(prompt, model=args.model):
            print(text, end="", flush=True)
        print()
        return 0
    except (CodeBuddyError, KeyboardInterrupt) as exc:
        print(f"\n错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
