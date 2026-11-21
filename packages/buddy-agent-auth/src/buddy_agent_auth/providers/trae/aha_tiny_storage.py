"""Read-only-by-default TinyStorage adapter with an explicit native-root helper."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import threading

from .aha_cache_codec import decode, encode
from .aha_device_cache import DeviceRecord
from .login_identity import atomic_json

PREF = "tiny_storage_data"
DEVICE = "aha.device.device_id"
REGION = "aha_device_region"


@dataclass(frozen=True)
class RegionRecord:
    ready: bool = False
    country_code: str = ""
    source: str = ""
    query: dict[str, str] | None = None
    headers: dict[str, str] | None = None

    @classmethod
    def load(cls, value: object) -> RegionRecord:
        if not isinstance(value, str):
            return cls()
        try:
            data = json.loads(decode(value))
        except (ValueError, UnicodeError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        def text(name):
            value = data.get(name)
            return value if isinstance(value, str) else ""
        def mapping(name):
            value = data.get(name)
            return {key: item for key, item in value.items() if isinstance(item, str)} if isinstance(value, dict) else None
        # 0x02844ce0 marks a parsed dictionary ready even when fields are absent.
        return cls(True, text("countryCode"), text("countryCodeSrc"), mapping("query"), mapping("headers"))

    def encoded(self, *, clock: int | None = None) -> str:
        data = {"countryCode": self.country_code, "countryCodeSrc": self.source}
        if self.query is not None:
            data["query"] = self.query
        if self.headers is not None:
            data["headers"] = self.headers
        return encode(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(), clock=clock)

    def updated(self, other: RegionRecord) -> RegionRecord:
        # RegionManager updates strings, but keeps optional maps when omitted.
        return RegionRecord(True, other.country_code, other.source,
                            self.query if other.query is None else other.query,
                            self.headers if other.headers is None else other.headers)


class NativeTinyStorage:
    def __init__(self, path: Path, *, writable: bool = False, legacy_preferences: Path | None = None):
        self.path, self.writable = Path(path), writable
        self.legacy_preferences = (Path(legacy_preferences) if legacy_preferences is not None else
                                   self.path.parent.parent / "Preferences"
                                   if self.path.name == "TinyStorage" and self.path.parent.name == "aha" else None)
        self._lock = threading.RLock()

    @classmethod
    def for_native_client(cls, *, client_root: Path | None = None, writable: bool = False):
        if client_root is None:
            roaming = os.environ.get("APPDATA")
            if not roaming:
                raise ValueError("native Windows cache root requires APPDATA or an explicit client_root")
            client_root = Path(roaming) / "Trae CN"
        return cls(Path(client_root) / "aha" / "TinyStorage", writable=writable)

    def _root(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (ValueError, UnicodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def get(self, key: str) -> str | None:
        try:
            root = self._root()
            if PREF not in root and self.legacy_preferences is not None:
                source = json.loads(self.legacy_preferences.read_text(encoding="utf-8-sig"))
                if isinstance(source, dict):
                    old = source.get(PREF)
                    values = dict(old) if isinstance(old, dict) else {}
                    if isinstance(source.get(DEVICE), str):
                        values[DEVICE] = source[DEVICE]
                    root[PREF] = values
                    if self.writable:
                        with self._lock:
                            # Recheck after acquiring the writer lock.
                            current = self._root()
                            if PREF not in current:
                                current[PREF] = values
                                atomic_json(self.path, current)
                            else:
                                root = current
            values = root.get(PREF)
        except (OSError, ValueError, UnicodeError):
            return None
        value = values.get(key) if isinstance(values, dict) else None
        return value if isinstance(value, str) else None

    def load_device(self, hardware_uuid: str) -> DeviceRecord | None:
        return DeviceRecord.load(self.get(DEVICE), hardware_uuid)

    def load_region(self) -> RegionRecord:
        return RegionRecord.load(self.get(REGION))

    def _set(self, key: str, value: str) -> None:
        if not self.writable:
            raise PermissionError("TinyStorage adapter is read-only")
        with self._lock:
            root = self._root()
            values = root.get(PREF)
            if not isinstance(values, dict):
                values = {}
                root[PREF] = values
            values[key] = value
            atomic_json(self.path, root)

    def save_device(self, record: DeviceRecord) -> None:
        self._set(DEVICE, record.encoded())

    def save_region(self, record: RegionRecord) -> None:
        self._set(REGION, record.encoded())
