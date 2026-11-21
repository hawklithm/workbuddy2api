"""Small Aha branches checked against allowlisted original PE instructions."""
from __future__ import annotations

from dataclasses import dataclass

UINT64_MAX = (1 << 64) - 1
_SPACE = b"\t\v\f \n\r"


@dataclass(frozen=True)
class ParsedUint64:
    ok: bool
    value: int


def parse_uint64(value: str) -> ParsedUint64:
    """RVA 0x041f5160, including false-result output values."""
    raw = value.encode("utf-8")
    clean, pos, number = True, 0, 0
    while pos < len(raw) and raw[pos] in _SPACE:
        clean, pos = False, pos + 1
    if pos == len(raw) or raw[pos] == 45:
        return ParsedUint64(False, 0)
    if raw[pos] == 43:
        pos += 1
    if pos == len(raw):
        return ParsedUint64(False, 0)
    for byte in raw[pos:]:
        if not 48 <= byte <= 57:
            return ParsedUint64(False, number)
        digit = byte - 48
        if number > UINT64_MAX // 10 or number == UINT64_MAX // 10 and digit >= 6:
            return ParsedUint64(False, UINT64_MAX)
        number = number * 10 + digit
    return ParsedUint64(clean, number)


def needs_registration(device_id: str, region: str, region_record_ready: bool) -> bool:
    """RVA 0x02849050; readiness belongs to decoded RegionManager data."""
    parsed = parse_uint64(device_id)
    if not parsed.ok or not parsed.value:
        return True
    if region in ("CN", "Cn", "cN", "cn"):
        return False
    return not region_record_ready


def retry_delay_ms(attempt: int) -> int:
    """RVA 0x0284eafb: uint32 arithmetic, five-attempt steps, 30s cap."""
    count = attempt & 0xffffffff
    calculated = (count // 5 * 1000 + 1000) & 0xffffffff
    return min(calculated, 30000)


@dataclass(frozen=True)
class OldIdMigration:
    accepted: bool
    device_id: str
    old_device_id: str
    write_cache: bool


def migrate_old_id(current_id: str, old_id: str, previous_old_id: str = "") -> OldIdMigration:
    """RVA 0x02849440. Telemetry checks do not reject the setter's input."""
    if not old_id or old_id == "0":
        return OldIdMigration(False, current_id, previous_old_id, False)
    return OldIdMigration(True, old_id if current_id else current_id, old_id, bool(current_id))
