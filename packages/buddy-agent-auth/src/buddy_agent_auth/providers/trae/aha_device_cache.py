"""Native Aha record semantics and envelope, with legacy profile support."""
from __future__ import annotations

from dataclasses import dataclass, field
import json

from .aha_cache_codec import decode, encode
from .aha_native_semantics import parse_uint64


@dataclass(frozen=True)
class DeviceRecord:
    device_id: str = field(repr=False)
    install_id: str = field(repr=False)
    uuid: str = field(repr=False)

    def as_dict(self) -> dict:
        return {"device_id_str": self.device_id, "install_id_str": self.install_id,
                "uuid": self.uuid, "version": "1.2"}

    def encoded(self, *, clock: int | None = None) -> str:
        raw = json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
        return encode(raw, clock=clock)

    @classmethod
    def load(cls, payload: object, hardware_uuid: str) -> DeviceRecord | None:
        # An invalid device record is a cache miss, not a damaged login key.
        if isinstance(payload, str):
            try:
                payload = json.loads(decode(payload))
            except (ValueError, UnicodeError):
                return None
        if not isinstance(payload, dict) or payload.get("version") != "1.2":
            return None
        values = [payload.get(key) for key in ("device_id_str", "install_id_str", "uuid")]
        if any(not isinstance(value, str) for value in values):
            return None
        device_id, install_id, cached_uuid = values
        parsed = parse_uint64(device_id)
        if not parsed.ok or not parsed.value:
            return None
        if cached_uuid != hardware_uuid:
            return None
        return cls(device_id, install_id, cached_uuid)
