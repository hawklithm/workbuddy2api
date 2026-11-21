"""Trae CN credential sources and optional native-device verification."""
from pathlib import Path
from .native_identity import DeviceIdSource, NativeIdentity, NativeIdentityError


class TraeNativeProvider:
    def __init__(self, *, directory: Path | None = None,
                 device_id_source: DeviceIdSource = 'get-device-id'):
        self.directory = directory
        self.device_id_source = device_id_source

    def load(self) -> NativeIdentity:
        if self.directory is None:
            return NativeIdentity.load(device_id_source=self.device_id_source)
        return NativeIdentity.load(self.directory, device_id_source=self.device_id_source)


def __getattr__(name):
    # Optional Trae dependencies are loaded only when token lifecycle is used.
    if name in ('TraeAuth', 'TraeCredentials'):
        from . import auth
        return getattr(auth, name)
    raise AttributeError(name)


__all__ = ['DeviceIdSource', 'NativeIdentity', 'NativeIdentityError', 'TraeNativeProvider',
           'TraeAuth', 'TraeCredentials']
