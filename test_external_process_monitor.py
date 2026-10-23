"""Real Windows tests only terminate processes created by these tests."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

SCRIPT = Path(__file__).parent / "scripts" / "watch_processes.py"
spec = importlib.util.spec_from_file_location("watch_processes", SCRIPT)
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)


def records(path):
    result = []
    for file in path.glob("*/events.jsonl"):
        for line in file.read_text(encoding="utf-8").splitlines():
            try:
                result.append(json.loads(line))
            except json.JSONDecodeError:
                pass  # Reader may race an append; the next read retries.
    return result


def wait_for(path, predicate):
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        found = [row for row in records(path) if predicate(row)]
        if found:
            return found[0]
        time.sleep(0.05)
    pytest.fail(f"Missing monitor event; events={records(path)}")


def test_journal_rotation(tmp_path):
    journal = watch.Journal(tmp_path / "events.jsonl", max_bytes=350, backups=2)
    for number in range(8):
        journal.emit("test", number=number)
    journal.close()
    assert len(list(tmp_path.iterdir())) == 3
    last = json.loads((tmp_path / "events.jsonl").read_text().splitlines()[-1])
    assert last["seq"] == 8
    assert last["number"] == 7


def test_background_does_not_block_or_overlap():
    import threading
    gate = threading.Event()
    worker = watch.Background()
    try:
        assert worker.submit("test", gate.wait, 2)
        assert not worker.submit("other", lambda: 1)
    finally:
        gate.set()
        worker.thread.join(3)
    assert worker.results.get()[0] == "test"


def test_business_tail_redacts_payload_and_handles_replacement(tmp_path):
    path = tmp_path / "business.jsonl"
    path.write_text(json.dumps(dict(event="stream_started", request_id="r1", headers="secret",
                                    body="private")) + "\n", encoding="utf-8")
    tail = watch.BusinessTail(path)
    assert tail.read()["tail"] == [dict(event="stream_started", request_id="r1")]
    assert tail.read() is None
    path.unlink()
    path.write_text('{"event":"stream_completed","status":200}\n', encoding="utf-8")
    assert tail.read()["tail"] == [dict(event="stream_completed", status=200)]


windows = pytest.mark.skipif(os.name != "nt", reason="Windows process handles required")


@pytest.fixture
def owned():
    processes = []
    def create(command):
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        processes.append(process)
        return process
    yield create
    for process in reversed(processes):
        if process.poll() is None:
            process.terminate()  # Only exact Popen objects created by this fixture.
        process.wait(timeout=15)


def monitor(owned, path, *mode, duration="4", events=False):
    args = [sys.executable, str(SCRIPT), "--output", str(path), "--interval", "0.05",
            "--duration", duration, "--no-health", "--no-business-log"]
    if not events:
        args += ["--no-system-events"]
    return owned(args + list(mode))


@windows
@pytest.mark.parametrize("code,expected", [
    ("raise SystemExit(0)", 0), ("raise RuntimeError('test failure')", 1),
    ("import os; os._exit(37)", 37), ("import os; os._exit(259)", 259),
    ("import ctypes; ctypes.windll.kernel32.ExitProcess(0xC0000005)", 0xC0000005),
])
def test_launch_exit_codes(tmp_path, owned, code, expected):
    proc = monitor(owned, tmp_path, "--launch", sys.executable, "-c",
                   "import time; time.sleep(0.2); " + code)
    assert proc.wait(timeout=20) == 0
    row = wait_for(tmp_path, lambda r: r["event"] == "process_exit")
    assert row["exit_code"] == expected
    assert row["exit_code_hex"] == f"0x{expected:08X}"
    assert row["exited_at"] >= row["last_observed_alive"]
    assert row["cause"] == "unknown"


@windows
def test_immediate_launch_exit_retains_root_handle(tmp_path, owned):
    proc = monitor(owned, tmp_path, "--launch", sys.executable, "-c", "raise SystemExit(23)")
    assert proc.wait(timeout=20) == 0
    assert wait_for(tmp_path, lambda r: r["event"] == "process_exit")["exit_code"] == 23


@windows
def test_attach_forced_termination_and_memory(tmp_path, owned):
    target = owned([sys.executable, "-c", "import time; time.sleep(30)"])
    observer = monitor(owned, tmp_path, "--pid", str(target.pid))
    wait_for(tmp_path, lambda r: r["event"] == "process_observed" and r["pid"] == target.pid)
    sample = wait_for(tmp_path, lambda r: r["event"] == "sample" and r["processes"])
    assert sample["processes"][0]["working_set_bytes"] > 0
    target.terminate()
    assert observer.wait(timeout=20) == 0
    row = wait_for(tmp_path, lambda r: r["event"] == "process_exit")
    assert row["exit_code"] == target.wait(timeout=3)


@windows
def test_monitor_duration_leaves_target_running(tmp_path, owned):
    target = owned([sys.executable, "-c", "import time; time.sleep(30)"])
    observer = monitor(owned, tmp_path, "--pid", str(target.pid), duration="0.3")
    assert observer.wait(timeout=20) == 0
    assert target.poll() is None
    assert records(tmp_path)[-1]["targets_stopped"] is False


@windows
def test_killing_attach_monitor_leaves_target_running(tmp_path, owned):
    target = owned([sys.executable, "-c", "import time; time.sleep(30)"])
    observer = monitor(owned, tmp_path, "--pid", str(target.pid))
    wait_for(tmp_path, lambda r: r["event"] == "process_observed")
    observer.terminate()
    observer.wait(timeout=5)
    assert target.poll() is None
    assert not any(r["event"] == "monitor_stop" for r in records(tmp_path))


@windows
@pytest.mark.parametrize("kill_root", [True, False])
def test_uv_python_exit_order(tmp_path, owned, kill_root):
    uv = shutil.which("uv")
    if not uv:
        pytest.skip("uv unavailable")
    observer = monitor(owned, tmp_path, "--launch", uv, "run", "--no-project",
        "--python", sys.executable, "python", "-c", "import time; time.sleep(2)", duration="5")
    root = wait_for(tmp_path, lambda r: r["event"] == "target_launched")["pid"]
    child = wait_for(tmp_path, lambda r: r["event"] == "process_observed" and r["pid"] != root)
    # Open an exact test process handle with termination rights. No name-based taskkill.
    import ctypes
    api = watch.Windows()
    handle = api.k.OpenProcess(0x100001, False, root if kill_root else child["pid"])
    assert handle
    try:
        api.k.TerminateProcess.argtypes = [watch.W.HANDLE, watch.W.UINT]
        api.check(api.k.TerminateProcess(handle, 71))
    finally:
        api.k.CloseHandle(handle)
    assert observer.wait(timeout=20) == 0
    exits = [r for r in records(tmp_path) if r["event"] == "process_exit"]
    by_pid = {r["pid"]: r for r in exits}
    assert {root, child["pid"]} <= by_pid.keys()
    assert by_pid[root if kill_root else child["pid"]]["exit_code"] == 71
    if kill_root:
        assert by_pid[root]["exited_at"] < by_pid[child["pid"]]["exited_at"]
    else:
        assert by_pid[child["pid"]]["exited_at"] <= by_pid[root]["exited_at"]


@windows
def test_event_collection_reports_channel_access():
    now = watch.utc()
    result = watch.collect_events(now, watch.utc(), {os.getpid()})
    assert result["audit_settings_changed"] is False
    assert {r["channel"] for r in result["channels"]} == {
        "System", "Application", "Security", "Microsoft-Windows-Sysmon/Operational"}
    assert all(r["status"] in ("readable", "unavailable") for r in result["channels"])


@windows
def test_missing_pid_is_explicit_failure(tmp_path, owned):
    proc = monitor(owned, tmp_path, "--pid", "4294967295")
    assert proc.wait(timeout=20) == 1
    assert any(r["event"] == "monitor_error" for r in records(tmp_path))


@windows
def test_pid_identity_rejects_stale_parent(tmp_path, owned):
    target = owned([sys.executable, "-c", "import time; time.sleep(30)"])
    journal = watch.Journal(tmp_path / "events.jsonl")
    tracker = watch.Tracker(watch.Windows(), journal)
    try:
        tracker.add(target.pid, 0, "python", parent={"created_ticks": 2**63})
        assert not tracker.active
    finally:
        tracker.close()
        journal.close()


@windows
def test_watch_mode_waits_without_starting_proxy(tmp_path, owned):
    proc = monitor(owned, tmp_path, "--watch-proxy", duration="0.5")
    assert proc.wait(timeout=20) == 0
    rows = records(tmp_path)
    assert rows[0]["mode"] == "watch"
    assert not any(row["event"] == "target_launched" for row in rows)


@windows
def test_watch_discovers_real_temporary_module(tmp_path, owned):
    # This isolated module shares only the invocation name, never imports or starts the proxy.
    module = tmp_path / "codebuddy_proxy.py"
    module.write_text("import time; time.sleep(30)\n", encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(tmp_path)
    target = subprocess.Popen([sys.executable, "-m", "codebuddy_proxy"], cwd=tmp_path, env=env,
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        # Discovery invokes CIM in a worker with a 10-second timeout. A 3-second
        # observer lifetime can end before a healthy query returns on a busy host.
        proc = monitor(owned, tmp_path / "monitor", "--watch-proxy", duration="12")
        row = wait_for(tmp_path / "monitor", lambda r: r["event"] == "process_observed" and r["pid"] == target.pid)
        assert row["name"].lower() == "python.exe"
        target.terminate()
        target.wait(timeout=5)
        assert proc.wait(timeout=20) == 0
        wait_for(tmp_path / "monitor", lambda r: r["event"] == "process_exit" and r["pid"] == target.pid)
    finally:
        if target.poll() is None:
            target.terminate()
        target.wait(timeout=5)


@windows
def test_launch_duration_leaves_exact_test_process_alive(tmp_path, owned):
    # Target self-expires even if this test fails. Only the emitted exact PID is opened.
    proc = monitor(owned, tmp_path, "--launch", sys.executable, "-c",
                   "import time; time.sleep(4)", duration="0.4")
    assert proc.wait(timeout=20) == 0
    pid = wait_for(tmp_path, lambda r: r["event"] == "target_launched")["pid"]
    api = watch.Windows()
    handle = api.open(pid)
    try:
        assert not api.exited(handle)
    finally:
        api.k.CloseHandle(handle)
