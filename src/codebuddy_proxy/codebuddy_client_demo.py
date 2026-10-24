#!/usr/bin/env python3
"""Minimal CodeBuddy external-link-v2 client demo.

The protocol is extracted from Tencent Cloud CodeBuddy's VSIX.  This demo
uses only the Python standard library and stores the session locally with
0600 permissions.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import pathlib
import sys
import tempfile
import threading
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
        self._session_lock = threading.RLock()
        self._auth_lock = threading.RLock()
        self._session_generation = 0
        self._persistence_enabled = True
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

    def _snapshot_session(self) -> tuple[dict[str, Any], int]:
        with self._session_lock:
            return copy.deepcopy(self.session), self._session_generation

    def _save_session(
        self,
        session: dict[str, Any],
        *,
        expected_generation: int | None = None,
    ) -> bool:
        persisted_session = dict(session)
        persisted_session["backend"] = self.profile.key
        persisted_session["endpoint"] = self.endpoint
        with self._session_lock:
            if not self._persistence_enabled:
                return False
            if (
                expected_generation is not None
                and self._session_generation != expected_generation
            ):
                return False
            self.session_file.parent.mkdir(parents=True, exist_ok=True)
            temp_name: str | None = None
            try:
                fd, temp_name = tempfile.mkstemp(
                    prefix=f".{self.session_file.name}.",
                    suffix=".tmp",
                    dir=self.session_file.parent,
                )
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(persisted_session, stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_name, self.session_file)
                temp_name = None
                try:
                    os.chmod(self.session_file, 0o600)
                except OSError:
                    pass
            except OSError as exc:
                raise CodeBuddyError(
                    f"无法原子写入 session 文件: {self.session_file}"
                ) from exc
            finally:
                if temp_name is not None:
                    try:
                        os.unlink(temp_name)
                    except OSError:
                        pass
            self.session = persisted_session
            self._session_generation += 1
            return True

    def _invalidate_persistence(self) -> None:
        """Prevent an old client reference from recreating a removed session."""
        with self._session_lock:
            self._session_generation += 1
            self._persistence_enabled = False

    def _enable_persistence(self) -> None:
        """Restore writes if a store deletion transaction was rolled back."""
        with self._session_lock:
            self._session_generation += 1
            self._persistence_enabled = True

    def _restore_session_snapshot(self, session: dict[str, Any]) -> None:
        """Restore in-memory state after an account-store transaction fails."""
        with self._session_lock:
            self.session = copy.deepcopy(session)
            self._session_generation += 1

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
        self._get_machine_id()
        session, _ = self._snapshot_session()
        return self._auth_headers_for_session(session, access=access, refresh=refresh)

    def _auth_headers_for_session(
        self,
        session: dict[str, Any],
        *,
        access: bool = True,
        refresh: bool = False,
    ) -> dict[str, str]:
        account = session.get("account") or {}
        auth = session.get("auth") or {}
        headers: dict[str, str] = {
            "X-Product-Code": "codebuddy",
            "X-IDE-Type": "vscode",
            "X-IDE-Name": "Visual Studio Code",
            "X-IDE-Version": "1.70.2",
            "X-Product-Version": "4.10.33259736",
            "X-Machine-Id": str(session.get("machineId") or ""),
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
            headers["X-Department-Info"] = str(account["departmentInfo"])
        domain = self._domain_for_token(auth)
        if domain:
            headers["X-Domain"] = domain
        return headers

    def conversation_headers(self, body: dict[str, Any] | None = None) -> dict[str, str]:
        """INTL (www.codebuddy.ai) 网关的「会话标识」闸门。

        2026-10-16 起，国际站对 chat/completions 强制校验 X-Conversation-* 头族，
        缺失即返回 HTTP 400 / code 11128 "conversation id missing" /
        "request illegal"（渠道校验失败）。国内站 copilot.tencent.com 无此闸门。

        X-Conversation-ID 需在**同一会话内稳定**（上游按它做 prompt 前缀缓存分桶），
        因此取首条消息（通常是 system）的哈希；同一会话跨轮复用同一个值。
        Message-ID / Request-ID 每请求新生成。

        参考社区独立复现与修复：
        - decolua/9router#4933  fix(codebuddy-intl): send X-Conversation-ID header family
        - Ebony-Vinyl/dsh-our-free-model#377  「双闸门」：X-Conversation-* + 首条 system
        """
        import hashlib
        import uuid as _uuid

        messages = (body or {}).get("messages") or []
        seed = ""
        if messages and isinstance(messages[0], dict):
            first = messages[0].get("content")
            seed = (first if isinstance(first, str)
                    else json.dumps(first, ensure_ascii=False, sort_keys=True))
        if not seed:
            seed = json.dumps(body or {}, ensure_ascii=False, sort_keys=True)
        conversation_id = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]

        return {
            "X-Conversation-ID": conversation_id,
            "X-Conversation-Message-ID": str(_uuid.uuid4()),
            "X-Conversation-Request-ID": str(_uuid.uuid4()),
        }
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
        with self._session_lock:
            saved_machine_id = self.session.get("machineId")
            if saved_machine_id:
                return str(saved_machine_id)
            try:
                hostname = platform.node()
                username = os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
                seed = f"{hostname}-{username}"
                machine_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, seed))
            except Exception:
                machine_id = str(uuid.uuid4())
            self._save_session({**self.session, "machineId": machine_id})
            return machine_id

    def begin_login(self) -> dict[str, str]:
        """Request an authorization URL and state without opening a browser."""
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
        return {"auth_url": auth_url, "state": str(state)}

    @staticmethod
    def _check_login_cancelled(cancel_event: Any | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise CodeBuddyError("登录已取消")

    @classmethod
    def _wait_for_login_poll(cls, deadline: float, cancel_event: Any | None) -> None:
        cls._check_login_cancelled(cancel_event)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        pause = min(1.0, remaining)
        if cancel_event is not None and hasattr(cancel_event, "wait"):
            if cancel_event.wait(pause):
                raise CodeBuddyError("登录已取消")
        else:
            time.sleep(pause)
        cls._check_login_cancelled(cancel_event)

    @staticmethod
    def _account_has_uid(account: Any) -> bool:
        return (
            isinstance(account, dict)
            and account.get("uid") is not None
            and bool(str(account.get("uid")).strip())
        )

    def complete_login(
        self,
        challenge: dict[str, Any],
        timeout: float = 300,
        cancel_event: Any | None = None,
    ) -> None:
        """Poll the challenge until a token and uid-bearing account are ready.

        One deadline covers both polling stages. No session state is saved
        unless a token and an account with a uid have both been returned.
        """
        if not isinstance(challenge, dict) or not challenge.get("state"):
            raise CodeBuddyError("登录 challenge 缺少 state")
        try:
            timeout_value = float(timeout)
        except (TypeError, ValueError) as exc:
            raise CodeBuddyError("登录超时时间无效") from exc
        if timeout_value <= 0:
            raise CodeBuddyError("登录超时时间必须大于 0")
        state = str(challenge["state"])
        state_query = urllib.parse.quote(state, safe="")
        deadline = time.monotonic() + timeout_value
        _, initial_generation = self._snapshot_session()
        no_auth = {
            "X-No-Authorization": "true",
            "X-No-User-Id": "true",
            "X-No-Enterprise-Id": "true",
            "X-No-Department-Info": "true",
        }
        while time.monotonic() < deadline:
            self._check_login_cancelled(cancel_event)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                token = self._unwrap(
                    self._request(
                        "GET",
                        f"{self._auth_path('/auth/token')}?state={state_query}",
                        headers={**self._enterprise_headers(), **no_auth},
                        timeout=min(30.0, remaining),
                    )
                )
            except CodeBuddyError:
                token = None
            if isinstance(token, dict) and token.get("accessToken"):
                account = None
                while time.monotonic() < deadline:
                    self._check_login_cancelled(cancel_event)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        account = self._unwrap(
                            self._request(
                                "GET",
                                f"{self._auth_path('/login/account')}?state={state_query}",
                                headers={
                                    # 关键修复：必须包含完整的 enterprise headers（包括 X-Domain）
                                    **self._enterprise_headers(token),
                                    "Authorization": f"Bearer {token['accessToken']}",
                                    "X-No-User-Id": "true",
                                    "X-No-Enterprise-Id": "true",
                                    "X-No-Department-Info": "true",
                                },
                                timeout=min(30.0, remaining),
                            )
                        )
                        self._check_login_cancelled(cancel_event)
                        if time.monotonic() >= deadline:
                            account = None
                            break
                        if self._account_has_uid(account):
                            break
                        account = None
                    except CodeBuddyError:
                        account = None
                    if account is not None:
                        break
                    self._wait_for_login_poll(deadline, cancel_event)

                if self._account_has_uid(account):
                    self._check_login_cancelled(cancel_event)
                    if time.monotonic() >= deadline:
                        raise CodeBuddyError("登录超时：未能获取包含 uid 的账户信息")
                    session, _ = self._snapshot_session()
                    self._save_session(
                        {**session, "auth": token, "account": account},
                        expected_generation=initial_generation,
                    )
                    return
                raise CodeBuddyError("登录超时：未能获取包含 uid 的账户信息")

            self._wait_for_login_poll(deadline, cancel_event)

        self._check_login_cancelled(cancel_event)
        raise CodeBuddyError("登录超时：未能获取访问令牌")

    def login(self, *, open_browser: bool = True, timeout: int = 300) -> None:
        challenge = self.begin_login()
        auth_url = challenge["auth_url"]
        print(f"请在浏览器中完成登录：\n{auth_url}")
        if open_browser:
            webbrowser.open(auth_url)
        self.complete_login(challenge, timeout=timeout)
        account = self.session.get("account") or {}
        print(f"登录成功，用户: {account.get('nickname') or account.get('uid', '<unknown>')}")

    @staticmethod
    def _token_headers(token: dict[str, Any]) -> dict[str, str]:
        headers: dict[str, str] = {}
        if token.get("accessToken"):
            headers["Authorization"] = f"Bearer {token['accessToken']}"
        if token.get("domain"):
            headers["X-Domain"] = str(token["domain"])
        return headers

    def refresh(self) -> bool:
        with self._auth_lock:
            return self._refresh_locked()

    def _refresh_locked(self) -> bool:
        session, generation = self._snapshot_session()
        auth = session.get("auth") or {}
        refresh_token = auth.get("refreshToken")
        if not refresh_token:
            return False
        self._get_machine_id()
        session, generation = self._snapshot_session()
        auth = session.get("auth") or {}
        refresh_token = auth.get("refreshToken")
        if not refresh_token:
            return False
        payload = self._unwrap(
            self._request(
                "POST",
                self._auth_path("/auth/token/refresh"),
                headers={
                    **self._auth_headers_for_session(session, access=False, refresh=True),
                    "X-Auth-Refresh-Source": "plugin",
                },
                body={},
            )
        )
        if not isinstance(payload, dict) or not payload.get("accessToken"):
            return False
        saved = self._save_session(
            {**session, "auth": payload}, expected_generation=generation
        )
        if saved:
            return True
        # A newer login or import completed while refresh was in flight.
        current_auth = self._snapshot_session()[0].get("auth") or {}
        current_expiry = int(current_auth.get("expiresAt") or 0)
        return bool(
            current_auth.get("accessToken")
            and (not current_expiry or current_expiry > int(time.time() * 1000))
        )

    def ensure_authenticated(
        self,
        *,
        open_browser: bool = True,
        allow_login: bool = True,
    ) -> None:
        with self._auth_lock:
            # A preceding request may have refreshed the token while this
            # caller waited for the per-client authentication lock.
            auth = self._snapshot_session()[0].get("auth") or {}
            now_ms = int(time.time() * 1000)
            expires_at = int(auth.get("expiresAt") or 0)
            if auth.get("accessToken") and (
                not expires_at or expires_at > now_ms + 60_000
            ):
                return
            if self.refresh():
                print("access token 已刷新")
                return
            if not allow_login:
                raise CodeBuddyError("当前账号需要重新登录；此请求禁止自动打开登录流程")
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
