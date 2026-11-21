"""Independent implementation of reviewed Aha device-ID service behavior.

The default operation is offline. Remote IDs come from a validated cache or
registration; the hardware UUID is exposed separately as the local fallback.
The native lifecycle accepts an explicit TinyStorage adapter (read-only by
default); the existing login adapter retains the independent identity store.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import Future
from copy import deepcopy
from dataclasses import dataclass, field, replace
import threading
from typing import Callable, Literal

import httpx

from .aha_registration import Registration, activate, register
from .aha_device_cache import DeviceRecord
from .device_hardware import Hardware
from .login_identity import IdentityStore, LoginIdentity, new_key


@dataclass(frozen=True)
class DeviceIdResult:
    identity: LoginIdentity = field(repr=False)
    hardware: Hardware = field(repr=False)
    source: Literal["local", "cache", "registration", "timeout", "native"]
    main_device_id: str | None = field(default=None, repr=False)
    sdk_device_id: str | None = field(default=None, repr=False)
    sdk_install_id: str | None = field(default=None, repr=False)

    @property
    def device_id(self) -> str:
        return self.main_device_id if self.main_device_id is not None else self.remote_device_id

    @property
    def remote_device_id(self) -> str:
        if self.sdk_device_id is not None:
            return self.sdk_device_id
        return self.identity.device_id if self.identity.registered else "0"

    @property
    def install_id(self) -> str:
        if self.sdk_install_id is not None:
            return self.sdk_install_id
        return self.identity.install_id if self.identity.registered else "0"

    @property
    def local_device_id(self) -> str:
        return self.hardware.local_device_id if self.hardware.local_device_id is not None else self.hardware.pc_uuid

    @property
    def guaranteed_device_id(self) -> str:
        return self.device_id if self.device_id != "0" else self.local_device_id


class AhaDeviceService:
    """Resolve an independent device identity without implicit HTTP access.

    ``allow_network=True`` permits registration on a cache miss. Activation is
    separate: ``require_activation=True`` is used by the existing login flow.
    A cached registration can be returned before activation, as in native Aha.
    ``get_device_id_async`` models the native pending getter. The synchronous
    method is a compatibility snapshot/registration adapter for existing login.
    ``start_native_mode`` selects the native lifecycle, including its retry
    policy and cache adapter; login preparation retains its stricter policy.
    """

    def __init__(self, store: IdentityStore, *, http: httpx.Client | None = None,
                 version: str = "3.4.20", hardware_reader: Callable[[], Hardware] | None = None):
        self.store = store
        self.http = http
        self.version = version
        self.hardware_reader = hardware_reader
        self._hardware: Hardware | None = None
        self._state_lock = threading.RLock()
        self._operation_lock = threading.RLock()
        self._latest: DeviceIdResult | None = None
        self._ready: Future[DeviceIdResult] = Future()
        self._native = None
        self._native_identity: LoginIdentity | None = None

    def _native_result(self, device_id: str, *, main_id: str | None = None) -> DeviceIdResult:
        with self._state_lock:
            identity = deepcopy(self._native_identity)
        # Reading an SDK ID does not bind our OAuth installation key to it.
        if (identity.device_id != device_id or identity.install_id != self._native.install_id
                or DeviceRecord.load(identity.device_cache, self._hardware.pc_uuid) is None):
            identity.registered, identity.activated = False, False
        return DeviceIdResult(identity, self._hardware, "native", main_id,
                              device_id, self._native.install_id)

    def _read_hardware(self, *, native: bool = False) -> Hardware:
        with self._state_lock:
            if self._hardware is None:
                self._hardware = (self.hardware_reader or (Hardware.read_native if native else Hardware.read))()
            return self._hardware

    def _publish(self, result: DeviceIdResult) -> None:
        with self._state_lock:
            # Keep a timed-out main ID until the real remote ID arrives.
            if not (self._latest is not None and result.remote_device_id == "0"
                    and self._latest.source == "timeout"):
                self._latest = deepcopy(result)
            if result.remote_device_id != "0" and not self._ready.done():
                self._ready.set_result(deepcopy(result))

    async def get_device_id_async(self) -> DeviceIdResult:
        """Return cached remote ID or wait; cancellation does not cancel registration."""
        if self._native is not None:
            return self._native_result(await self._native.get_device_id())
        with self._state_lock:
            latest = self._latest
        if latest is None:
            latest = await asyncio.to_thread(self.get_device_id)
        if latest.remote_device_id != "0":
            return deepcopy(latest)
        return deepcopy(await asyncio.shield(asyncio.wrap_future(self._ready)))

    async def resolve_device_id_async(self, *, timeout: float = 40.0) -> DeviceIdResult:
        """JS-wrapper semantics: local main ID on timeout; accept late remote ID."""
        if self._native is not None:
            ids = await self._native.resolve(timeout=timeout)
            return self._native_result(ids["remoteDeviceId"], main_id=ids["deviceId"])
        # Reviewed iCubeDeviceRegister.js: O$=4e4 milliseconds.
        if timeout < 0:
            raise ValueError("timeout must be nonnegative")
        # Initialize even for timeout=0, before wait_for can cancel the getter.
        with self._state_lock:
            initialized = self._latest is not None
        if not initialized:
            await asyncio.to_thread(self.get_device_id)
        with self._state_lock:
            if self._latest.remote_device_id != "0":
                return deepcopy(self._latest)
        try:
            return await asyncio.wait_for(self.get_device_id_async(), timeout)
        except asyncio.TimeoutError:
            with self._state_lock:
                # Registration may have resolved between timeout and this lock.
                if self._latest.remote_device_id != "0":
                    return deepcopy(self._latest)
                self._latest = replace(self._latest, source="timeout",
                                       main_device_id=self._latest.local_device_id)
                return deepcopy(self._latest)

    async def register_device_async(self, *, require_activation: bool = False) -> DeviceIdResult:
        """Explicit registration entry point; never scheduled by an offline getter."""
        if self._native is not None:
            raise ValueError("native mode active; use its start/wait_worker lifecycle")
        return await asyncio.to_thread(self.get_device_id, allow_network=True,
                                       require_activation=require_activation)

    def get_all_ids(self) -> dict[str, str]:
        if self._native is not None:
            return self._native.get_all_ids()
        with self._state_lock:
            latest = self._latest
        if latest is None:
            latest = self.get_device_id()
        return {"deviceId": latest.device_id, "localDeviceId": latest.local_device_id,
                "remoteDeviceId": latest.remote_device_id}

    async def start_native_mode(self, *, allow_network: bool = False, backend=None,
                                app_region: str = "CN", region_record_ready: bool | None = None, storage=None,
                                native_cache: bool = False,
                                flags_2: bool = False, flags_3: bool = False,
                                sleep=asyncio.sleep):
        """Create the raw native lifecycle, outside stricter login policy.

        Caller owns its lifetime and must await close(). Network startup is
        explicit even when an HTTP client has already been configured.
        """
        from .aha_native_lifecycle import NativeAhaDeviceService
        from .aha_native_backend import NativeHttpBackend
        from .aha_tiny_storage import RegionRecord, NativeTinyStorage
        if native_cache:
            if storage is not None:
                raise ValueError("choose native_cache or an explicit storage adapter")
            storage = NativeTinyStorage.for_native_client()
        if self._native is not None:
            if backend is not None and self._native.backend is None:
                self._native.backend = backend
            await self._native.start(allow_network=allow_network)
            return self._native
        def native_snapshot():
            with self._operation_lock:
                return self._resolve_identity(allow_network=False, require_activation=False, validate_hardware=False)
        snapshot = await asyncio.to_thread(native_snapshot)
        region = RegionRecord.load(snapshot.identity.device_region)
        record = DeviceRecord(snapshot.remote_device_id, snapshot.install_id, snapshot.hardware.pc_uuid)
        if not snapshot.identity.registered:
            record = None
        if storage is not None:
            record = await asyncio.to_thread(storage.load_device, snapshot.hardware.pc_uuid)
            region = await asyncio.to_thread(storage.load_region)
        if region_record_ready is None:
            region_record_ready = region.ready
        if backend is None and self.http is not None:
            backend = NativeHttpBackend(self.http, snapshot.hardware, self.version)
        if allow_network and backend is None:
            raise ValueError("native lifecycle requires an explicitly supplied HTTP client or backend")
        async def save_cache(value: DeviceRecord):
            if storage is not None:
                await asyncio.to_thread(storage.save_device, value)
            def persist():
                with self.store.locked():
                    identity = self.store.load()
                    if identity.device_id != value.device_id:
                        identity.private_key_pem = new_key()
                    identity.device_id, identity.install_id = value.device_id, value.install_id
                    identity.hardware_uuid = value.uuid
                    identity.device_cache = value.encoded()
                    identity.registered, identity.activated = True, False
                    self.store.save(identity)
                    with self._state_lock:
                        self._native_identity = deepcopy(identity)
            await asyncio.to_thread(persist)
        async def update_region(country: str, source: str):
            nonlocal region
            incoming = RegionRecord(True, country, source)
            region = region.updated(incoming)
            # Native merges into its live optional value, but serializes the
            # incoming RegionInfo argument (RVA 0x0284416f), not the merge.
            if storage is not None:
                await asyncio.to_thread(storage.save_region, incoming)
            else:
                def persist_region():
                    with self.store.locked():
                        identity = self.store.load()
                        identity.device_region = incoming.encoded()
                        self.store.save(identity)
                await asyncio.to_thread(persist_region)
        native = NativeAhaDeviceService(hardware_uuid=snapshot.hardware.pc_uuid, record=record,
            local_device_id=snapshot.local_device_id,
            app_region=app_region, region_record_ready=bool(region_record_ready),
            flags_2=flags_2, flags_3=flags_3, backend=backend, save_cache=save_cache,
            update_region=update_region, sleep=sleep)
        self._native_identity = deepcopy(snapshot.identity)
        self._native = native
        native.start_wrapper()
        await asyncio.sleep(0)
        await native.start(allow_network=allow_network)
        return native

    def get_device_id(self, *, allow_network: bool = False,
                      require_activation: bool = False) -> DeviceIdResult:
        if self._native is not None:
            if allow_network or require_activation:
                raise ValueError("native mode active; use its lifecycle, separate from login preparation")
            ids = self._native.get_all_ids()
            return self._native_result(ids["remoteDeviceId"], main_id=ids["deviceId"])
        if not allow_network and not require_activation:
            with self._state_lock:
                if self._latest is not None and self._latest.remote_device_id != "0":
                    return deepcopy(self._latest)
        # One native service instance owns one initialization/registration path.
        # Serialize same-instance callers before the cross-process profile lock.
        with self._operation_lock:
            return self._resolve_identity(allow_network=allow_network, require_activation=require_activation)

    def _resolve_identity(self, *, allow_network: bool, require_activation: bool,
                          validate_hardware: bool = True) -> DeviceIdResult:
        hardware = self._read_hardware(native=not validate_hardware)
        if validate_hardware and (not isinstance(hardware.pc_uuid, str) or not hardware.pc_uuid or hardware.pc_uuid == "0"):
            raise ValueError("hardware UUID unavailable; refusing to invent a registered device")
        with self.store.locked():
            identity = self.store.load()
            previous_device_id = identity.device_id
            # Migrate our legacy record once; do not read native TinyStorage.
            if identity.device_cache is None:
                identity.device_cache = (DeviceRecord(identity.device_id, identity.install_id,
                    identity.hardware_uuid).encoded() if identity.registered else {})
                self.store.save(identity)
            record = DeviceRecord.load(identity.device_cache, hardware.pc_uuid)
            if record is None:
                # Invalidate only the device record view, preserving the stored
                # installation key and old record until a registration succeeds.
                identity = replace(identity, registered=False, activated=False)
            else:
                activated = (identity.activated and identity.device_id == record.device_id
                             and identity.install_id == record.install_id
                             and identity.hardware_uuid == hardware.pc_uuid)
                identity = replace(identity, device_id=record.device_id, install_id=record.install_id,
                                   hardware_uuid=hardware.pc_uuid, registered=True, activated=activated)
            # Persist a new/migrated installation even if registration fails.
            if not self.store.path.exists():
                self.store.save(identity)
            source = "cache" if identity.registered else "local"
            # Publish initialized IDs before HTTP: the JS timeout must remain
            # independent of a slow registration/activation operation.
            self._publish(DeviceIdResult(replace(identity), hardware, source))
            needs_registration = not identity.registered
            needs_activation = require_activation and not identity.activated
            if (needs_registration or needs_activation) and allow_network and self.http is None:
                raise ValueError("network operation requires an explicitly supplied HTTP client")
            if allow_network and needs_registration:
                registration = register(self.http, hardware, self.version, strict_ids=require_activation)
                if previous_device_id != registration.device_id:
                    identity.private_key_pem = new_key()
                identity.device_id, identity.install_id = registration.device_id, registration.install_id
                identity.hardware_uuid, identity.region = hardware.pc_uuid, registration.region
                identity.registered, identity.activated = True, False
                identity.device_cache = DeviceRecord(identity.device_id, identity.install_id, hardware.pc_uuid).encoded()
                # Native registration also saves before activation. Failure of
                # activation must not cause another installation registration.
                self.store.save(identity)
                source = "registration"
                self._publish(DeviceIdResult(replace(identity), hardware, source))
            if require_activation and identity.registered:
                if any(not value.isascii() or not value.isdecimal() or int(value) <= 0
                       for value in (identity.device_id, identity.install_id)):
                    raise ValueError("login activation requires positive decimal device/install IDs")
            if require_activation and not identity.activated:
                if not allow_network:
                    raise ValueError("device activation not confirmed; network access is disabled")
                activate(self.http, Registration(identity.device_id, identity.install_id, identity.region),
                         hardware, self.version)
                identity.activated = True
                self.store.save(identity)
            result = DeviceIdResult(identity, hardware, source)
            self._publish(result)
            return result
