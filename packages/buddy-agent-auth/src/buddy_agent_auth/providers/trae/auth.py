"""Trae CN credential loader + token lifecycle.

Sources (reverse engineered from Trae CN 3.4.4):
- Credentials live in `%APPDATA%/Trae CN/User/globalStorage/storage.json` under
  `iCubeAuthInfo://icube.cloudide` (AES-128-CBC wrapped, see credential_decrypt.py)
  and the device ECDSA keypair under `iCubeAuthInfo://icube-dc:<deviceId>`.
- `POST {host}/trae/api/v3/oauth/ExchangeToken` exchanges a RefreshToken for a
  fresh {Token, RefreshToken} pair. Requests carry an ECDSA P-256 DeviceProof:
      payload = "POST\n/trae/api/v3/oauth/ExchangeToken\n{ClientID}\n{RefreshToken}\n{ts}\n{nonce}"
      Signature = base64(ECDSA-P256-SHA256(payload))
- `POST {host}/api/v2/GetUserToken` (header `X-Cloudide-Token`) mints the
  long-lived RS256 JWT that is sent as `Authorization: Cloud-IDE-JWT <jwt>`.

The proxy is read-only w.r.t. the Trae install by default: it re-reads the
current tokens from storage.json, so the IDE remains the source of truth.
RefreshToken rotation is only performed when the stored token is already
expired (opt-in via TRAE2API_ALLOW_ROTATION=1) because the server may
invalidate the previous refresh token, which would log out the running IDE.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from .credential_decrypt import decrypt_value
from .client_metadata import client_version

DEFAULT_CLIENT_ID = "ono9krqynydwx5"  # Jy() default for TRAE (non-SOLO)
EXCHANGE_PATH = "/trae/api/v3/oauth/ExchangeToken"
IDE_VERSION = "3.4.4"
USER_AGENT = "trae-cn/3.4.4"

DEFAULT_STORAGE = Path(os.environ.get("APPDATA", "")) / "Trae CN" / "User" / "globalStorage" / "storage.json"


@dataclass
class DeviceInfo:
    """Defaults mirror the real Trae client fingerprint on this machine
    (observed in %APPDATA%/Trae CN/logs/.../completion.log)."""
    device_id: str = ""
    machine_id: str = ""
    platform_code: str = "IDE_PC"
    device_type: str = "PC"
    device_name: str = "trae2api"
    device_model: str = "MS-7B89"
    client_version: str = field(default_factory=client_version)
    device_public_key: str = ""
    device_brand: str = "MS-7B89"
    device_cpu: str = "AMD"
    os_info: str = "Windows"
    os_version: str = "Windows 10 Education"

    def to_payload(self) -> dict:
        return {
            "DeviceID": self.device_id,
            "MachineID": self.machine_id,
            "PlatformCode": self.platform_code,
            "DeviceType": self.device_type,
            "DeviceName": self.device_name,
            "DeviceModel": self.device_model,
            "ClientVersion": self.client_version,
            "DevicePublicKey": self.device_public_key,
            "DeviceBrand": self.device_brand,
            "DeviceCPU": self.device_cpu,
            "OSInfo": self.os_info,
            "OSVersion": self.os_version,
        }


@dataclass
class TraeCredentials:
    token: str = ""
    refresh_token: str = ""
    expired_at: str = ""
    refresh_expired_at: str = ""
    token_release_at: str = ""
    user_id: str = ""
    tenant_id: str = ""
    host: str = "https://api.trae.cn"
    region: str = "CN"
    username: str = ""
    jwt: str = ""
    jwt_exp: float = 0.0
    extra: dict = field(default_factory=dict)


def _parse_iso(ts: str) -> float:
    if not ts:
        return 0.0
    try:
        from datetime import datetime, timezone
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _jwt_exp(jwt: str) -> float:
    try:
        payload_b64 = jwt.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return float(payload.get("exp", 0))
    except Exception:
        return 0.0


class TraeAuth:
    """Loads Trae credentials and keeps a usable Cloud-IDE-JWT on hand."""

    def __init__(
        self,
        storage_path: str | os.PathLike | None = None,
        creds_file: str | os.PathLike | None = None,
        client_id: str = DEFAULT_CLIENT_ID,
        http: httpx.Client | None = None,
    ) -> None:
        self.storage_path = Path(storage_path) if storage_path else DEFAULT_STORAGE
        self.creds_file = Path(creds_file) if creds_file else Path.home() / ".traeapi" / "trae2api.json"
        self.client_id = client_id
        self.http = http or httpx.Client(timeout=30, headers={"User-Agent": USER_AGENT})
        self.creds = TraeCredentials()
        self.device = DeviceInfo()
        self.private_key = None

    # ---------- loading ----------

    def load(self) -> TraeCredentials:
        """Load credentials: own file first, then live Trae storage.json."""
        if self.creds_file.exists():
            data = json.loads(self.creds_file.read_text(encoding="utf-8"))
            auth = data.get("auth", data)
            self.creds = TraeCredentials(
                token=auth.get("token", ""),
                refresh_token=auth.get("refreshToken", ""),
                expired_at=auth.get("expiredAt", ""),
                refresh_expired_at=auth.get("refreshExpiredAt", ""),
                token_release_at=auth.get("tokenReleaseAt", ""),
                user_id=auth.get("userId", ""),
                host=auth.get("host", "https://api.trae.cn"),
                region=(auth.get("userRegion") or {}).get("region", "CN") if isinstance(auth.get("userRegion"), dict) else str(auth.get("userRegion") or "CN"),
                username=auth.get("username", ""),
                jwt=auth.get("jwt", ""),
                jwt_exp=auth.get("jwt_exp", 0.0),
                extra=auth.get("extra", {}),
            )
            dev = data.get("device", {})
            self.device = DeviceInfo(
                device_id=dev.get("deviceId", ""),
                machine_id=dev.get("machineId", ""),
                device_name=dev.get("deviceName", "trae2api"),
                device_model=dev.get("deviceModel", ""),
                device_brand=dev.get("deviceBrand", ""),
                device_cpu=dev.get("deviceCpu", ""),
                client_version=dev.get("clientVersion") or client_version(),
                os_info=dev.get("osInfo", "Windows"),
                os_version=dev.get("osVersion", "10"),
            )
            if dev.get("privateKeyPEM"):
                self.private_key = serialization.load_pem_private_key(dev["privateKeyPEM"].encode(), password=None)
                self.device.device_public_key = self.private_key.public_key().public_bytes(
                    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
                ).decode()
            if not self.creds.jwt and self.creds.token:
                self.creds.jwt = self.creds.token
                self.creds.jwt_exp = _jwt_exp(self.creds.jwt)
            return self.creds

        if self.storage_path.exists():
            return self._load_from_trae_storage()
        raise FileNotFoundError(
            f"No credentials: neither {self.creds_file} nor Trae storage at {self.storage_path}"
        )

    def _load_from_trae_storage(self) -> TraeCredentials:
        raw = json.loads(self.storage_path.read_text(encoding="utf-8"))
        auth_plain = decrypt_value(raw["iCubeAuthInfo://icube.cloudide"])
        if not auth_plain:
            raise ValueError("Failed to decrypt iCubeAuthInfo://icube.cloudide")
        auth = json.loads(auth_plain)
        self.creds = TraeCredentials(
            token=auth.get("token", ""),
            refresh_token=auth.get("refreshToken", ""),
            expired_at=auth.get("expiredAt", ""),
            refresh_expired_at=auth.get("refreshExpiredAt", ""),
            token_release_at=auth.get("tokenReleaseAt", ""),
            user_id=auth.get("userId", ""),
            host=auth.get("host", "https://api.trae.cn"),
            region=(auth.get("userRegion") or {}).get("region", "CN"),
            username=auth.get("account", {}).get("username", ""),
        )
        # device keypair (optional; needed only for refresh)
        dc_key = next((k for k in raw if k.startswith("iCubeAuthInfo://icube-dc:")), None)
        if dc_key:
            dc_plain = decrypt_value(raw[dc_key])
            if dc_plain:
                dc = json.loads(dc_plain)
                try:
                    self.private_key = serialization.load_pem_private_key(dc["privateKeyPEM"].encode(), password=None)
                    pub = self.private_key.public_key().public_bytes(
                        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
                    ).decode()
                    self.device.device_public_key = pub
                    self.device.device_id = dc_key.rsplit(":", 1)[1]
                except Exception:
                    self.private_key = None
        # machineId from same file
        mid = raw.get("telemetry.machineId")
        if mid:
            self.device.machine_id = mid
        self.creds.jwt = self.creds.token
        self.creds.jwt_exp = _jwt_exp(self.creds.jwt)
        return self.creds

    def save(self) -> None:
        data = {
            "auth": {
                "token": self.creds.token,
                "refreshToken": self.creds.refresh_token,
                "expiredAt": self.creds.expired_at,
                "refreshExpiredAt": self.creds.refresh_expired_at,
                "tokenReleaseAt": self.creds.token_release_at,
                "userId": self.creds.user_id,
                "host": self.creds.host,
                "userRegion": {"region": self.creds.region},
                "username": self.creds.username,
                "jwt": self.creds.jwt,
                "jwt_exp": self.creds.jwt_exp,
                "extra": self.creds.extra,
            },
            "device": {
                "deviceId": self.device.device_id,
                "machineId": self.device.machine_id,
                "deviceName": self.device.device_name,
                "deviceModel": self.device.device_model,
                "deviceBrand": self.device.device_brand,
                "deviceCpu": self.device.device_cpu,
                "clientVersion": self.device.client_version,
                "osInfo": self.device.os_info,
                "osVersion": self.device.os_version,
                "privateKeyPEM": self.private_key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                ).decode() if self.private_key else "",
            },
        }
        self.creds_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    # ---------- token lifecycle ----------

    def token_valid(self, margin_sec: float = 3600) -> bool:
        if not self.creds.jwt:
            return False
        exp = self.creds.jwt_exp or _jwt_exp(self.creds.jwt)
        if not exp:
            return False
        return time.time() < exp - margin_sec

    def cloudide_token_expired(self) -> bool:
        exp = _parse_iso(self.creds.expired_at)
        return bool(exp and time.time() > exp - 3600)

    def get_jwt(self, force_refresh: bool = False) -> str:
        """Return a valid Cloud-IDE-JWT, refreshing as needed (non-destructive first)."""
        if force_refresh or not self.token_valid():
            self.refresh_jwt()
        return self.creds.jwt

    def refresh_jwt(self) -> str:
        """Mint a fresh JWT via /api/v2/GetUserToken from the cloudide token."""
        if self.cloudide_token_expired():
            allow_rotation = os.environ.get("TRAE2API_ALLOW_ROTATION") == "1"
            if not allow_rotation:
                raise RuntimeError(
                    "cloudide token expired; set TRAE2API_ALLOW_ROTATION=1 to rotate "
                    "the RefreshToken (this may log out the running Trae IDE)"
                )
            self.exchange_refresh_token()
        url = f"{self.creds.host}/api/v2/GetUserToken"
        r = self.http.post(url, json={}, headers={"X-Cloudide-Token": self.creds.token})
        r.raise_for_status()
        result = (r.json() or {}).get("Result") or {}
        jwt = result.get("Token")
        if not jwt:
            raise RuntimeError(f"GetUserToken returned no Token: {list((r.json() or {}).keys())}")
        self.creds.jwt = jwt
        self.creds.jwt_exp = _jwt_exp(jwt)
        if result.get("UserID"):
            self.creds.user_id = str(result["UserID"])
        if result.get("TenantID"):
            self.creds.tenant_id = str(result["TenantID"])
        return self.creds.jwt

    # ---------- refresh token exchange (rotates; use carefully) ----------

    def sign_device_proof(self, method: str, path: str) -> dict:
        if self.private_key is None:
            raise RuntimeError("device private key not loaded; cannot sign DeviceProof")
        ts = int(time.time())
        nonce = secrets.token_hex(16)
        payload = "\n".join([method, path, self.client_id, self.creds.refresh_token, str(ts), nonce])
        sig = self.private_key.sign(payload.encode(), ec.ECDSA(hashes.SHA256()))
        return {"Signature": base64.b64encode(sig).decode(), "Timestamp": ts, "Nonce": nonce}

    def exchange_refresh_token(self) -> dict:
        """POST /trae/api/v3/oauth/ExchangeToken (rotates the refresh token)."""
        path = EXCHANGE_PATH
        proof = self.sign_device_proof("POST", path)
        body = {
            "ClientID": self.client_id,
            "ClientSecret": "",
            "RefreshToken": self.creds.refresh_token,
            "DeviceInfo": self.device.to_payload(),
            "DeviceProof": proof,
            "IDEVersion": IDE_VERSION,
        }
        r = self.http.post(f"{self.creds.host}{path}", json=body)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        err = (data.get("ResponseMetadata") or {}).get("Error") or {}
        if err.get("Code"):
            raise RuntimeError(f"ExchangeToken error {err.get('Code')}: {err.get('Message')}")
        result = data.get("Result") or {}
        token = result.get("Token")
        if not token:
            raise RuntimeError(f"ExchangeToken returned no Token (HTTP {r.status_code}): {r.text[:200]}")
        self.creds.token = token
        self.creds.refresh_token = result.get("RefreshToken", self.creds.refresh_token)
        self.creds.expired_at = _ms_to_iso(result.get("TokenExpireAt"))
        self.creds.refresh_expired_at = _ms_to_iso(result.get("RefreshExpireAt"))
        self.creds.jwt = token
        self.creds.jwt_exp = _jwt_exp(token)
        return result

    # ---------- headers ----------

    def auth_headers(self) -> dict:
        jwt = self.get_jwt()
        return {
            "Authorization": f"Cloud-IDE-JWT {jwt}",
            "X-User-Region": self.creds.region,
        }

    def internal_headers(self) -> dict:
        jwt = self.get_jwt()
        return {"x-cloudide-token": jwt}


def _ms_to_iso(ms) -> str:
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(float(ms) / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return ""
