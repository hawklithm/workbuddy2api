"""Independent, persistent login device identity; never reads native credentials."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import secrets
import tempfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".login-", suffix=".secret", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def new_key() -> str:
    return ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()


@dataclass
class LoginIdentity:
    machine_id: str = field(repr=False)
    private_key_pem: str = field(repr=False)
    device_id: str = field(default="", repr=False)
    install_id: str = field(default="", repr=False)
    registered: bool = False
    activated: bool = False
    region: str = "CN"
    hardware_uuid: str = field(default="", repr=False)
    device_cache: object = field(default=None, repr=False)
    device_region: object = field(default=None, repr=False)

    @property
    def public_key_pem(self) -> str:
        key = serialization.load_pem_private_key(self.private_key_pem.encode(), password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
            raise ValueError("independent device key must use ECDSA P-256")
        return key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()

    def as_dict(self) -> dict:
        result = {"version": 1, "machineId": self.machine_id, "privateKeyPEM": self.private_key_pem,
                "deviceId": self.device_id, "installId": self.install_id, "registered": self.registered,
                "activated": self.activated, "region": self.region, "hardwareUuid": self.hardware_uuid}
        if self.device_cache is not None:
            result["ahaDeviceCache"] = self.device_cache
        if self.device_region is not None:
            result["ahaDeviceRegion"] = self.device_region
        return result


class IdentityStore:
    def __init__(self, credentials: Path):
        self.credentials = credentials
        self.path = credentials.with_name(credentials.stem + ".device.secret")

    @contextmanager
    def locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.path.with_name(self.path.name + ".lock.secret")
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise ValueError("another login is using this independent device profile") from None
        try:
            os.close(fd)
            yield
        finally:
            lock.unlink()

    def load(self) -> LoginIdentity:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if data.get("version") != 1:
                    raise ValueError()
                identity = LoginIdentity(data["machineId"], data["privateKeyPEM"], data["deviceId"],
                    data["installId"], data["registered"], data["activated"], data["region"], data["hardwareUuid"],
                    data.get("ahaDeviceCache") if data.get("ahaDeviceCache") is not None else
                    ({} if "ahaDeviceCache" in data else None), data.get("ahaDeviceRegion"))
                if (not isinstance(identity.machine_id, str) or not identity.machine_id
                        or type(identity.registered) is not bool or type(identity.activated) is not bool
                        or identity.activated and not identity.registered):
                    raise ValueError()
                if identity.registered:
                    if any(not isinstance(value, str) for value in
                           (identity.device_id, identity.install_id, identity.hardware_uuid)):
                        raise ValueError()
                    # New records have their own native-compatible cache
                    # validator; legacy profiles retain the old strict check.
                    if identity.device_cache is None and (not identity.device_id.isdecimal()
                            or int(identity.device_id) <= 0 or not identity.install_id.isdecimal()
                            or int(identity.install_id) <= 0 or not identity.hardware_uuid):
                        raise ValueError()
                identity.public_key_pem
                return identity
            except (KeyError, TypeError, ValueError, AttributeError):
                raise ValueError("invalid independent device profile; refusing to replace its identity") from None
        # Migrate only the explicitly selected independent credential file.
        # Old random device IDs are retained as unverified until registration.
        device = {}
        if self.credentials.exists():
            try:
                data = json.loads(self.credentials.read_text(encoding="utf-8"))
                device = data.get("device", {})
                if not isinstance(device, dict):
                    raise ValueError()
            except (ValueError, AttributeError):
                raise ValueError("invalid existing independent credentials") from None
        identity = LoginIdentity(device.get("machineId") or hashlib.sha256(secrets.token_bytes(32)).hexdigest(),
                                 device.get("privateKeyPEM") or new_key(), device.get("deviceId") or "")
        identity.public_key_pem
        return identity

    def save(self, identity: LoginIdentity) -> None:
        atomic_json(self.path, identity.as_dict())
        from .app_storage import save_tiny_storage
        save_tiny_storage(identity)
