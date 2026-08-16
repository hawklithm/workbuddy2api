#!/usr/bin/env python3
"""Record raw CodeBuddy backend responses for offline mock tests.

This intentionally bypasses codebuddy_proxy.py. It performs exactly two
backend data calls after authentication/session preparation:

  1. GET  /v3/config
  2. POST /v2/chat/completions (stream=true, message: hi)

The response envelope includes status, headers, request metadata, and the
complete raw response body. Authorization values are never written.
"""

from __future__ import annotations

import argparse
import base64
import json
import pathlib
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

from codebuddy_proxy.codebuddy_client_demo import CodeBuddyClient, CodeBuddyError


def safe_headers(headers: Any) -> dict[str, str]:
    result = {}
    for key, value in headers.items():
        if key.lower() in {"authorization", "x-refresh-token", "cookie", "set-cookie"}:
            result[key] = "<redacted>"
        else:
            result[key] = str(value)
    return result


def write_fixture(path: pathlib.Path, *, request: dict[str, Any], status: int,
                  headers: Any, body: bytes, content_type: str) -> None:
    fixture = {
        "fixture_version": 1,
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "request": request,
        "response": {
            "status": status,
            "headers": safe_headers(headers),
            "content_type": content_type,
            "body_encoding": "base64",
            "body_base64": base64.b64encode(body).decode("ascii"),
            "body_text": body.decode("utf-8", errors="replace"),
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fixture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def direct_request(client: CodeBuddyClient, method: str, path: str,
                   body: Any = None, *, accept: str = "application/json") -> tuple[int, Any, bytes, str]:
    headers = {
        **client.auth_headers(),
        "Accept": accept,
        "X-Product": "SaaS",
        # The VSIX sends a CodeBuddy IDE UA. The config service rejects a
        # generic Python/curl UA with code 12403 ("check ua").
        "User-Agent": "CodeBuddyIDE/4.10.33259736",
        "X-IDE-Type": "VSCode",
        "X-IDE-Name": "VSCode",
        "X-IDE-Version": "1.70.2",
        "X-Product-Version": "4.10.33259736",
        "X-Requested-With": "XMLHttpRequest",
    }
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    url = client.endpoint + path
    if path == "/v3/config":
        # CloudProductManager calls axios.get(path, {params: {repos: []}}).
        url += "?repos="
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            raw = response.read()
            return response.status, response.headers, raw, response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, exc.headers, raw, exc.headers.get("Content-Type", "")
    except urllib.error.URLError as exc:
        raise CodeBuddyError(f"请求 {method} {path} 失败: {exc.reason}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="Record two raw CodeBuddy backend responses")
    parser.add_argument("--endpoint", default="https://copilot.tencent.com")
    parser.add_argument("--session-file", type=pathlib.Path)
    parser.add_argument("--output-dir", type=pathlib.Path, default=pathlib.Path("fixtures/codebuddy-real"))
    parser.add_argument("--model", default="default")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    client = CodeBuddyClient(args.endpoint, session_file=args.session_file)
    # Authentication may refresh the local session or perform browser SSO;
    # this is preparation, not one of the two model/LLM data calls.
    client.ensure_authenticated(open_browser=not args.no_browser)

    models_path = args.output_dir / "models.v3-config.json"
    chat_path = args.output_dir / "chat-hi.sse.json"

    models_request = {"method": "GET", "path": "/v3/config?repos=", "body": None,
                      "headers": {"X-Product": "SaaS", "X-IDE-Type": "VSCode",
                                   "X-IDE-Name": "VSCode", "X-IDE-Version": "1.70.2",
                                   "X-Product-Version": "4.10.33259736"}}
    status, headers, raw, content_type = direct_request(client, "GET", "/v3/config")
    write_fixture(models_path, request=models_request, status=status,
                  headers=headers, body=raw, content_type=content_type)
    if status != 200:
        raise CodeBuddyError(f"模型列表请求失败，HTTP {status}；原始响应已保存到 {models_path}")

    chat_body = {
        "model": args.model,
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    chat_request = {"method": "POST", "path": "/v2/chat/completions", "body": chat_body}
    status, headers, raw, content_type = direct_request(
        client, "POST", "/v2/chat/completions", chat_body, accept="text/event-stream"
    )
    write_fixture(chat_path, request=chat_request, status=status,
                  headers=headers, body=raw, content_type=content_type)
    if status != 200:
        raise CodeBuddyError(f"chat 请求失败，HTTP {status}；原始响应已保存到 {chat_path}")

    print(f"models fixture: {models_path} ({len(json.loads(models_path.read_text())['response']['body_text'])} chars)")
    print(f"chat fixture:   {chat_path} ({len(raw)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
