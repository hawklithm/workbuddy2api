"""Independent Windows process evidence collector. No proxy imports or stop/restart API.

Run with a separate console/hidden Start-Process; see docs/external-process-monitor.md.
"""
from __future__ import annotations

import argparse
import ctypes as C
from ctypes import wintypes as W
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import threading
import time
import urllib.request
import uuid


def utc():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def filetime(value):
    ticks = (value.dwHighDateTime << 32) | value.dwLowDateTime
    return ticks


def ticks_utc(ticks):
    return datetime.fromtimestamp((ticks - 116444736000000000) / 10_000_000,
                                  timezone.utc).isoformat(timespec="microseconds")


class Journal:
    def __init__(self, path, max_bytes=8 * 1024 * 1024, backups=4):
        self.path, self.max_bytes, self.backups = Path(path), max_bytes, backups
        self.seq = 0
        self.stream = self.path.open("a", encoding="utf-8")

    def emit(self, event, **fields):
        self.seq += 1
        line = json.dumps(dict(timestamp=utc(), monotonic_ns=time.monotonic_ns(),
                               seq=self.seq, event=event, **fields), ensure_ascii=True) + "\n"
        if self.stream.tell() + len(line.encode()) > self.max_bytes:
            self.stream.close()
            for index in range(self.backups, 0, -1):
                source = self.path if index == 1 else Path(f"{self.path}.{index - 1}")
                if source.exists():
                    source.replace(Path(f"{self.path}.{index}"))
            self.stream = self.path.open("a", encoding="utf-8")
        self.stream.write(line)
        self.stream.flush()
        if event != "sample":
            os.fsync(self.stream.fileno())

    def close(self):
        self.stream.close()


class PROCESSENTRY32W(C.Structure):
    _fields_ = [("dwSize", W.DWORD), ("cntUsage", W.DWORD), ("th32ProcessID", W.DWORD),
                ("th32DefaultHeapID", C.c_size_t), ("th32ModuleID", W.DWORD),
                ("cntThreads", W.DWORD), ("th32ParentProcessID", W.DWORD),
                ("pcPriClassBase", W.LONG), ("dwFlags", W.DWORD), ("szExeFile", W.WCHAR * 260)]


class MEMORY(C.Structure):
    _fields_ = [("cb", W.DWORD), ("PageFaultCount", W.DWORD)] + [
        (name, C.c_size_t) for name in ("PeakWorkingSetSize", "WorkingSetSize",
        "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
        "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage", "PrivateUsage")]


class Windows:
    def __init__(self):
        self.k = C.WinDLL("kernel32", use_last_error=True)
        self.p = C.WinDLL("psapi", use_last_error=True)
        for dll, name, restype, args in [
            (self.k, "OpenProcess", W.HANDLE, [W.DWORD, W.BOOL, W.DWORD]),
            (self.k, "CloseHandle", W.BOOL, [W.HANDLE]),
            (self.k, "CreateToolhelp32Snapshot", W.HANDLE, [W.DWORD, W.DWORD]),
            (self.k, "Process32FirstW", W.BOOL, [W.HANDLE, C.POINTER(PROCESSENTRY32W)]),
            (self.k, "Process32NextW", W.BOOL, [W.HANDLE, C.POINTER(PROCESSENTRY32W)]),
            (self.k, "WaitForSingleObject", W.DWORD, [W.HANDLE, W.DWORD]),
            (self.k, "GetExitCodeProcess", W.BOOL, [W.HANDLE, C.POINTER(W.DWORD)]),
            (self.k, "GetProcessTimes", W.BOOL, [W.HANDLE] + [C.POINTER(W.FILETIME)] * 4),
            (self.k, "QueryFullProcessImageNameW", W.BOOL,
             [W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)]),
            (self.p, "GetProcessMemoryInfo", W.BOOL, [W.HANDLE, C.POINTER(MEMORY), W.DWORD]),
        ]:
            fn = getattr(dll, name)
            fn.restype, fn.argtypes = restype, args

    def check(self, ok):
        if not ok:
            raise C.WinError(C.get_last_error())

    def snapshot(self):
        handle = self.k.CreateToolhelp32Snapshot(2, 0)
        if handle == C.c_void_p(-1).value:
            raise C.WinError(C.get_last_error())
        result = {}
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = C.sizeof(entry)
            ok = self.k.Process32FirstW(handle, C.byref(entry))
            while ok:
                result[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile)
                ok = self.k.Process32NextW(handle, C.byref(entry))
            if C.get_last_error() != 18:  # ERROR_NO_MORE_FILES
                raise C.WinError(C.get_last_error())
            return result
        finally:
            self.k.CloseHandle(handle)

    def open(self, pid):
        # Query + synchronize only: deliberately no PROCESS_TERMINATE permission.
        handle = self.k.OpenProcess(0x1000 | 0x100000, False, pid)
        self.check(handle)
        return handle

    def times(self, handle):
        values = [W.FILETIME() for _ in range(4)]
        self.check(self.k.GetProcessTimes(handle, *(C.byref(v) for v in values)))
        return filetime(values[0]), filetime(values[1])

    def exited(self, handle):
        status = self.k.WaitForSingleObject(handle, 0)
        if status == 0xFFFFFFFF:
            raise C.WinError(C.get_last_error())
        return status == 0

    def exit_code(self, handle):
        value = W.DWORD()
        self.check(self.k.GetExitCodeProcess(handle, C.byref(value)))
        return value.value

    def image(self, handle):
        buf, size = C.create_unicode_buffer(32768), W.DWORD(32768)
        self.check(self.k.QueryFullProcessImageNameW(handle, 0, buf, C.byref(size)))
        return buf.value

    def memory(self, handle):
        value = MEMORY()
        value.cb = C.sizeof(value)
        self.check(self.p.GetProcessMemoryInfo(handle, C.byref(value), C.sizeof(value)))
        return dict(working_set_bytes=value.WorkingSetSize, private_bytes=value.PrivateUsage)


class Tracker:
    def __init__(self, api, journal):
        self.api, self.journal = api, journal
        self.active = {}
        self.identities = set()
        self.pids = set()

    def add(self, pid, ppid, name, parent=None):
        if pid in self.active:
            return
        handle = self.api.open(pid)
        try:
            created, _ = self.api.times(handle)
            identity = (pid, created)
            if identity in self.identities:
                return
            if parent and created < parent["created_ticks"]:
                return  # Stale PPID, not a child of this incarnation.
            if parent and parent.get("handle"):
                _, parent_ended = self.api.times(parent["handle"])
                if parent_ended and created > parent_ended:
                    return
            fields = dict(pid=pid, ppid=ppid, name=name, created_ticks=created,
                          created_at=ticks_utc(created))
            try:
                fields["image"] = self.api.image(handle)
            except OSError as exc:
                fields["image_error"] = str(exc)
            observed_at = utc()
            self.active[pid] = dict(handle=handle,
                                   last_alive=None if self.api.exited(handle) else observed_at, **fields)
            self.identities.add(identity)
            self.pids.add(pid)
            self.journal.emit("process_observed", **fields)
            handle = None
        finally:
            if handle is not None:
                self.api.k.CloseHandle(handle)

    def discover(self, snapshot):
        pending = dict(snapshot)
        changed = True
        while changed:
            changed = False
            for pid, (ppid, name) in list(pending.items()):
                if pid not in self.active and ppid in self.active:
                    try:
                        self.add(pid, ppid, name, self.active[ppid])
                    except OSError as exc:
                        self.journal.emit("process_open_failed", pid=pid, ppid=ppid, error=str(exc))
                    del pending[pid]
                    changed = pid in self.active or changed

    def sample(self, record=True):
        rows = []
        for pid, process in list(self.active.items()):
            h = process["handle"]
            observed_at = utc()
            if self.api.exited(h):
                code = self.api.exit_code(h)
                _, ended = self.api.times(h)
                self.journal.emit("process_exit", pid=pid, ppid=process["ppid"],
                    created_ticks=process["created_ticks"], exit_code=code,
                    exit_code_hex=f"0x{code:08X}", exit_ticks=ended,
                    exited_at=ticks_utc(ended) if ended else None,
                    last_observed_alive=process["last_alive"],
                    cause="unknown", source="retained_process_handle")
                self.api.k.CloseHandle(h)
                del self.active[pid]
            else:
                process["last_alive"] = observed_at
                if not record:
                    continue
                row = dict(pid=pid, created_ticks=process["created_ticks"])
                try:
                    row.update(self.api.memory(h))
                except OSError as exc:
                    row["memory_error"] = str(exc)
                rows.append(row)
        if record:
            self.journal.emit("sample", processes=rows)

    def close(self):
        for process in self.active.values():
            self.api.k.CloseHandle(process["handle"])


def health(url):
    started = time.monotonic()
    try:
        # Do not send local probes through environment proxy servers; never read response bodies.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                            NoRedirect())
        with opener.open(url, timeout=1) as response:
            return dict(ok=response.status == 200, status=response.status,
                        duration_ms=round((time.monotonic() - started) * 1000))
    except Exception as exc:
        return dict(ok=False, error=type(exc).__name__,
                    duration_ms=round((time.monotonic() - started) * 1000))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class BusinessTail:
    """Bounded, allowlisted correlation hints; deliberately not an in-flight request census."""
    def __init__(self, path):
        self.path, self.previous = Path(path), None

    def read(self):
        stat = self.path.stat()
        identity = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
        if identity == self.previous:
            return None
        with self.path.open("rb") as stream:
            offset = max(0, stat.st_size - 65536)
            stream.seek(offset)
            data = stream.read(65536)
        lines = data.splitlines()
        if offset:
            lines = lines[1:]  # First line may start in the middle of a JSON record.
        rows = []
        for line in lines[-32:]:
            try:
                obj = json.loads(line)
                rows.append({key: obj[key] for key in (
                    "timestamp", "event", "request_id", "status", "run_id", "pid") if key in obj})
            except (ValueError, TypeError):
                continue
        self.previous = identity
        return dict(size=stat.st_size, modified_ns=stat.st_mtime_ns, tail=rows,
                    complete_history=False)


def collect_events(start, end, pids):
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-File",
        str(Path(__file__).with_name("collect_windows_events.ps1")),
        "-Since", start, "-Until", end, "-ProcessIds", ",".join(map(str, sorted(pids)))],
        capture_output=True, encoding="utf-8", errors="replace", timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError(f"event collector failed ({result.returncode})")
    return json.loads(result.stdout.lstrip("\ufeff"))


def find_proxies():
    # Command lines stay inside the helper; never store credentials or request content.
    command = """
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
    $ErrorActionPreference = 'Stop'
    $rows = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='uv.exe' OR Name='workbuddy2api-evidence.exe' OR Name='workbuddy2api-runtime.exe'" |
      Where-Object { $_.CommandLine -match '(?:^|\\s)-m\\s+codebuddy_proxy(?:\\s|$)' -and
        $_.CommandLine -notmatch 'watch_processes\\.py' } |
      ForEach-Object { @{pid=$_.ProcessId; ppid=$_.ParentProcessId; name=$_.Name;
        created_ticks=$_.CreationDate.ToUniversalTime().ToFileTimeUtc()} })
    ConvertTo-Json -InputObject $rows -Compress
    """
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, encoding="utf-8", errors="replace", timeout=10,
        creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError(f"proxy discovery failed ({result.returncode})")
    return json.loads(result.stdout.lstrip("\ufeff"))


class Background:
    """Bounded single worker: slow health/event APIs must not delay handle sampling."""
    def __init__(self):
        self.results = queue.Queue()
        self.thread = None

    def submit(self, event, fn, *args):
        if self.thread and self.thread.is_alive():
            return False
        def work():
            try:
                value = fn(*args)
                self.results.put((event, dict(result=value)))
            except Exception as exc:
                self.results.put((event + "_failed", dict(error=str(exc))))
        self.thread = threading.Thread(target=work, daemon=True)
        self.thread.start()
        return True

    def drain(self, journal):
        while True:
            try:
                event, fields = self.results.get_nowait()
            except queue.Empty:
                break
            journal.emit(event, **fields)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--pid", type=int, help="Attach to this live root PID and discover descendants")
    mode.add_argument("--watch-proxy", action="store_true", help="Wait for existing/future proxy module processes")
    mode.add_argument("--launch", nargs=argparse.REMAINDER, help="Explicitly launch a NEW process")
    parser.add_argument("--output", type=Path, default=Path.home() / ".workbuddy2api" / "monitor")
    parser.add_argument("--interval", type=float, default=0.25)
    parser.add_argument("--duration", type=float, help="Stop monitoring only; targets remain running")
    parser.add_argument("--health-url", default="http://127.0.0.1:8787/health")
    parser.add_argument("--no-health", action="store_true")
    parser.add_argument("--no-system-events", action="store_true")
    parser.add_argument("--business-log", type=Path,
                        default=Path.home() / ".workbuddy2api" / "codebuddy-proxy.jsonl")
    parser.add_argument("--no-business-log", action="store_true")
    args = parser.parse_args(argv)
    if os.name != "nt":
        parser.error("Windows only")
    if args.interval < 0.05 or (args.duration is not None and args.duration <= 0):
        parser.error("interval must be >= 0.05 and duration > 0")
    if args.launch == []:
        parser.error("--launch requires an executable and arguments")
    if args.pid is not None and args.pid <= 0:
        parser.error("PID must be positive")
    if not args.no_health and args.health_url not in (
            "http://127.0.0.1:8787/health", "http://localhost:8787/health"):
        parser.error("health probes are restricted to localhost:8787/health")
    run_dir = args.output / (datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
    run_dir.mkdir(parents=True, exist_ok=False)
    journal = Journal(run_dir / "events.jsonl")
    api, tracker = Windows(), None
    stopped = threading.Event()
    def stop(signum, frame):
        journal.emit("monitor_signal", signal=signum)
        stopped.set()
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    tracker = Tracker(api, journal)
    events, probes, discovery = Background(), Background(), Background()
    business = BusinessTail(args.business_log)
    child = None
    reason = "duration"
    status = 0
    started = time.monotonic()
    event_since = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    next_event = next_health = next_discovery = next_sample = 0
    journal.emit("monitor_start", monitor_pid=os.getpid(),
                 mode="watch" if args.watch_proxy else "launch" if args.launch else "attach",
                 interval=args.interval, health_enabled=not args.no_health,
                 system_events_enabled=not args.no_system_events,
                 limitations=["discovery can miss short-lived descendants", "exit code does not identify killer",
                              "system events depend on existing audit settings and access"])
    print(str(run_dir), flush=True)
    try:
        if args.launch:
            # Files capture native and descendant OS writes too. No pipes to deadlock.
            with (run_dir / "target-stdout.log").open("ab", buffering=0) as out, \
                 (run_dir / "target-stderr.log").open("ab", buffering=0) as err:
                child = subprocess.Popen(args.launch, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                         creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)
            pid = child.pid
            journal.emit("target_launched", pid=pid, executable=args.launch[0])
        else:
            pid = args.pid
        if not args.watch_proxy:
            snapshot = api.snapshot()
            ppid, name = snapshot.get(pid, (os.getpid() if child else None, "unknown"))
            tracker.add(pid, ppid, name)
        while not stopped.is_set():
            now = time.monotonic()
            if args.watch_proxy:
                if now >= next_discovery and discovery.submit("proxy_discovery", find_proxies):
                    next_discovery = now + 5
                while not discovery.results.empty():
                    event, fields = discovery.results.get_nowait()
                    if event.endswith("_failed"):
                        journal.emit(event, **fields)
                    else:
                        for row in fields["result"]:
                            if row["pid"] in tracker.active:
                                continue
                            # Verify CIM's creation time against the opened handle to reject PID reuse.
                            try:
                                handle = api.open(row["pid"])
                                try:
                                    created, _ = api.times(handle)
                                    if abs(created - row["created_ticks"]) > 10:
                                        raise OSError("PID identity changed since discovery")
                                    tracker.add(row["pid"], row["ppid"], row["name"])
                                finally:
                                    api.k.CloseHandle(handle)
                            except OSError as exc:
                                journal.emit("process_open_failed", pid=row["pid"], error=str(exc))
            # Discover before removing dead parents so surviving descendants are included.
            tracker.discover(api.snapshot())
            tracker.sample(record=now >= next_sample)
            if now >= next_sample:
                next_sample = now + 2
                if not args.no_business_log:
                    try:
                        tail = business.read()
                        if tail is not None:
                            journal.emit("business_log_tail", **tail)
                    except OSError as exc:
                        journal.emit("business_log_unavailable", error=str(exc))
            events.drain(journal)
            probes.drain(journal)
            if not args.no_system_events and now >= next_event:
                until = utc()
                if events.submit("windows_events", collect_events, event_since, until, set(tracker.pids)):
                    journal.emit("windows_event_query", since=event_since, until=until,
                                 pids=sorted(tracker.pids))
                    # Overlap for delayed event-log delivery. Consumers dedupe channel + record_id.
                    event_since = (datetime.fromisoformat(until) - timedelta(seconds=60)).isoformat()
                    next_event = now + 30
            if not args.no_health and now >= next_health:
                if probes.submit("health", health, args.health_url):
                    next_health = now + 2
            if not tracker.active and not args.watch_proxy:
                reason = "all_tracked_processes_exited"
                break
            if args.duration and now - started >= args.duration:
                break
            stopped.wait(args.interval)
        if stopped.is_set():
            reason = "signal"
    except Exception as exc:
        journal.emit("monitor_error", error=repr(exc))
        reason, status = "error", 1
    finally:
        # A final event query includes the termination window; never stop the target.
        if events.thread:
            events.thread.join(timeout=17)
        events.drain(journal)
        if not args.no_system_events:
            try:
                journal.emit("windows_events", result=collect_events(event_since, utc(), tracker.pids))
            except Exception as exc:
                journal.emit("windows_events_failed", error=str(exc))
        if probes.thread:
            probes.thread.join(timeout=2)
        probes.drain(journal)
        if not args.no_business_log:
            try:
                tail = business.read()
                if tail is not None:
                    journal.emit("business_log_tail", **tail)
            except OSError as exc:
                journal.emit("business_log_unavailable", error=str(exc))
        if child is not None:
            journal.emit("launched_root_status", pid=child.pid, exit_code=child.poll())
        journal.emit("monitor_stop", reason=reason, still_tracked=list(tracker.active),
                     targets_stopped=False)
        tracker.close()
        journal.close()
    return status


if __name__ == "__main__":
    raise SystemExit(main())
