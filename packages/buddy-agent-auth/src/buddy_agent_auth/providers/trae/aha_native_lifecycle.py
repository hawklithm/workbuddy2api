"""Native Aha device lifecycle, separated from OAuth installation-key policy.

The backend is injected. Merely constructing/reading this service cannot issue
requests; start(allow_network=True) is an explicit lifecycle operation.
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol
import uuid

from .aha_device_cache import DeviceRecord
from .aha_native_semantics import migrate_old_id, needs_registration, retry_delay_ms


@dataclass(frozen=True)
class RegisterReply:
    device_id: object = None
    install_id: object = None
    status: int = 200
    net_error: int = 0
    region_record_ready: bool | None = None
    country_code: str | None = None
    country_source: str = "did"


@dataclass(frozen=True)
class ActiveReply:
    status: int = 200
    net_error: int = 0


class NativeBackend(Protocol):
    async def register(self, device_id: str, install_id: str) -> RegisterReply: ...
    async def activate(self, device_id: str, install_id: str) -> ActiveReply: ...


class NativeAhaDeviceService:
    def __init__(self, *, hardware_uuid: str | None, record: DeviceRecord | None = None,
                 local_device_id: str | None = None,
                 app_region: str = "CN", region_record_ready: bool = False,
                 flags_2: bool = False, flags_3: bool = False,
                 backend: NativeBackend | None = None,
                 save_cache: Callable[[DeviceRecord], Awaitable[None]] | None = None,
                 update_region: Callable[[str, str], Awaitable[None]] | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.hardware_uuid = hardware_uuid
        self.local_device_id = (local_device_id if local_device_id is not None else
                                hardware_uuid if hardware_uuid is not None else str(uuid.uuid4()))
        self.device_id = record.device_id if record else ""
        self.install_id = record.install_id if record else ""
        self.old_device_id = ""
        self.main_id = self.device_id or "0"
        self.remote_id = self.device_id or "0"
        self.app_region, self.region_record_ready = app_region, region_record_ready
        self.defer_callback = bool(flags_2 and flags_3)
        # The reviewed GetDeviceId wrapper initializes the four-byte context
        # to zero at 0x0284a683. The two-bit branch is retained for other inputs.
        self.backend, self.save_cache, self.sleep = backend, save_cache, sleep
        self.update_region = update_region
        self.network_type = 0  # Native connection type 6 is unavailable.
        self.in_flight = False
        self.waiting_network = False
        self.last_error: str | None = None
        self.last_cache_error: str | None = None
        self.retry_delays = deque(maxlen=256)
        self._enabled = False
        self._worker: asyncio.Task | None = None
        self._getters: set[asyncio.Future] = set()
        self._resolutions: set[asyncio.Task] = set()
        self._wrappers: set[asyncio.Task] = set()

    def start_wrapper(self, *, timeout: float = 40.0) -> asyncio.Task:
        task = asyncio.create_task(self.resolve(timeout=timeout))
        self._wrappers.add(task)
        def finished(done):
            self._wrappers.discard(done)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(finished)
        return task

    def _needs_registration(self) -> bool:
        return needs_registration(self.device_id, self.app_region, self.region_record_ready)

    async def start(self, *, allow_network: bool = False) -> None:
        if not allow_network:
            return
        if self.backend is None:
            raise ValueError("native lifecycle requires an explicitly supplied backend")
        self._enabled = True
        if self._needs_registration() and (self._worker is None or self._worker.done()):
            self._worker = asyncio.create_task(self._cycle())

    async def _save(self) -> None:
        if self.save_cache is not None:
            try:
                await self.save_cache(DeviceRecord(self.device_id, self.install_id, self.hardware_uuid or ""))
            except OSError as error:
                self.last_cache_error = type(error).__name__

    async def set_old_device_id(self, value: str) -> bool:
        result = migrate_old_id(self.device_id, value, self.old_device_id)
        self.device_id, self.old_device_id = result.device_id, result.old_device_id
        if result.write_cache:
            await self._save()
        return result.accepted

    async def get_device_id(self, old_device_id: str | None = None) -> str:
        if old_device_id is not None:
            await self.set_old_device_id(old_device_id)
        # Native checks NONEMPTY here, not the numeric validity of the ID.
        if self.device_id:
            return self.device_id
        future = asyncio.get_running_loop().create_future()
        self._getters.add(future)
        try:
            return await future
        finally:
            self._getters.discard(future)

    def _notify(self) -> None:
        for future in tuple(self._getters):
            if not future.done():
                future.set_result(self.device_id)

    async def resolve(self, *, timeout: float = 40.0, old_device_id: str | None = None) -> dict[str, str]:
        if timeout < 0:
            raise ValueError("timeout must be nonnegative")
        task = asyncio.create_task(self.get_device_id(old_device_id))
        self._resolutions.add(task)
        def resolved(done):
            self._resolutions.discard(done)
            if not done.cancelled() and done.exception() is None:
                self.main_id = self.remote_id = done.result()
        task.add_done_callback(resolved)
        try:
            self.main_id = self.remote_id = await asyncio.wait_for(asyncio.shield(task), timeout)
        except asyncio.TimeoutError:
            if not self.main_id or self.main_id == "0":
                self.main_id = self.local_device_id
        return self.get_all_ids()

    def get_all_ids(self) -> dict[str, str]:
        return {"deviceId": self.main_id, "localDeviceId": self.local_device_id, "remoteDeviceId": self.remote_id}

    async def _cycle(self):
        attempt = 0
        while self._enabled:
            self.in_flight = True
            try:
                reply = await self.backend.register(self.device_id, self.install_id)
                parsed = (reply.net_error == 0 and reply.status == 200
                          and isinstance(reply.device_id, str) and isinstance(reply.install_id, str))
                if parsed:
                    # Parser copies both fields BEFORE the success check.
                    self.device_id, self.install_id = reply.device_id, reply.install_id
                success = parsed and self.device_id not in ("", "0")
                if success:
                    await self._save()
                    if not self.defer_callback:
                        self._notify()
                    activation = asyncio.create_task(self.backend.activate(self.device_id, self.install_id))
                    try:
                        # Native dispatches activation, then persists region;
                        # activation response handling follows that callback.
                        await asyncio.sleep(0)
                        if reply.region_record_ready is not None:
                            self.region_record_ready = reply.region_record_ready
                        if reply.country_code is not None and self.update_region is not None:
                            try:
                                await self.update_region(reply.country_code, reply.country_source)
                            except OSError as error:
                                self.last_cache_error = type(error).__name__
                        active = await activation
                    finally:
                        if not activation.done():
                            activation.cancel()
                            await asyncio.gather(activation, return_exceptions=True)
                    # Body parsing is diagnostic in the original callback.
                    active_ok = active.status == 200 and active.net_error == 0
                    if not self.defer_callback or active_ok:
                        if self.defer_callback:
                            self._notify()
                        self.last_error = None if active_ok else "activation transport failed after early delivery"
                        return
                    self.last_error = "activation transport failed"
                else:
                    self.last_error = "registration failed"
            finally:
                self.in_flight = False
            if self.network_type == 6:
                self.waiting_network = True
                return
            delay = retry_delay_ms(attempt)
            self.retry_delays.append(delay)
            attempt = (attempt + 1) & 0xffffffff
            await self.sleep(delay / 1000)

    async def on_network_change(self, connection_type: int) -> None:
        self.network_type = connection_type
        if (not self._enabled or connection_type == 6 or self.in_flight
                or not self._needs_registration()):
            return
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        self.waiting_network = False
        self._worker = asyncio.create_task(self._cycle())

    async def wait_worker(self) -> None:
        if self._worker is not None:
            await self._worker

    async def close(self) -> None:
        self._enabled = False
        tasks = list(self._resolutions | self._wrappers)
        if self._worker is not None:
            tasks.append(self._worker)
        for task in tasks:
            task.cancel()
        for future in tuple(self._getters):
            future.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
