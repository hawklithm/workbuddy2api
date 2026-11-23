"""Application-owned device storage and account/device usage records."""
from contextlib import contextmanager
import json
import os
import sys
from pathlib import Path
import time


_reported_fallbacks: set[tuple[str, str]] = set()
_fallback_project_root: Path | None = None


def configure_fallback(project_root: Path | None) -> None:
    """Set the legacy application fallback without importing that application."""
    global _fallback_project_root
    _fallback_project_root = Path(project_root) if project_root is not None else None


def _python_profile_home(home: Path) -> Path | None:
    """Identify a matching Windows profile only from a per-user Python install."""
    if os.name != 'nt':
        return None
    python_path = Path(sys._base_executable)
    for candidate in python_path.parents:
        if candidate.name.casefold() != home.name.casefold() or candidate.parent.name.casefold() != 'users':
            continue
        relative = python_path.relative_to(candidate)
        parts = [part.casefold() for part in relative.parts]
        if parts[:4] == ['appdata', 'local', 'programs', 'python']:
            return candidate
    return None


def root() -> Path:
    override = os.environ.get('TRAE2API_HOME')
    if override:
        path = Path(override).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        return path
    home = Path.home()
    path = home / '.traeapi'
    try:
        path.mkdir(parents=True, exist_ok=True)
        return path
    except PermissionError:
        alternate = _python_profile_home(home)
        candidates = []
        if alternate is not None and alternate != home:
            candidates.append(alternate / '.traeapi')
        candidates.append((_fallback_project_root or Path.cwd()) / '.traeapi')
        for fallback in candidates:
            if fallback == path:
                continue
            try:
                fallback.mkdir(parents=True, exist_ok=True)
            except PermissionError:
                continue
            key = (str(path), str(fallback))
            if key not in _reported_fallbacks:
                _reported_fallbacks.add(key)
                print(f'[trae2api] {path} is not writable; using {fallback}. '
                      'Set TRAE2API_HOME to select the storage directory.', file=sys.stderr)
            return fallback
        raise PermissionError(f"cannot initialize storage at {path} or fallback locations; "
                              "set TRAE2API_HOME to a writable directory") from None


@contextmanager
def _locked():
    # OS-managed locks are released on process termination.
    path = root() / '.storage.lock'
    with path.open('a+b') as lock:
        if os.name == 'nt':
            import msvcrt
            lock.seek(0)
            if not lock.read(1):
                lock.write(b'0')
                lock.flush()
            while True:
                try:
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == 'nt':
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)


def device_path(device_id: str) -> Path:
    # Remote IDs are normally decimal; allow other safe IDs used by clients.
    if (not isinstance(device_id, str) or not device_id or device_id in ('.', '..')
            or any(c in device_id for c in '<>:"/\\|?*')
            or any(ord(c) < 32 for c in device_id) or device_id[-1] in ' .'):
        raise ValueError('device ID cannot be used as a directory name')
    return root() / device_id / 'TinyStorage'


def save_tiny_storage(identity) -> None:
    from .aha_device_cache import DeviceRecord
    from .login_identity import atomic_json
    record = DeviceRecord.load(identity.device_cache, identity.hardware_uuid)
    if not identity.registered or record is None:
        return
    path = device_path(record.device_id)
    with _locked():
        if path.exists():
            data = json.loads(path.read_text(encoding='utf-8-sig'))
            if not isinstance(data, dict) or not isinstance(data.get('tiny_storage_data', {}), dict):
                raise ValueError('invalid application TinyStorage')
        else:
            data = {}
        values = data.setdefault('tiny_storage_data', {})
        values['aha.device.device_id'] = record.encoded()
        if isinstance(identity.device_region, str):
            values['aha_device_region'] = identity.device_region
        atomic_json(path, data)


def _read_pairs(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(data, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in data.items()):
        raise ValueError('invalid device_pair.json; refusing to overwrite it')
    return data


def resolve_pair(user_id: str, device_id: str, client, version: str):
    """Reuse the account binding, registering a device only on first use."""
    from .login_identity import atomic_json
    if not isinstance(user_id, str) or not user_id:
        raise ValueError('check-in credentials must contain auth.userId')
    from .device_hardware import Hardware
    hardware = Hardware.for_account(user_id)
    path = root() / 'device_pair.json'
    # Hold the lock through registration to avoid two processes registering for
    # the same previously unseen account at the same time.
    with _locked():
        data = _read_pairs(path)
        if user_id not in data:
            from .aha_registration import Registration, register, activate
            from .aha_device_cache import DeviceRecord
            pending_path = root() / 'device_pending.json'
            if pending_path.exists():
                pending = json.loads(pending_path.read_text(encoding='utf-8-sig'))
                if not isinstance(pending, dict):
                    raise ValueError('invalid device_pending.json; refusing to overwrite it')
            else:
                pending = {}
            if user_id in pending:
                entry = pending[user_id]
                if (not isinstance(entry, dict) or set(entry) != {'device_id', 'install_id', 'region', 'hardware_uuid'}
                        or any(not isinstance(value, str) or not value for value in entry.values())
                        or any(not entry[key].isascii() or not entry[key].isdecimal() or int(entry[key]) <= 0
                               for key in ('device_id', 'install_id'))
                        or entry['region'] not in ('CN', 'SG', 'US', 'USTTP')
                        or entry['hardware_uuid'] != hardware.pc_uuid):
                    raise ValueError('invalid pending device registration for this account')
                registration = Registration(entry['device_id'], entry['install_id'], entry['region'])
            else:
                registration = register(client, hardware, version or None)
                pending[user_id] = {'device_id': registration.device_id, 'install_id': registration.install_id,
                                    'region': registration.region, 'hardware_uuid': hardware.pc_uuid}
                atomic_json(pending_path, pending)
            activate(client, registration, hardware, version or '3.4.20', require_confirmation=False)
            device_id = registration.device_id
            cache_path = device_path(device_id)
            if cache_path.exists():
                cache = json.loads(cache_path.read_text(encoding='utf-8-sig'))
                if not isinstance(cache, dict) or not isinstance(cache.get('tiny_storage_data', {}), dict):
                    raise ValueError('invalid application TinyStorage')
            else:
                cache = {}
            cache.setdefault('tiny_storage_data', {})['aha.device.device_id'] = (
                DeviceRecord(device_id, registration.install_id, hardware.pc_uuid).encoded())
            atomic_json(cache_path, cache)
            data[user_id] = device_id
            atomic_json(path, data)
            del pending[user_id]
            atomic_json(pending_path, pending)
        else:
            device_id = data[user_id]
            device_path(device_id)
    return device_id, hardware
