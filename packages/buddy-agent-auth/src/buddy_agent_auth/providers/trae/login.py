"""Independent OAuth login flow for Trae CN with a persistent device profile.

Reverse engineered from out-build/vs/code/electron-main/oauth/marscode/ (login.js,
userLogin/loginUrlBuilder.js, oauthLocalServer.js):

1. Resolve/register a persistent hardware profile, its remote device ID and ECDSA
   P-256 keypair independently, and generate a fresh PKCE pair (S256).
2. Serve a one-shot local HTTP server on 127.0.0.1:<random> listening for
   GET /authorize (also /login-confirm, /login-success).
3. Open the browser at
   {consoleHost}/authorization?login_version=1&auth_from=trae&login_channel=native_ide
   &auth_type=local&client_id={ClientID}&redirect=0&login_trace_id={uuid}
   &auth_callback_url=http://127.0.0.1:{port}/authorize&machine_id=...&device_id=...
   &x_device_id=...&x_machine_id=...&x_device_brand=...&x_device_type=...
   &x_os_version=...&x_app_version=...&x_app_type=trae
   &code_challenge={S256}&code_challenge_method=S256
4. The web app redirects back with authCodeInfo={"AuthCode": "..."} (URL-encoded JSON).
5. POST {apiHost}/trae/api/v3/oauth/ExchangeToken
   {ClientID, AuthCode, CodeVerifier, DeviceInfo, IDEVersion}   (no DeviceProof)
   -> Result.{Token, RefreshToken, TokenExpireAt, TokenExpireDuration, RefreshExpireAt}
6. POST {apiHost}/cloudide/api/v3/trae/GetUserInfo (x-cloudide-token) for the profile.
7. Persist everything to trae2api.json (same format TraeAuth.load() consumes).

Usage:  python -m buddy_agent_auth.providers.trae.login [--timeout 1800] [--no-browser]
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import uuid
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from cryptography.hazmat.primitives import serialization

from .auth import DEFAULT_CLIENT_ID, TraeAuth
from .client_metadata import client_version, device_model, installed_client, os_metadata
from .device_hardware import Hardware, manufacturer, cpu_brand
from .login_identity import IdentityStore, atomic_json
from .aha_device_service import AhaDeviceService

CONSOLE_HOST = os.environ.get("TRAE2API_CONSOLE_HOST", "https://www.trae.cn")
API_HOST = os.environ.get("TRAE2API_API_HOST", "https://api.trae.cn")
SUCCESS_HTML = "<html><body><h3>trae2api: login received. You can close this tab.</h3></body></html>"

_legacy_credentials_file: Path | None = None


def configure_legacy_credentials(path: Path | None) -> None:
    global _legacy_credentials_file
    _legacy_credentials_file = Path(path) if path is not None else None


def default_credentials_file() -> Path:
    if _legacy_credentials_file is not None:
        return _legacy_credentials_file
    from .app_storage import root
    return root() / "trae2api.json"


class TraeLoginFlow:
    def __init__(
        self,
        client_id: str = DEFAULT_CLIENT_ID,
        console_host: str = CONSOLE_HOST,
        api_host: str = API_HOST,
        creds_file: str | os.PathLike | None = None,
        device_name: str | None = None,
        http: httpx.Client | None = None,
    ) -> None:
        self.client_id = client_id
        self.console_host = console_host.rstrip("/")
        self.api_host = api_host.rstrip("/")
        self.creds_file = Path(creds_file) if creds_file else default_credentials_file()

        self.http = http or httpx.Client(timeout=60, trust_env=False)
        self._owns_http = http is None
        self.identity_store = IdentityStore(self.creds_file)
        with self.identity_store.locked():
            self.identity = self.identity_store.load()
            self.identity_store.save(self.identity)
        self.device_id = self.identity.device_id
        self.machine_id = self.identity.machine_id
        self.device_name = device_name or f"{os.environ.get('USERNAME') or os.environ.get('USER') or 'trae'}2api"
        self.client_version = client_version("3.4.20")
        self.device_model = device_model()
        self.device_brand = manufacturer()
        self.os_name, self.os_version = os_metadata()
        self.device_cpu = cpu_brand()
        self.private_key_pem = self.identity.private_key_pem
        self.public_key_pem = self.identity.public_key_pem
        product = installed_client()
        self.user_agent = f"VSCode {product.editor_version} ({product.name})"
        self.plugin_version = product.build_version
        self.http.headers["User-Agent"] = self.user_agent

        # PKCE (Mxe(): verifier 48 random bytes base64url, challenge sha256 b64url)
        self.code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
        self.code_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(self.code_verifier.encode()).digest()
        ).decode().rstrip("=")

        self.login_trace_id = str(uuid.uuid4())
        self.result: dict = {}
        self.error_code: str | None = None
        self._server: ThreadingHTTPServer | None = None
        self._done = threading.Event()
        self.on_event = None

    def prepare_device(self) -> None:
        """Register once, resume activation if needed, and reuse the same key."""
        profile_path = self.creds_file.with_name(self.creds_file.stem + '.hardware.secret')
        if profile_path.exists():
            values = json.loads(profile_path.read_text(encoding='utf-8'))
            if not isinstance(values, dict) or set(values) != set(Hardware.__dataclass_fields__):
                raise ValueError('invalid login hardware profile')
            hardware = Hardware(**values)
        elif self.identity.registered:
            # Existing installations must keep the hardware that registered them.
            hardware = Hardware.read()
        else:
            hardware = Hardware.allocate()
            atomic_json(profile_path, asdict(hardware))
        result = AhaDeviceService(self.identity_store, http=self.http, version=self.client_version,
                                  hardware_reader=lambda: hardware).get_device_id(
            allow_network=True, require_activation=True)
        identity, hardware = result.identity, result.hardware
        self.identity = identity
        self.device_id, self.machine_id = identity.device_id, identity.machine_id
        self.private_key_pem, self.public_key_pem = identity.private_key_pem, identity.public_key_pem
        self.device_model, self.device_brand, self.device_cpu = hardware.model, hardware.brand, hardware.cpu
        self.os_name, self.os_version = hardware.os_name, hardware.os_version

    def _emit_event(self, event, value=None):
        if self.on_event is not None:
            self.on_event(event, value)

    def cancel(self) -> None:
        """Interrupt the callback wait; callers must still discard cancelled results."""
        self._done.set()

    # ---------- local callback server ----------

    def _start_server(self) -> int:
        flow = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                parsed = urlparse(self.path)
                qs = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                if parsed.path == "/authorize":
                    err = qs.get("error_code")
                    if err:
                        flow.error_code = err
                    auth_info_raw = qs.get("authCodeInfo", "")
                    try:
                        auth_info = json.loads(auth_info_raw) if auth_info_raw else {}
                    except json.JSONDecodeError:
                        auth_info = {}
                    auth_code = auth_info.get("AuthCode", "") if isinstance(auth_info, dict) else ""
                    if isinstance(auth_code, str) and auth_code:
                        flow.result["AuthCode"] = auth_code
                        flow.result["userInfo"] = qs.get("userInfo", "")
                        self._respond(SUCCESS_HTML)
                        flow._done.set()
                    elif err:
                        self._respond("<html><body>Login failed.</body></html>", 400)
                        flow._done.set()
                    else:
                        self._respond("<html><body>Missing authorization code.</body></html>", 400)
                elif parsed.path in ("/login-confirm", "/login-success"):
                    self._respond("<html><body>Waiting for authorization callback.</body></html>")
                else:
                    self._respond("<html><body>unknown path</body></html>", 404)

            def _respond(self, body: str, code: int = 200) -> None:
                data = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, fmt, *args):  # silence default logging
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = self._server.server_address[1]
        t = threading.Thread(target=self._server.serve_forever, daemon=True)
        t.start()
        return port

    # ---------- login url ----------

    def build_login_url(self, port: int) -> str:
        callback = f"http://127.0.0.1:{port}/authorize"
        params = {
            "login_version": "1",
            "auth_from": "trae",
            "login_channel": "native_ide",
            "plugin_version": self.plugin_version,
            "auth_type": "local",
            "client_id": self.client_id,
            "redirect": "0",
            "login_trace_id": self.login_trace_id,
            "auth_callback_url": callback,
            "machine_id": self.machine_id,
            "device_id": self.device_id,
            "x_device_id": self.device_id,
            "x_machine_id": self.machine_id,
            "x_device_brand": self.device_model,
            "x_device_type": self.os_name,
            "x_os_version": self.os_version,
            "x_app_version": self.client_version,
            "x_app_type": "trae",
            "code_challenge": self.code_challenge,
            "code_challenge_method": "S256",
        }
        return f"{self.console_host}/authorization?{urlencode(params)}"

    # ---------- exchange ----------

    def _device_info(self) -> dict:
        return {
            "DeviceID": self.device_id,
            "MachineID": self.machine_id,
            "PlatformCode": "IDE_PC",
            "DeviceType": "PC",
            "DeviceName": self.device_name,
            "DeviceModel": self.device_model,
            "ClientVersion": self.client_version,
            "DevicePublicKey": self.public_key_pem,
            "DeviceBrand": self.device_brand,
            "DeviceCPU": self.device_cpu,
            "OSInfo": self.os_name,
            "OSVersion": self.os_version,
        }

    def _post_json(self, url: str, body: dict, token: str = "") -> dict:
        try:
            response = self.http.post(url, content=json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode(),
                headers={"Content-Type": "application/json", "x-cloudide-token": token,
                         "User-Agent": self.user_agent}, follow_redirects=False)
        except httpx.HTTPError as exc:
            raise ValueError(f"login network error ({type(exc).__name__}); not retried") from None
        if response.status_code != 200:
            raise ValueError(f"login request failed: HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError:
            raise ValueError("login endpoint returned invalid JSON") from None
        if not isinstance(data, dict):
            raise ValueError("login endpoint returned an invalid object")
        return data

    def exchange_auth_code(self, auth_code: str) -> dict:
        url = f"{self.api_host}/trae/api/v3/oauth/ExchangeToken"
        body = {
            "ClientID": self.client_id,
            "AuthCode": auth_code,
            "CodeVerifier": self.code_verifier,
            "DeviceInfo": self._device_info(),
            "IDEVersion": self.client_version,
        }
        data = self._post_json(url, body)
        metadata = data.get("ResponseMetadata") or {}
        if not isinstance(metadata, dict):
            raise RuntimeError("ExchangeToken returned invalid metadata")
        err = metadata.get("Error") or {}
        if not isinstance(err, dict):
            raise RuntimeError("ExchangeToken returned an invalid error object")
        if err.get("Code"):
            raise RuntimeError("ExchangeToken was rejected by the server")
        result = data.get("Result") or {}
        if not isinstance(result, dict) or not isinstance(result.get("Token"), str) or not result["Token"]:
            raise RuntimeError("ExchangeToken returned no valid Token")
        return result

    def fetch_user_info(self, token: str) -> dict:
        url = f"{self.api_host}/cloudide/api/v3/trae/GetUserInfo"
        body = {"ReqSource": "IDE", "IDEVersion": self.client_version}
        data = self._post_json(url, body, token=token)
        metadata = data.get("ResponseMetadata") or {}
        if not isinstance(metadata, dict) or metadata.get("Error"):
            raise RuntimeError("GetUserInfo was rejected by the server")
        result = data.get("Result")
        if not isinstance(result, dict):
            raise RuntimeError("GetUserInfo returned no user profile")
        return result

    # ---------- orchestration ----------

    def start(self, open_browser: bool = True, timeout_sec: float = 1800.0, log=print) -> TraeAuth:
        try:
            self.prepare_device()
            return self._login(open_browser, timeout_sec, log)
        finally:
            if self._server is not None:
                self._server.shutdown()
                self._server.server_close()
                self._server = None
            if self._owns_http:
                self.http.close()

    def _login(self, open_browser: bool, timeout_sec: float, log) -> TraeAuth:
        port = self._start_server()
        url = self.build_login_url(port)
        log("=" * 70)
        log("trae2api login: open this URL in your browser and complete sign-in:")
        log(url)
        self._emit_event('auth_url', url)
        log("=" * 70)
        if open_browser:
            try:
                webbrowser.open(url)
            except Exception:
                pass
        if not self._done.wait(timeout_sec):
            raise TimeoutError("login timed out waiting for the OAuth callback")
        if self.error_code and "AuthCode" not in self.result:
            raise RuntimeError("browser authorization failed")
        auth_code = self.result["AuthCode"]
        self._emit_event('exchanging')
        log("[login] received auth code, exchanging tokens ...")
        result = self.exchange_auth_code(auth_code)
        self._emit_event('profile')
        log("[login] exchange ok, fetching user info ...")
        profile = self.fetch_user_info(result["Token"])
        if not profile.get("UserID"):
            raise RuntimeError("GetUserInfo did not identify the logged-in account; credentials were not replaced")

        auth = TraeAuth(creds_file=self.creds_file)
        auth.creds.token = result["Token"]
        auth.creds.refresh_token = result.get("RefreshToken", "")
        auth.creds.expired_at = _iso_from_ms_or_iso(result.get("TokenExpireAt"), result.get("TokenExpireDuration"))
        auth.creds.refresh_expired_at = _iso_from_ms_or_iso(result.get("RefreshExpireAt"))
        auth.creds.token_release_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        auth.creds.user_id = str(profile.get("UserID") or result.get("UserID") or "")
        auth.creds.host = self.api_host
        auth.creds.region = profile.get("AIRegion") or profile.get("Region") or "CN"
        auth.creds.username = profile.get("ScreenName", "")
        auth.creds.jwt = result["Token"]
        from .auth import _jwt_exp
        auth.creds.jwt_exp = _jwt_exp(result["Token"])
        auth.creds.extra = {"tenant_id": profile.get("TenantID", "")}

        auth.device.device_id = self.device_id
        auth.device.machine_id = self.machine_id
        auth.device.device_name = self.device_name
        auth.device.device_model = self.device_model
        auth.device.device_brand = self.device_brand
        auth.device.client_version = self.client_version
        auth.device.device_public_key = self.public_key_pem
        auth.device.device_cpu = self.device_cpu
        auth.device.os_info = self.os_name
        auth.device.os_version = self.os_version
        auth.private_key = serialization.load_pem_private_key(self.private_key_pem.encode(), password=None)

        # Save through a private temporary file so a failed write does not
        # truncate the previous independent login. Native storage is untouched.
        import tempfile
        with self.identity_store.locked():
            current = self.identity_store.load()
            if current.as_dict() != self.identity.as_dict():
                auth.http.close()
                raise RuntimeError("independent device profile changed during login; credentials were not replaced")
            fd, name = tempfile.mkstemp(prefix=".login-", suffix=".secret", dir=self.creds_file.parent)
            os.close(fd)
            try:
                auth.creds_file = Path(name)
                auth.save()
                os.replace(name, self.creds_file)
            finally:
                auth.creds_file = self.creds_file
                if os.path.exists(name):
                    os.unlink(name)
        log(f"[login] credentials saved to {self.creds_file}")
        log(f"[login] user {auth.creds.user_id} ({auth.creds.username or 'unknown'}) region={auth.creds.region}")
        log(f"[login] token expires {auth.creds.expired_at}, refresh expires {auth.creds.refresh_expired_at}")
        return auth


def _iso_from_ms_or_iso(value, duration_ms=None) -> str:
    from datetime import datetime, timezone
    try:
        if value is None and duration_ms:
            return datetime.fromtimestamp(time.time() + float(duration_ms) / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        if value is None:
            return ""
        v = float(value)
        if v > 10_000_000_000:  # ms epoch
            return datetime.fromtimestamp(v / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        if v > 10_000_000:  # s epoch
            return datetime.fromtimestamp(v, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError):
        pass
    return str(value or "")


def run_login(timeout_sec: float = 1800.0, open_browser: bool = True,
              creds_file: str | os.PathLike | None = None, prepare_only: bool = False) -> TraeAuth | None:
    flow = TraeLoginFlow(creds_file=creds_file)
    if prepare_only:
        try:
            flow.prepare_device()
            print(json.dumps({"action": "device_prepared", "registered": flow.identity.registered,
                              "activated": flow.identity.activated}))
        finally:
            flow.http.close()
        return None
    return flow.start(open_browser=open_browser, timeout_sec=timeout_sec)


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="buddy-trae-login")
    parser.add_argument("--timeout", type=float, default=1800.0)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--creds")
    parser.add_argument("--prepare-device", action="store_true")
    args = parser.parse_args(argv)
    try:
        run_login(timeout_sec=args.timeout, open_browser=not args.no_browser,
                  creds_file=args.creds, prepare_only=args.prepare_device)
    except (ValueError, RuntimeError, OSError, TimeoutError) as exc:
        parser.exit(1, f"Login failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
