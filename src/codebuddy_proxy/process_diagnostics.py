"""Best-effort process-level diagnostics, independent of proxy request handling.

Console stdout/stderr is mirrored verbatim to a bounded file. Lifecycle events
are recorded in separate bounded JSONL. Neither should break the proxy when
logging fails. Forced termination (TerminateProcess/SIGKILL) cannot execute any
Python exit handler; an unpaired startup is only a diagnostic clue.
"""
from __future__ import annotations

import asyncio
import ctypes
import faulthandler
import json
import logging
import logging.handlers
import os
import signal
from pathlib import Path
import sys
import threading
import time
import traceback
import uuid
from typing import Any, Callable, TextIO


def process_memory() -> dict[str, int]:
    """Get best-effort process memory sizes in bytes, without dependencies."""
    try:
        if sys.platform == "win32":
            from ctypes import wintypes

            class MemoryCounters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = MemoryCounters()
            counters.cb = ctypes.sizeof(MemoryCounters)
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            if ctypes.windll.psapi.GetProcessMemoryInfo(
                handle, ctypes.byref(counters), counters.cb
            ):
                return {
                    "rss_bytes": int(counters.WorkingSetSize),
                    "peak_rss_bytes": int(counters.PeakWorkingSetSize),
                    "private_commit_bytes": int(counters.PagefileUsage),
                }
        else:
            import resource
            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            # Linux returns KiB; macOS returns bytes.
            return {"peak_rss_bytes": int(peak * (1 if sys.platform == "darwin" else 1024))}
    except (Exception, OSError):
        pass
    return {}


def _secure_touch(path: Path) -> None:
    """Apply POSIX owner-only permissions when creating a sensitive log."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.close(fd)


class EventJournal:
    def __init__(self, path: Path, fallback: TextIO):
        self.fallback = fallback
        self.lock = threading.RLock()
        self.handler: logging.handlers.RotatingFileHandler | None = None
        self.failed = False
        try:
            _secure_touch(path)
            self.handler = logging.handlers.RotatingFileHandler(
                path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
            )
            self.handler.setFormatter(logging.Formatter("%(message)s"))
        except OSError as exc:
            self._warn(exc)

    def _warn(self, exc: BaseException) -> None:
        if not self.failed:
            self.failed = True
            try:
                self.fallback.write(
                    "[process diagnostics] lifecycle log unavailable: "
                    + type(exc).__name__ + "\n"
                )
                self.fallback.flush()
            except Exception:
                pass

    def emit(self, event: str, **fields: Any) -> None:
        if self.handler is None:
            return
        record = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "event": event,
            **fields,
        }
        with self.lock:
            try:
                # Direct stream writes allow us to detect file write failures,
                # which standard logging.Handler.emit may suppress.
                line = json.dumps(record, ensure_ascii=False, default=str)
                with self.handler.lock:
                    entry = logging.LogRecord(
                        "workbuddy.process", logging.INFO, "", 0, line, (), None
                    )
                    if self.handler.shouldRollover(entry):
                        self.handler.doRollover()
                    self.handler.stream.write(line + "\n")
                    self.handler.flush()
            except Exception as exc:
                self._warn(exc)

    def close(self) -> None:
        if self.handler is not None:
            try:
                self.handler.close()
            except (OSError, ValueError) as exc:
                self._warn(exc)


class ConsoleSink:
    """Byte-for-byte text mirroring with bounded file rotation."""

    def __init__(self, path: Path, error_reporter: Callable[[str, BaseException], None]):
        self.lock = threading.RLock()
        self.report = error_reporter
        self.handler: logging.handlers.RotatingFileHandler | None = None
        self.failed = False
        try:
            _secure_touch(path)
            self.handler = logging.handlers.RotatingFileHandler(
                path, maxBytes=20 * 1024 * 1024, backupCount=4, encoding="utf-8"
            )
        except OSError as exc:
            self.report("console_open", exc)

    def write(self, text: str) -> None:
        if not text or self.handler is None or self.failed:
            return
        with self.lock:
            try:
                with self.handler.lock:
                    stream = self.handler.stream
                    if stream is None:
                        return
                    if stream.tell() + len(text.encode("utf-8", "replace")) > self.handler.maxBytes:
                        self.handler.doRollover()
                        stream = self.handler.stream
                    stream.write(text)
                    stream.flush()
            except Exception as exc:
                self.failed = True
                self.report("console_write", exc)

    def flush(self) -> None:
        if self.handler is not None and not self.failed:
            try:
                self.handler.flush()
            except Exception as exc:
                self.failed = True
                self.report("console_flush", exc)

    def close(self) -> None:
        if self.handler is not None:
            try:
                self.handler.close()
            except (OSError, ValueError) as exc:
                self.report("console_close", exc)


class TeeStream:
    def __init__(self, original: TextIO, sink: ConsoleSink):
        self.original = original
        self.sink = sink

    def write(self, data: str) -> int:
        # Preserve original console semantics, even if mirroring fails.
        count = self.original.write(data)
        self.sink.write(data)
        return count

    def flush(self) -> None:
        try:
            self.original.flush()
        finally:
            self.sink.flush()

    def isatty(self) -> bool:
        return self.original.isatty()

    def fileno(self) -> int:
        return self.original.fileno()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.original, name)


class ProcessDiagnostics:
    def __init__(self, directory: Path):
        self.directory = directory
        self.run_id = uuid.uuid4().hex
        self.started_at = time.time()
        self.original_out: TextIO = sys.stdout
        self.original_err: TextIO = sys.stderr
        self.journal: EventJournal | None = None
        self.sink: ConsoleSink | None = None
        self.crash_file: Any = None
        self.previous_thread_hook: Any = None
        self.previous_unraisable_hook: Any = None
        self.exit_recorded = False
        self.last_signal: int | None = None
        self.signal_handlers: dict[int, tuple[Any, Any]] = {}
        self.signal_observers: dict[int, Any] = {}
        self.previous_sigterm_handler: Any = None

    def event(self, name: str, **fields: Any) -> None:
        if self.journal is not None:
            self.journal.emit(name, pid=os.getpid(), run_id=self.run_id, **fields)

    def log_failure(self, where: str, exc: BaseException) -> None:
        # Never include actual message/body/token in the diagnostic event.
        self.event(
            "diagnostic_log_failure", target=where,
            exception_type=type(exc).__name__, **process_memory(),
        )
        try:
            self.original_err.write(
                f"[process diagnostics] {where} failed: {type(exc).__name__}\n"
            )
            self.original_err.flush()
        except Exception:
            pass

    def _thread_exception(self, args: Any) -> None:
        self.event(
            "thread_unhandled_exception",
            thread_name=getattr(args.thread, "name", "unknown"),
            exception_type=getattr(args.exc_type, "__name__", "unknown"),
            **process_memory(),
        )
        if self.previous_thread_hook is not None:
            self.previous_thread_hook(args)

    def _unraisable_exception(self, args: Any) -> None:
        self.event(
            "unraisable_exception",
            exception_type=getattr(args.exc_type, "__name__", "unknown"),
            **process_memory(),
        )
        if self.previous_unraisable_hook is not None:
            self.previous_unraisable_hook(args)

    def _install_sigterm_exit_handler(self) -> None:
        """Preserve an observable SIGTERM exit after Uvicorn replays signals."""
        if threading.current_thread() is not threading.main_thread():
            return
        self.previous_sigterm_handler = signal.getsignal(signal.SIGTERM)

        def handle_sigterm(sig: int, frame: Any) -> None:
            self.last_signal = sig
            self.event("process_signal_received", signal=signal.Signals(sig).name,
                       **process_memory())
            raise SystemExit(128 + sig)

        try:
            signal.signal(signal.SIGTERM, handle_sigterm)
            self.signal_handlers[signal.SIGTERM] = (self.previous_sigterm_handler, handle_sigterm)
        except (ValueError, OSError):
            pass

    def install_signal_observers(self) -> None:
        """Wrap existing Uvicorn handlers; delegate signals without changing shutdown."""
        if threading.current_thread() is not threading.main_thread():
            return
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous = signal.getsignal(signum)
            if signum in self.signal_handlers and previous is self.signal_handlers[signum][1]:
                continue
            if not callable(previous):
                continue

            def observer(sig: int, frame: Any, original: Any = previous) -> None:
                self.last_signal = sig
                self.event("process_signal_received", signal=signal.Signals(sig).name,
                           **process_memory())
                original(sig, frame)

            try:
                signal.signal(signum, observer)
                self.signal_observers[signum] = observer
                if signum not in self.signal_handlers:
                    self.signal_handlers[signum] = (previous, observer)
            except (OSError, ValueError):
                pass

    def install_asyncio(self, loop: asyncio.AbstractEventLoop) -> None:
        old_handler = loop.get_exception_handler()

        def handler(current_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
            exc = context.get("exception")
            self.event(
                "asyncio_unhandled_exception",
                exception_type=type(exc).__name__ if exc is not None else None,
                **process_memory(),
            )
            if old_handler is not None:
                old_handler(current_loop, context)
            else:
                current_loop.default_exception_handler(context)

        loop.set_exception_handler(handler)

    def __enter__(self) -> ProcessDiagnostics:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.log_failure("create_log_directory", exc)
            return self
        self.journal = EventJournal(self.directory / "process-events.jsonl", self.original_err)
        self.sink = ConsoleSink(self.directory / "console.log", self.log_failure)
        sys.stdout = TeeStream(self.original_out, self.sink)
        sys.stderr = TeeStream(self.original_err, self.sink)
        self.previous_thread_hook = threading.excepthook
        self.previous_unraisable_hook = sys.unraisablehook
        threading.excepthook = self._thread_exception
        sys.unraisablehook = self._unraisable_exception
        try:
            _secure_touch(self.directory / "crash.log")
            self.crash_file = (self.directory / "crash.log").open("a", encoding="utf-8")
            faulthandler.enable(file=self.crash_file, all_threads=True)
        except (OSError, RuntimeError, ValueError) as exc:
            self.log_failure("faulthandler_enable", exc)
        self._install_sigterm_exit_handler()
        self._report_incomplete_prior_run()
        self.event(
            "process_start", ppid=os.getppid(), python=sys.version.split()[0],
            **process_memory(),
        )
        return self

    def _report_incomplete_prior_run(self) -> None:
        """An unmatched previous start suggests an unobserved exit, not proven crash."""
        path = self.directory / "process-events.jsonl"
        try:
            with path.open("rb") as source:
                source.seek(0, os.SEEK_END)
                size = source.tell()
                source.seek(max(0, size - 256 * 1024))
                events = source.read().decode("utf-8", "replace").splitlines()
            last_start = None
            for line in events:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if item.get("event") == "process_start":
                    last_start = item
                elif (last_start is not None and item.get("event") == "process_exit"
                      and item.get("run_id") == last_start.get("run_id")):
                    last_start = None
            if last_start is not None:
                self.event("previous_run_exit_unobserved",
                           previous_pid=last_start.get("pid"),
                           previous_run_id=last_start.get("run_id"),
                           note="May indicate a force-kill, process crash, or concurrent run")
        except (OSError, ValueError):
            pass

    def finish(self, code: int, reason: str) -> None:
        if self.exit_recorded:
            return
        self.exit_recorded = True
        self.event(
            "process_exit", exit_code=code, reason=reason,
            uptime_seconds=round(time.time() - self.started_at, 3),
            signal=signal.Signals(self.last_signal).name if self.last_signal else None,
            **process_memory(),
        )

    def run(self, fn: Callable[[], int | None]) -> int:
        with self:
            try:
                result = fn()
                code = result if isinstance(result, int) else 0
                self.finish(code, "signal_shutdown" if self.last_signal else
                            ("normal" if code == 0 else "nonzero_return"))
                return code
            except KeyboardInterrupt:
                self.event("process_interrupt", signal="SIGINT", **process_memory())
                self.finish(130, "keyboard_interrupt")
                return 130
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
                self.event("process_system_exit", exit_code=code, **process_memory())
                self.finish(code, "signal_termination" if self.last_signal else "system_exit")
                if exc.code is not None and not isinstance(exc.code, int):
                    print(str(exc.code), file=sys.stderr)
                return code
            except BaseException as exc:
                self.event(
                    "process_fatal_exception",
                    exception_type=type(exc).__name__, **process_memory(),
                )
                traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)
                self.finish(1, "uncaught_exception")
                return 1

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if not self.exit_recorded:
            self.finish(1 if exc_type is not None else 0, "context_exit")
        if self.previous_thread_hook is not None:
            threading.excepthook = self.previous_thread_hook
        if self.previous_unraisable_hook is not None:
            sys.unraisablehook = self.previous_unraisable_hook
        if threading.current_thread() is threading.main_thread():
            for signum, (previous, observer) in self.signal_handlers.items():
                current = signal.getsignal(signum)
                if current is observer or current is self.signal_observers.get(signum):
                    signal.signal(signum, previous)
        self.signal_handlers.clear()
        self.signal_observers.clear()
        sys.stdout = self.original_out
        sys.stderr = self.original_err
        if self.crash_file is not None:
            try:
                faulthandler.disable()
                self.crash_file.close()
            except Exception:
                pass
        if self.sink is not None:
            self.sink.close()
        if self.journal is not None:
            self.journal.close()
