"""Process diagnostics and version contract tests (no running proxy required)."""
from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from codebuddy_proxy.process_diagnostics import (
    ConsoleSink,
    ProcessDiagnostics,
    TeeStream,
    process_memory,
)


def _events(directory: Path):
    file = directory / "process-events.jsonl"
    return [json.loads(s) for s in file.read_text(encoding="utf-8").splitlines()]


def test_successful_run_captures_console_and_lifecycle(tmp_path, capsys):
    monitor = ProcessDiagnostics(tmp_path)

    def command():
        print("stdout greeting")
        print("stderr greeting", file=sys.stderr)
        return 0

    assert monitor.run(command) == 0
    stdout, stderr = capsys.readouterr()
    assert "stdout greeting" in stdout
    assert "stderr greeting" in stderr
    assert sys.stdout is monitor.original_out
    assert sys.stderr is monitor.original_err
    recorded = (tmp_path / "console.log").read_text(encoding="utf-8")
    assert "stdout greeting\n" in recorded
    assert "stderr greeting\n" in recorded
    assert [r["event"] for r in _events(tmp_path)] == ["process_start", "process_exit"]
    exit_record = _events(tmp_path)[-1]
    assert exit_record["exit_code"] == 0
    assert exit_record["reason"] == "normal"
    assert exit_record["run_id"] == _events(tmp_path)[0]["run_id"]
    assert "uptime_seconds" in exit_record


def test_unhandled_exception_traceback_and_nonzero_exit(tmp_path, capsys):
    def crash():
        raise RuntimeError("intentional failure for test")

    assert ProcessDiagnostics(tmp_path).run(crash) == 1
    stderr = capsys.readouterr().err
    assert "Traceback (most recent call last)" in stderr
    captured = (tmp_path / "console.log").read_text(encoding="utf-8")
    assert "RuntimeError: intentional failure for test" in captured
    names = [r["event"] for r in _events(tmp_path)]
    assert names == ["process_start", "process_fatal_exception", "process_exit"]
    fatal = _events(tmp_path)[1]
    assert fatal["exception_type"] == "RuntimeError"
    assert _events(tmp_path)[-1]["reason"] == "uncaught_exception"
    assert _events(tmp_path)[-1]["exit_code"] == 1


@pytest.mark.parametrize("fn,code,reason", [
    (lambda: 7, 7, "nonzero_return"),
    (lambda: (_ for _ in ()).throw(KeyboardInterrupt()), 130, "keyboard_interrupt"),
    (lambda: (_ for _ in ()).throw(SystemExit(4)), 4, "system_exit"),
    (lambda: (_ for _ in ()).throw(SystemExit(None)), 0, "system_exit"),
])
def test_distinct_exit_reasons(tmp_path, fn, code, reason):
    assert ProcessDiagnostics(tmp_path).run(fn) == code
    assert _events(tmp_path)[-1]["exit_code"] == code
    assert _events(tmp_path)[-1]["reason"] == reason


def test_next_start_records_unobserved_prior_exit_without_assuming_crash(tmp_path):
    def stop_without_exit():
        journal = tmp_path / "process-events.jsonl"
        journal.write_text(
            json.dumps({"event": "process_start", "pid": 1234, "run_id": "old-id"})
            + "\n", encoding="utf-8",
        )

    stop_without_exit()
    assert ProcessDiagnostics(tmp_path).run(lambda: 0) == 0
    events = _events(tmp_path)
    assert [r["event"] for r in events] == [
        "process_start", "previous_run_exit_unobserved", "process_start", "process_exit",
    ]
    assert events[1]["previous_pid"] == 1234
    assert events[1]["previous_run_id"] == "old-id"


def test_completed_previous_run_does_not_raise_false_alarm(tmp_path):
    assert ProcessDiagnostics(tmp_path).run(lambda: 0) == 0
    assert ProcessDiagnostics(tmp_path).run(lambda: 0) == 0
    assert not any(x["event"] == "previous_run_exit_unobserved" for x in _events(tmp_path))


def test_asyncio_exceptions_are_recorded_and_delegated(tmp_path):
    loop = asyncio.new_event_loop()
    forwarded = []
    loop.set_exception_handler(lambda loop, context: forwarded.append(context))
    try:
        with ProcessDiagnostics(tmp_path) as monitor:
            monitor.install_asyncio(loop)
            context = {"message": "Background task failed", "exception": ValueError("bad")}
            loop.call_exception_handler(context)
        assert forwarded == [context]
        events = _events(tmp_path)
        assert events[1]["event"] == "asyncio_unhandled_exception"
        assert events[1]["exception_type"] == "ValueError"
    finally:
        loop.close()


def test_thread_and_unraisable_exceptions_are_recorded_and_forwarded(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(threading, "excepthook", lambda args: calls.append("thread"))
    monkeypatch.setattr(sys, "unraisablehook", lambda args: calls.append("unraisable"))
    with ProcessDiagnostics(tmp_path) as monitor:
        thread_args = SimpleNamespace(
            exc_type=ValueError, thread=SimpleNamespace(name="worker"),
        )
        monitor._thread_exception(thread_args)
        unraisable_args = SimpleNamespace(exc_type=RuntimeError)
        monitor._unraisable_exception(unraisable_args)
    assert calls == ["thread", "unraisable"]
    assert {x["event"] for x in _events(tmp_path)} >= {
        "thread_unhandled_exception", "unraisable_exception",
    }


def test_console_rotation_is_bounded_and_mirrors_original(tmp_path):
    text = []

    class Original:
        def write(self, value):
            text.append(value)
            return len(value)

        def flush(self):
            pass

        def isatty(self):
            return False

    sink = ConsoleSink(tmp_path / "console.log", lambda *args: None)
    assert sink.handler is not None
    sink.handler.maxBytes = 16
    sink.handler.backupCount = 2
    tee = TeeStream(Original(), sink)
    for i in range(8):
        tee.write(f"line-{i}\n")
    sink.close()
    assert "".join(text) == "".join(f"line-{i}\n" for i in range(8))
    assert (tmp_path / "console.log").exists()
    assert len(list(tmp_path.glob("console.log*"))) <= 3


def test_console_file_error_does_not_suppress_original(capsys, tmp_path):
    errors = []
    sink = ConsoleSink(tmp_path / "console.log", lambda name, exc: errors.append(name))
    assert sink.handler is not None
    sink.handler.stream.close()
    tee = TeeStream(sys.stdout, sink)
    assert tee.write("still visible\n") == len("still visible\n")
    assert capsys.readouterr().out == "still visible\n"
    assert errors == ["console_write"]
    sink.close()


def test_log_failure_records_exception_type_not_sensitive_text(tmp_path):
    with ProcessDiagnostics(tmp_path) as monitor:
        monitor.log_failure("jsonl_write", OSError("SECRET TEST LOG CONTENT"))
    output = _events(tmp_path)
    failure = next(x for x in output if x["event"] == "diagnostic_log_failure")
    assert failure["target"] == "jsonl_write"
    assert failure["exception_type"] == "OSError"
    assert "SECRET TEST LOG CONTENT" not in (tmp_path / "process-events.jsonl").read_text()


def test_sigterm_handler_records_signal_and_exit_code(tmp_path):
    monitor = ProcessDiagnostics(tmp_path)

    def simulate_sigterm():
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)

    assert monitor.run(simulate_sigterm) == 128 + signal.SIGTERM
    events = _events(tmp_path)
    assert any(x['event'] == 'process_signal_received' for x in events)
    assert events[-1]['reason'] == 'signal_termination'
    assert events[-1]['exit_code'] == 128 + signal.SIGTERM


def test_force_kill_is_detectable_on_next_start(tmp_path):
    script = '''
from pathlib import Path
from codebuddy_proxy.process_diagnostics import ProcessDiagnostics
import sys, time
ProcessDiagnostics(Path(sys.argv[1])).run(lambda: time.sleep(40))
'''
    proc = subprocess.Popen([sys.executable, '-c', script, str(tmp_path)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(150):
            if (tmp_path / 'process-events.jsonl').exists():
                if any(x['event'] == 'process_start' for x in _events(tmp_path)):
                    break
            time.sleep(0.03)
        else:
            pytest.fail('child did not start within deadline')
        proc.kill()
        proc.wait(timeout=8)
        assert ProcessDiagnostics(tmp_path).run(lambda: 0) == 0
        assert any(x['event'] == 'previous_run_exit_unobserved' for x in _events(tmp_path))
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=8)


def test_main_wiring_captures_runtime_and_keeps_cli_result(tmp_path, monkeypatch):
    import codebuddy_proxy.__main__ as proxy

    opts = SimpleNamespace(log_file=tmp_path / 'codebuddy-proxy.jsonl')
    monkeypatch.setattr(proxy, 'parse_args', lambda: opts)

    def fake_runtime(args):
        assert args is opts
        print('uvicorn runtime output', file=sys.stderr)
        return 0

    monkeypatch.setattr(proxy, '_run_proxy', fake_runtime)
    assert proxy.main() == 0
    assert 'uvicorn runtime output' in (tmp_path / 'console.log').read_text(encoding='utf-8')
    assert _events(tmp_path)[-1]['exit_code'] == 0
    assert proxy.process_diagnostics is None


def test_file_handler_error_hook_reports_to_process_events(tmp_path, monkeypatch):
    import codebuddy_proxy.__main__ as proxy
    import logging

    handler = proxy.ObservedTimedRotatingFileHandler(tmp_path / 'proxy.log', when='midnight')
    try:
        with ProcessDiagnostics(tmp_path) as monitor:
            monkeypatch.setattr(proxy, 'process_diagnostics', monitor)
            try:
                raise OSError('failed file write test')
            except OSError:
                handler.handleError(logging.LogRecord('test', 40, '', 1, 'test', (), None))
            monkeypatch.setattr(proxy, 'process_diagnostics', None)
    finally:
        handler.close()
    assert any(e['event'] == 'diagnostic_log_failure' and e['target'] == 'file_logger'
               for e in _events(tmp_path))


def test_proxy_jsonl_logging_failure_is_not_silenced(tmp_path, monkeypatch):
    import codebuddy_proxy.__main__ as proxy

    class FailingLogger:
        def info(self, text):
            raise OSError('secret payload must not appear in structured diagnostics')

    state = proxy.ProxyState.__new__(proxy.ProxyState)
    state.runtime_info = {'app_version': '2.0.7'}
    state.json_logger = FailingLogger()
    with ProcessDiagnostics(tmp_path) as monitor:
        monkeypatch.setattr(proxy, 'process_diagnostics', monitor)
        state.write_log('client_request_summary', status=200)
        state.write_body_log('upstream_response', b'secret request body')
        monkeypatch.setattr(proxy, 'process_diagnostics', None)
    failures = [e for e in _events(tmp_path) if e['event'] == 'diagnostic_log_failure']
    assert [e['target'] for e in failures] == ['jsonl_write', 'jsonl_body_write']
    assert all(e['exception_type'] == 'OSError' for e in failures)
    assert 'secret' not in (tmp_path / 'process-events.jsonl').read_text(encoding='utf-8')


def test_process_memory_is_safe_and_nonnegative():
    result = process_memory()
    assert isinstance(result, dict)
    assert all(isinstance(value, int) and value >= 0 for value in result.values())


def test_subprocess_captures_real_traceback_without_parent_hook_interference(tmp_path):
    script = """
from pathlib import Path
from codebuddy_proxy.process_diagnostics import ProcessDiagnostics
def boom():
    print("child stdout", flush=True)
    raise RuntimeError("child error")
raise SystemExit(ProcessDiagnostics(Path(__import__('sys').argv[1])).run(boom))
"""
    proc = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)], capture_output=True,
        text=True, timeout=15,
    )
    assert proc.returncode == 1
    assert "child stdout" in proc.stdout
    assert "RuntimeError: child error" in proc.stderr
    log = (tmp_path / "console.log").read_text(encoding="utf-8")
    assert "child stdout" in log
    assert "RuntimeError: child error" in log


def test_api_version_is_207():
    import codebuddy_proxy.__main__ as proxy
    assert proxy.app.version == '2.0.7'


def test_project_version_is_207_in_pyproject():
    pyproject = Path(__file__).resolve().parent / "pyproject.toml"
    content = pyproject.read_text(encoding="utf-8")
    assert 'version = "2.0.7"' in content
