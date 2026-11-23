"""Explicit HTTP adapter for NativeAhaDeviceService; no account endpoints."""
from __future__ import annotations

import asyncio
import json

import httpx

from .aha_native_lifecycle import RegisterReply, ActiveReply
from .aha_registration import REGISTER_URL, ACTIVE_URL, registration_request
from .device_hardware import Hardware

MAX_RESPONSE_SIZE = 0x100000  # DoRequest -> DownloadToString, RVA 0x02849f21.


class NativeHttpBackend:
    def __init__(self, client: httpx.Client, hardware: Hardware, version: str):
        self.client, self.hardware, self.version = client, hardware, version

    def _post(self, url: str, query: dict, body: bytes, headers: dict):
        with self.client.stream("POST", url, params=query, content=body, headers=headers,
                                follow_redirects=True) as response:
            result = bytearray()
            for chunk in response.iter_bytes(65536):
                if len(result) + len(chunk) > MAX_RESPONSE_SIZE:
                    return response.status_code, -1, b"", response.headers
                result.extend(chunk)
            return response.status_code, 0, bytes(result), response.headers

    async def register(self, device_id: str, install_id: str) -> RegisterReply:
        query, body = registration_request(self.hardware, self.version,
                                            device_id=device_id, install_id=install_id)
        try:
            status, error, body, headers = await asyncio.to_thread(self._post, REGISTER_URL, query, body,
                {"Content-Type": "application/json", "Content-Encoding": "gzip"})
        except httpx.HTTPError:
            # The state machine uses zero/nonzero. TTNet's precise error-number
            # taxonomy is not synthesized by this HTTPX transport adapter.
            return RegisterReply(status=0, net_error=-1)
        try:
            value = json.loads(body.decode("utf-8-sig"))
        except (ValueError, UnicodeError):
            value = None
        return RegisterReply(value.get("device_id_str") if isinstance(value, dict) else None,
                             value.get("install_id_str") if isinstance(value, dict) else None,
                             status=status, net_error=error,
                             region_record_ready=True if "x-tt-store-region" in headers else None,
                             country_code=headers.get("x-tt-store-region"),
                             country_source=headers.get("x-tt-store-region-src", "did"))

    async def activate(self, device_id: str, install_id: str) -> ActiveReply:
        query = {"aid": "787976", "app_name": "Trae CN", "version_code": self.version,
                 "channel": "stable", "os": "Windows", "device_platform": "PC",
                 "device_id": device_id, "iid": install_id,
                 "pc_uuid": self.hardware.pc_uuid, "pc_serial": self.hardware.pc_serial}
        try:
            status, error, _, _ = await asyncio.to_thread(self._post, ACTIVE_URL, query, b"",
                {"Content-Type": "application/json"})
            return ActiveReply(status=status, net_error=error)
        except httpx.HTTPError:
            return ActiveReply(status=0, net_error=-1)
