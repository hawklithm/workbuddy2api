"""Read and validate Trae credential files without device-registration side effects."""
from __future__ import annotations

import base64
import json
from pathlib import Path
import time


def reject_expired_token(token: str) -> None:
    """Use JWT expiry for local diagnostics, not authentication."""
    parts = token.split('.')
    if len(parts) != 3:
        return
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + '=' * (-len(parts[1]) % 4)))
        expiry = payload.get('exp') if isinstance(payload, dict) else None
    except (ValueError, UnicodeError):
        return
    if type(expiry) in (int, float) and expiry <= time.time():
        raise ValueError('login token expired; sign in to Trae again')


def load_file(path: str | Path) -> tuple[dict, dict, str, str]:
    """Return auth/device metadata and validated token/device ID."""
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise ValueError('credentials must be a JSON object')
    auth, device = data.get('auth', data), data.get('device', {})
    if not isinstance(auth, dict) or not isinstance(device, dict):
        raise ValueError('credentials auth and device must be JSON objects')
    token = auth.get('token') or auth.get('jwt')
    if 'deviceId' not in device:
        device = auth
    device_id = device.get('deviceId')
    if not isinstance(token, str) or not token or not isinstance(device_id, str) or not device_id or device_id == '0':
        raise ValueError('credentials must contain auth.token (or auth.jwt) and device.deviceId (or legacy auth.deviceId)')
    reject_expired_token(token)
    return auth, device, token, device_id
