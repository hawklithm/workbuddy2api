# Process diagnostics (2.0.7)

This release adds process-level evidence for unattended proxy failures without
changing the HTTP/LLM business logic. Files use the directory of `--log-file`
(default `~/.workbuddy2api/`).

## Files

| File | Content | Retention |
| --- | --- | --- |
| `console.log` | Python-level stdout/stderr, including ordinary tracebacks and Uvicorn error logs, while still displaying them in the terminal | 20 MiB per file, 4 rotations (up to ~100 MiB) |
| `process-events.jsonl` | Structured process start, exit code/reason, caught signal, fatal exception class, memory measurements, unhandled asyncio/thread exceptions and diagnostic log failures | 2 MiB per file, 3 rotations |
| `crash.log` | `faulthandler` native/Python thread stacks for fatal Python runtime signals | Append-only; inspect/truncate manually after incidents |
| `proxy.log`, `codebuddy-proxy.jsonl` | Existing request, proxy and operational logs (no semantic changes) | Existing daily retention |

The new files are created on startup, and on POSIX new files use mode `0600`.
On Windows their ACL is inherited from the containing directory; protect that
directory if multiple users share the machine.

**Warning:** Unlike the structured process events, console and crash outputs
can include exception content, upstream fragments, tokens or other private data
printed by dependencies. Do not attach raw files to public issues without
reviewing and redacting them.

## How to inspect an incident

1. Find the last `process_start` in `process-events.jsonl` and its `run_id`.
2. Check for `process_fatal_exception`, `process_signal_received`,
   `asyncio_unhandled_exception`, `thread_unhandled_exception`, or
   `diagnostic_log_failure` for that run.
3. Match with its `process_exit` and `exit_code`. A normal graceful
   `uvicorn.run()` return records code `0`. A caught uncaught exception
   records `1`, and SIGTERM can record `143` (128+15).
4. Read `console.log` and `crash.log` for the traceback, without relying on
   exception messages in the JSONL journal.
5. If the process terminates abruptly (e.g. Windows TerminateProcess, kill -9,
   host shutdown), Python cannot run an exit handler. On its **next** launch,
   `previous_run_exit_unobserved` records the previous PID/run ID if the
   most recent start has no matching exit. This is not proof of a crash;
   simultaneous proxy instances can also result in an unmatched run.

## Signal and output capture limitations

Signals handled by Uvicorn are observed with a delegating wrapper so its
original signal behavior runs first. SIGTERM is recorded even if Uvicorn
gracefully stops and replays the signal. Windows force termination bypasses
Python handlers, so it is detectable only retrospectively. The Python tee
captures `sys.stdout` and `sys.stderr`; writes directly to native file
descriptors, subprocess handles or the original binary stream are not fully
mirrored. Use a terminal/supervisor-level redirection if those must also be
preserved.

Logs are best-effort by design: a failed console or JSONL write is reported
in `process-events.jsonl` if possible and to the original stderr without
crashing the proxy. If the entire log volume becomes inaccessible, diagnostics
may be incomplete.

## Version

`pyproject.toml` and FastAPI metadata are `2.0.7`. From a source checkout,
run `uv sync` before starting to refresh installed distribution metadata
used by the runtime startup event. The current running proxy is not restarted
by implementing this change.

## Tests

`test_process_diagnostics.py` exercises console mirroring, rotation, normal
and abnormal exits, SIGTERM, forced child-process termination, unobserved exit
detection, traceback retention, asyncio/thread exception forwarding, memory
snapshots, log failure fallback, and the version contract.
