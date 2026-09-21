#!/usr/bin/env python3
"""Minimal CodeBuddy external-link-v2 client demo.

The protocol is extracted from Tencent Cloud CodeBuddy's VSIX.  This demo
uses only the Python standard library and stores the session locally with
0600 permissions.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from typing import Any, Iterator

from codebuddy_proxy.backend_profile import (
    BackendProfile,
    ResolvedBackend,
    normalize_endpoint,
    resolve_backend,
    resolve_backend_from_args,
)


class CodeBuddyError(RuntimeError):
    pass


def _load_json_bytes(raw: bytes) -> Any:
    """按 utf-8 → gbk → cp936 → latin-1 依次解码并解析 JSON。

    兼容上游偶发返回非 UTF-8 字节（如 GBK）的情况，避免 `raw.decode("utf-8")`
    抛 `UnicodeDecodeError`。若所有编码都无法解析为合法 JSON，最终以
    latin-1 解码后抛 `json.JSONDecodeError`，交由调用方统一处理。
    """
    for encoding in ("utf-8", "gbk", "cp936", "latin-1"):
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            continue
    # latin-1 能解码任意字节，故 decode 不会失败；走到这里仅当 JSON 结构非法。
    return json.loads(raw.decode("latin-1"))


class CodeBuddyClient:
    def __init__(
        self,
        endpoint: str | None = None,
        platform: str = "VSCode",
        session_file: pathlib.Path | str | None = None,
        *,
        backend: ResolvedBackend | BackendProfile | None = None,
        profile: BackendProfile | None = None,
    ) -> None:
        if backend is not None and profile is not None:
            raise ValueError("CodeBuddyClient 不能同时指定 backend 和 profile")
        if profile is not None:
            backend = profile
        if isinstance(backend, ResolvedBackend):
            if endpoint is None and session_file is None:
                resolved_backend = backend
            else:
                resolved_backend = resolve_backend(
                    profile=backend.profile,
                    endpoint=endpoint or backend.endpoint,
                    session_file=session_file or backend.session_file,
                    environment={},
                )
        elif isinstance(backend, BackendProfile):
            resolved_backend = resolve_backend(
                profile=backend,
                endpoint=endpoint,
                session_file=session_file,
                environment={},
            )
        else:
            # Direct library callers historically defaulted to the domestic
            # endpoint; environment precedence belongs to the CLI resolver.
            resolved_backend = resolve_backend(
                endpoint=endpoint,
                session_file=session_file,
                environment={},
            )

        self.backend = resolved_backend
        self.profile = resolved_backend.profile
        self.endpoint = resolved_backend.endpoint
        self.platform = platform
        self.prefix = self.profile.auth_prefix
        self.session_file = resolved_backend.session_file
        self.session: dict[str, Any] = self._load_session()

    def _load_session(self) -> dict[str, Any]:
        try:
            session = json.loads(self.session_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            raise CodeBuddyError(f"无法读取 session 文件: {self.session_file}: {exc}") from exc

        if not isinstance(session, dict):
            raise CodeBuddyError(f"session 文件格式无效（应为 JSON object）: {self.session_file}")
        self._validate_session_metadata(session)
        return session

    def _validate_session_metadata(self, session: dict[str, Any]) -> None:
        """Reject credentials saved for a different backend or endpoint."""

        stored_backend = session.get("backend")
        stored_endpoint = session.get("endpoint")

        # Sessions written before profile metadata was introduced are
        # intentionally usable only by the domestic profile's default endpoint.
        # This preserves the old default while preventing an old token from
        # being sent to a custom or global endpoint.
        if not stored_backend:
            if self.profile.key != "domestic":
                raise CodeBuddyError(
                    f"session 文件 {self.session_file} 没有 backend 元数据，"
                    "只能由 domestic profile 读取；请使用匹配的 --session-file。"
                )
            if self.endpoint != self.profile.default_endpoint:
                raise CodeBuddyError(
                    f"session 文件 {self.session_file} 没有 backend/endpoint 元数据，"
                    "只能在 domestic profile 的默认 endpoint 下读取；"
                    "请使用匹配的 --session-file。"
                )
            if stored_endpoint:
                try:
                    stored_endpoint = normalize_endpoint(str(stored_endpoint))
                except ValueError:
                    stored_endpoint = ""
                if stored_endpoint != self.endpoint:
                    raise CodeBuddyError(
                        f"session 文件 {self.session_file} 的 endpoint 与当前 backend 不匹配；"
                        "原文件未修改，请使用匹配的 --session-file。"
                    )
            return

        if stored_backend != self.profile.key:
            raise CodeBuddyError(
                f"session 文件 {self.session_file} 属于 backend={stored_backend!r}，"
                f"当前 backend={self.profile.key!r}，backend 不匹配；原文件未修改。"
            )

        if not stored_endpoint:
            raise CodeBuddyError(
                f"session 文件 {self.session_file} 缺少 endpoint 元数据；原文件未修改。"
            )

        try:
            normalized_stored_endpoint = normalize_endpoint(str(stored_endpoint))
        except ValueError:
            normalized_stored_endpoint = ""
        if normalized_stored_endpoint != self.endpoint:
            raise CodeBuddyError(
                f"session 文件 {self.session_file} 的 endpoint 与当前 backend 不匹配；"
                "原文件未修改，请使用匹配的 --session-file。"
            )

    def _save_session(self, session: dict[str, Any]) -> None:
        persisted_session = dict(session)
        persisted_session["backend"] = self.profile.key
        persisted_session["endpoint"] = self.endpoint
        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(self.session_file, flags, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(persisted_session, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
        finally:
            try:
                os.chmod(self.session_file, 0o600)
            except OSError:
                pass
        self.session = persisted_session

    def build_url(self, path: str) -> str:
        """Build a URL from the resolved endpoint and a relative path."""

        return f"{self.endpoint}/{path.lstrip('/')}"

    def enterprise_models_path(self, enterprise_id: str) -> str:
        """Return the profile-defined enterprise model-config path."""

        return self.profile.enterprise_models_path.format(
            enterpriseId=urllib.parse.quote(str(enterprise_id), safe="")
        )

    def _auth_path(self, suffix: str) -> str:
        return f"/v2{self.profile.auth_prefix}{suffix}"

    @staticmethod
    def _unwrap(payload: Any) -> Any:
        # CodeBuddy responses observed in the extension use {data: {data: ...}}.
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            nested = payload["data"]
            if "data" in nested:
                return nested["data"]
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: Any = None,
        timeout: float = 30,
    ) -> Any:
        # 🔥 所有请求都必须包含这两个基础标识（匹配插件行为）
        request_headers = {
            "User-Agent": "Mozilla/5.0 (compatible; Genie-IDE/1.0)",
            "X-Product-Code": "codebuddy",
        }
        # 关键修复：合并调用者传入的 headers
        if headers:
            request_headers.update(headers)
        
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(
            self.build_url(path),
            data=data,
            headers=request_headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                content_type = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise CodeBuddyError(f"HTTP {exc.code} {path}: {detail[:1000]}") from exc
        except urllib.error.URLError as exc:
            raise CodeBuddyError(f"请求失败 {path}: {exc.reason}") from exc
        if "json" not in content_type and not raw:
            return None
        try:
            return _load_json_bytes(raw)
        except json.JSONDecodeError as exc:
            raise CodeBuddyError(f"{path} 返回的不是 JSON: {raw[:300]!r}") from exc

    def auth_headers(self, *, access: bool = True, refresh: bool = False) -> dict[str, str]:
        """构造认证 headers，模拟插件行为"""
        account = self.session.get("account") or {}
        auth = self.session.get("auth") or {}
        headers: dict[str, str] = {
            # 🔥 关键标识：所有请求都需要这个 header
            "X-Product-Code": "codebuddy",
            # 🔥 IDE 识别信息（服务端用于识别客户端类型）
            "X-IDE-Type": "vscode",
            "X-IDE-Name": "Visual Studio Code",
            "X-IDE-Version": "1.70.2",  # VSCode 最低版本要求
            "X-Product-Version": "4.10.33259736",  # 插件版本
            "X-Machine-Id": self._get_machine_id(),
        }
        
        if account.get("uid"):
            headers["X-User-Id"] = str(account["uid"])
        if access and auth.get("accessToken"):
            headers["Authorization"] = f"Bearer {auth['accessToken']}"
        if refresh and auth.get("refreshToken"):
            headers["X-Refresh-Token"] = str(auth["refreshToken"])
        if account.get("enterpriseId"):
            headers["X-Enterprise-Id"] = str(account["enterpriseId"])
            headers["X-Tenant-Id"] = str(account["enterpriseId"])
        if account.get("departmentInfo"):
            # 🔥 新增：部门信息（如果有）
            headers["X-Department-Info"] = str(account["departmentInfo"])
        domain = self._domain_for_token(auth)
        if domain:
            # The extension calls this the domain header.  The server accepts
            # X-Domain for the plugin protocol.
            headers["X-Domain"] = domain
        return headers

    def _enterprise_headers(self, token: dict[str, Any] | None = None) -> dict[str, str]:
        """构造企业认证 headers（模拟 VSIX enterpriseHeaders）"""
        domain = self._domain_for_token(token)
        return {"X-Domain": domain} if domain else {}

    def _domain_for_token(self, token: dict[str, Any] | None = None) -> str:
        """Use token.domain first, then the resolved endpoint authority."""

        if token and token.get("domain"):
            return str(token["domain"])
        try:
            return urllib.parse.urlsplit(self.endpoint).netloc
        except ValueError:
            return ""

    def _get_machine_id(self) -> str:
        """获取或生成机器ID（模拟VSCode的machineId）"""
        import platform
        import uuid
        
        # 尝试从session读取已保存的machineId
        saved_machine_id = self.session.get("machineId")
        if saved_machine_id:
            return str(saved_machine_id)
        
        # 生成一个稳定的machineId（基于主机名和用户）
        try:
            hostname = platform.node()
            username = os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
            seed = f"{hostname}-{username}"
            machine_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, seed))
        except Exception:
            # 如果失败，生成随机ID
            machine_id = str(uuid.uuid4())
        
        # 保存到session
        self.session["machineId"] = machine_id
        self._save_session(self.session)
        return machine_id

    def login(self, *, open_browser: bool = True, timeout: int = 300) -> None:
        no_auth = {
            "X-No-Authorization": "true",
            "X-No-User-Id": "true",
            "X-No-Enterprise-Id": "true",
            "X-No-Department-Info": "true",
        }
        state_payload = self._unwrap(
            self._request(
                "POST",
                f"{self._auth_path('/auth/state')}?platform={urllib.parse.quote(self.platform)}",
                headers={**self._enterprise_headers(), **no_auth},
                body={},
            )
        )
        if not isinstance(state_payload, dict) or not state_payload.get("authUrl"):
            raise CodeBuddyError(f"登录状态响应缺少 authUrl: {state_payload!r}")
        auth_url = str(state_payload["authUrl"])
        state = state_payload.get("state")
        if not state:
            raise CodeBuddyError("登录状态响应缺少 state")
        print(f"请在浏览器中完成登录：\n{auth_url}")
        if open_browser:
            webbrowser.open(auth_url)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(1)
            try:
                token = self._unwrap(
                    self._request(
                        "GET",
                        f"{self._auth_path('/auth/token')}?state={urllib.parse.quote(str(state), safe='')}",
                        headers={**self._enterprise_headers(), **no_auth},
                    )
                )
            except CodeBuddyError:
                continue
            if isinstance(token, dict) and token.get("accessToken"):
                # 🔥 关键修复：/login/account 也需要轮询（模拟 VSIX loopGetAccount）
                # 服务端在用户浏览器登录后，账户信息可能需要异步准备
                account = None
                account_deadline = time.monotonic() + 60  # 账户轮询最多60秒
                
                while time.monotonic() < account_deadline:
                    time.sleep(1)  # 每秒轮询一次
                    try:
                        account = self._unwrap(
                            self._request(
                                "GET",
                                f"{self._auth_path('/login/account')}?state={urllib.parse.quote(str(state), safe='')}",
                                headers={
                                    # 关键修复：必须包含完整的 enterprise headers（包括 X-Domain）
                                    **self._enterprise_headers(token),
                                    "Authorization": f"Bearer {token['accessToken']}",
                                    "X-No-User-Id": "true",
                                    "X-No-Enterprise-Id": "true",
                                    "X-No-Department-Info": "true",
                                },
                            )
                        )
                        if isinstance(account, dict) and account.get("uid"):
                            # 账户信息获取成功
                            break
                    except CodeBuddyError:
                        # 401 或其他错误：账户信息可能尚未就绪，继续轮询
                        continue
                
                if not account or not isinstance(account, dict):
                    raise CodeBuddyError("账户信息获取超时")
                
                self._save_session({**self.session, "auth": token, "account": account})
                print(f"登录成功，用户: {account.get('nickname') or account.get('uid', '<unknown>')}")
                return

    @staticmethod
    def _token_headers(token: dict[str, Any]) -> dict[str, str]:
        headers: dict[str, str] = {}
        if token.get("accessToken"):
            headers["Authorization"] = f"Bearer {token['accessToken']}"
        if token.get("domain"):
            headers["X-Domain"] = str(token["domain"])
        return headers

    def refresh(self) -> bool:
        auth = self.session.get("auth") or {}
        refresh_token = auth.get("refreshToken")
        if not refresh_token:
            return False
        payload = self._unwrap(
            self._request(
                "POST",
                self._auth_path("/auth/token/refresh"),
                headers={
                    **self.auth_headers(access=False, refresh=True),
                    "X-Auth-Refresh-Source": "plugin",
                },
                body={},
            )
        )
        if not isinstance(payload, dict) or not payload.get("accessToken"):
            return False
        self._save_session({**self.session, "auth": payload})
        return True

    def ensure_authenticated(self, *, open_browser: bool = True) -> None:
        auth = self.session.get("auth") or {}
        now_ms = int(time.time() * 1000)
        expires_at = int(auth.get("expiresAt") or 0)
        if auth.get("accessToken") and (not expires_at or expires_at > now_ms + 60_000):
            return
        if self.refresh():
            print("access token 已刷新")
            return
        self.login(open_browser=open_browser)

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
