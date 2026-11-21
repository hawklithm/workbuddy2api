"""Read non-secret metadata from the local client and operating system."""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import re
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class InstalledClient:
    app_version: str = ""
    editor_version: str = "1.107.1"
    name: str = "Trae CN"
    ug_url: str = "https://api.trae.cn"
    telemetry_enabled: bool = True
    build_version: str = "local"


def installed_client() -> InstalledClient:
    paths = []
    if os.environ.get("TRAE2API_PRODUCT_JSON"):
        paths.append(Path(os.environ["TRAE2API_PRODUCT_JSON"]))
    if os.environ.get("LOCALAPPDATA"):
        paths.append(Path(os.environ["LOCALAPPDATA"]) / "Programs/Trae CN/resources/app/product.json")
    # Python is often installed in the same per-user Programs directory.
    paths.append(Path(sys.base_prefix).parent.parent / "Trae CN/resources/app/product.json")
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            value = data.get("appVersion") if isinstance(data, dict) else None
            if isinstance(value, str) and re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][\w.-]+)?", value):
                try:
                    package = json.loads(path.with_name("package.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    package = {}
                editor = package.get("version", "") if isinstance(package, dict) else ""
                if not isinstance(editor, str) or not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][\w.-]+)?", editor):
                    editor = "1.107.1"  # Reviewed 3.4.20 installation fallback.
                ug = data.get("bootConfig", {}).get("ug", {}).get("trae", {}).get("normal")
                name = data.get("nameShort")
                return InstalledClient(value, editor, name if isinstance(name, str) and name else "Trae CN",
                                       ug if isinstance(ug, str) and ug else "https://api.trae.cn",
                                       data.get("enableTelemetry") is not False,
                                       data.get("tronBuildVersion") or "local")
        except (OSError, ValueError):
            continue
    return InstalledClient()


def installed_version() -> str:
    return installed_client().app_version


def client_version(fallback: str = "3.4.4") -> str:
    return installed_version() or fallback


def device_model() -> str:
    """Read the actual system product name; never use the account/device name."""
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\BIOS") as key:
                value, _ = winreg.QueryValueEx(key, "SystemProductName")
            return clean_model(value)
        except OSError:
            return ""
    return ""


def clean_model(value: str) -> str:
    # Same control-character filtering as the native HF helper.
    return re.sub(r"[\x00-\x08\x0a-\x1f\x7f]", "", value) if isinstance(value, str) else ""


def os_metadata() -> tuple[str, str]:
    name = platform.system()
    if name == "Windows":
        # Node/libuv os.version() uses Windows' ProductName, including edition.
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as key:
                version, _ = winreg.QueryValueEx(key, "ProductName")
            if isinstance(version, str) and version:
                return "windows", version
        except OSError:
            pass
        return "windows", f"Windows {platform.win32_ver()[0] or platform.release()}"
    if name == "Darwin":
        return "mac", f"macOS {platform.mac_ver()[0]}"
    if name == "Linux":
        try:
            values = platform.freedesktop_os_release()
            return "linux", values.get("PRETTY_NAME") or "Linux"
        except OSError:
            return "linux", "Linux"
    return name.lower(), ""
