"""Read native credentials, optionally verifying an existing native device."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
from typing import Literal

from .credential_decrypt import decrypt_value

DeviceIdSource = Literal["existing", "get-device-id"]


class NativeIdentityError(ValueError):
    pass


def native_user_data() -> Path:
    location = os.environ.get("TRAE2API_USER_DATA")
    if location:
        return Path(location)
    if os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "Trae CN"
    raise NativeIdentityError("native user-data directory unavailable; set TRAE2API_USER_DATA")


@dataclass(frozen=True)
class NativeIdentity:
    user_data: Path
    user_id: str = field(repr=False)
    token: str = field(repr=False)
    device_id: str = field(repr=False)
    session: str
    user_region: str
    device_id_source: DeviceIdSource = "get-device-id"

    @classmethod
    def load(cls, directory: Path | None = None, *,
             device_id_source: DeviceIdSource = "get-device-id") -> "NativeIdentity":
        if device_id_source not in ("existing", "get-device-id"):
            raise NativeIdentityError("unsupported native device ID source")
        root = directory if directory is not None else native_user_data()
        try:
            raw = json.loads((root / "User/globalStorage/storage.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise NativeIdentityError("cannot read native credential storage; sign in to Trae CN first") from None
        if not isinstance(raw, dict):
            raise NativeIdentityError("invalid native credential storage")
        encrypted = raw.get("iCubeAuthInfo://icube.cloudide")
        try:
            plain = decrypt_value(encrypted) if isinstance(encrypted, str) else None
            auth = json.loads(plain) if plain else {}
        except Exception:
            raise NativeIdentityError("cannot decode native login") from None
        if not isinstance(auth, dict):
            raise NativeIdentityError("invalid native login")
        token, user_id, account = auth.get("token"), auth.get("userId"), auth.get("account")
        if not isinstance(token, str) or not token or not isinstance(user_id, str) or not user_id:
            raise NativeIdentityError("native login is missing its token or account ID")
        if not isinstance(account, dict) or account.get("scope") != "marscode":
            raise NativeIdentityError("native check-in requires a CN marscode account")
        # Account-bound devices are resolved by app_storage after authentication.
        # Neither native device cache nor session logs are needed for this mode.
        if device_id_source == "get-device-id":
            return cls(root, user_id, token, "", "", "CN", device_id_source)

        try:
            sessions = sorted((p for p in (root / "logs").iterdir()
                               if p.is_dir() and re.fullmatch(r"\d{8}T\d{6}", p.name)),
                              key=lambda p: p.name, reverse=True)
            if not sessions:
                raise NativeIdentityError("no native session log available to verify the remote device ID")
            log = sessions[0] / "main.log"
            if log.stat().st_size > 32 * 1024 * 1024:
                raise NativeIdentityError("native session log exceeds the verification size limit")
            text = log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            raise NativeIdentityError("cannot read the latest native session log; start Trae CN first") from None

        resolved = list(re.finditer(r"\[ICDRS\] \(init\) resolve rdid:\s*([^\s,]+)", text))
        initialized = list(re.finditer(
            r"\[ICDRS\] \(init\) initialization done, did:\s*([^\s,]+), ldid:\s*([^\s,]+), rdid:\s*([^\s,]+)", text)
        )
        if not resolved or not initialized:
            raise NativeIdentityError("latest native session has no completed remote device resolution")
        initialized_id, local_id, remote_id = initialized[-1].groups()
        device_id = resolved[-1][1]
        # A late resolution after initialization updates both live IDs.
        initialized_after_resolution = initialized[-1].start() > resolved[-1].start()
        if (device_id in ("", "0") or device_id == local_id
                or (initialized_after_resolution and (initialized_id != device_id or remote_id != device_id))):
            raise NativeIdentityError("native session device evidence is inconsistent or uses a local fallback")
        if "iCubeAuthInfo://icube-dc:" + device_id not in raw:
            raise NativeIdentityError("native remote device ID does not match a native OAuth device record")

        regions = []
        for line in text.splitlines():
            if "getStoreRegion" in line:
                regions.extend(re.findall(r'''["']?region["']?\s*[:=]\s*["']?(CN|SG|US|USTTP|UNKNOWN)\b''', line))
        # This mode is for the reviewed CN installation. Never substitute the
        # independent proxy credential's AIRegion for a native store region.
        if not regions:
            raise NativeIdentityError("native store region unavailable in the latest session log")
        return cls(root, user_id, token, device_id, sessions[0].name, regions[-1], device_id_source)

    def ensure_current(self) -> None:
        current = type(self).load(self.user_data, device_id_source=self.device_id_source)
        if current != self:
            raise NativeIdentityError("native login, device, region or session changed; rerun the command")
