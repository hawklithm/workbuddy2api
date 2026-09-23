"""Windows asyncio loop selection for the CodeBuddy HTTP proxy."""

import asyncio
import socket
import subprocess
import sys
import time
from unittest.mock import Mock

import httpx
import pytest
import uvicorn

import codebuddy_proxy.__main__ as proxy


def _capture_run(monkeypatch):
    run = Mock()
    monkeypatch.setattr(proxy.uvicorn, "run", run)
    return run


def test_windows_uses_selector_with_new_uvicorn(monkeypatch):
    monkeypatch.setattr(proxy.sys, "platform", "win32")
    run = _capture_run(monkeypatch)

    proxy.run_http_server(host="0.0.0.0", port=8787)

    run.assert_called_once_with(
        proxy.app,
        host="0.0.0.0",
        port=8787,
        log_level="warning",
        loop=asyncio.SelectorEventLoop,
    )


def test_windows_uses_policy_with_old_uvicorn(monkeypatch):
    monkeypatch.setattr(proxy.sys, "platform", "win32")
    monkeypatch.setattr(proxy.uvicorn, "Config", type("OldConfig", (), {}))
    selector_policy = Mock(return_value=object())
    monkeypatch.setattr(asyncio, "WindowsSelectorEventLoopPolicy", selector_policy, raising=False)
    set_policy = Mock()
    monkeypatch.setattr(asyncio, "set_event_loop_policy", set_policy)
    run = _capture_run(monkeypatch)

    proxy.run_http_server(host="127.0.0.1", port=8787)

    selector_policy.assert_called_once_with()
    set_policy.assert_called_once_with(selector_policy.return_value)
    run.assert_called_once_with(
        proxy.app, host="127.0.0.1", port=8787, log_level="warning"
    )


def test_windows_proactor_opt_out_preserves_default(monkeypatch):
    monkeypatch.setattr(proxy.sys, "platform", "win32")
    set_policy = Mock()
    monkeypatch.setattr(asyncio, "set_event_loop_policy", set_policy)
    run = _capture_run(monkeypatch)

    proxy.run_http_server(host="127.0.0.1", port=8787, windows_loop="proactor")

    set_policy.assert_not_called()
    run.assert_called_once_with(
        proxy.app, host="127.0.0.1", port=8787, log_level="warning"
    )


def test_other_platforms_use_uvicorn_default(monkeypatch):
    monkeypatch.setattr(proxy.sys, "platform", "linux")
    run = _capture_run(monkeypatch)

    proxy.run_http_server(host="127.0.0.1", port=8787)

    run.assert_called_once_with(
        proxy.app, host="127.0.0.1", port=8787, log_level="warning"
    )


def test_windows_loop_cli_option():
    assert proxy.parse_args([]).windows_loop == "selector"
    assert proxy.parse_args(["--windows-loop", "proactor"]).windows_loop == "proactor"


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows asyncio")
def test_installed_uvicorn_creates_windows_selector_loop():
    if not hasattr(uvicorn.Config, "get_loop_factory"):
        pytest.skip("older Uvicorn uses Windows event-loop policy")
    config = uvicorn.Config(proxy.app, loop=asyncio.SelectorEventLoop)
    loop = config.get_loop_factory()()
    try:
        assert isinstance(loop, asyncio.SelectorEventLoop)
    finally:
        loop.close()


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows socket server")
def test_windows_proxy_starts_and_accepts_http_requests(tmp_path):
    """Exercise the real Windows Uvicorn startup, without upstream login."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "codebuddy_proxy",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--mock-dir",
            str(tmp_path),
            "--session-file",
            str(tmp_path / "session.json"),
            "--log-file",
            str(tmp_path / "proxy.jsonl"),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        url = f"http://127.0.0.1:{port}/health"
        deadline = time.monotonic() + 10
        response = None
        with httpx.Client(trust_env=False, timeout=0.5) as client:
            while time.monotonic() < deadline and process.poll() is None:
                try:
                    response = client.get(url)
                    break
                except httpx.TransportError:
                    time.sleep(0.1)
            if response is None:
                stdout, stderr = process.communicate(timeout=3) if process.poll() is not None else ("", "")
                pytest.fail(f"Windows proxy did not start: {stdout}\n{stderr}")
            assert response.status_code == 200
            assert response.json()["status"] == "ok"
            # Separate connections exercise the accept path beyond startup.
            for _ in range(5):
                assert httpx.get(url, trust_env=False, timeout=1).status_code == 200
    finally:
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=5)
