#!/usr/bin/env python3
"""Portable test runner for this repository (Windows/macOS/Linux).

Run from any directory: python scripts/run_tests.py
Optional: python scripts/run_tests.py --quick
Optional: python scripts/run_tests.py --compare-main
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys


def run(command: list[str], *, cwd: Path, env: dict[str, str]) -> int:
    print("\n$ " + " ".join(command), flush=True)
    result = subprocess.run(command, cwd=str(cwd), env=env, check=False)
    print(f"Exit code: {result.returncode}", flush=True)
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Run repository tests with src imports configured")
    parser.add_argument("--quick", action="store_true", help="Only run login header tests")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    src = root / "src"
    if not (src / "codebuddy_proxy").is_dir():
        print(f"ERROR: missing package directory: {src / 'codebuddy_proxy'}", file=sys.stderr)
        return 2

    env = os.environ.copy()
    old_path = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(src), str(root), old_path) if p)

    print(f"Project: {root}")
    print(f"Python: {sys.executable}")
    print(f"PYTHONPATH: {env['PYTHONPATH']}")
    print("No session files will be deleted or modified by this runner.")
    check = subprocess.run([sys.executable, "-c", "import pytest"], cwd=str(root), env=env, check=False, capture_output=True, text=True)
    if check.returncode != 0:
        print("\\nERROR: pytest is not installed for this Python interpreter.", file=sys.stderr)
        print("Create an isolated environment and install pytest:", file=sys.stderr)
        print("  python3 -m venv .venv", file=sys.stderr)
        print("  .venv/bin/python -m pip install pytest", file=sys.stderr)
        print("  .venv/bin/python scripts/run_tests.py", file=sys.stderr)
        print("On Windows use .venv\\\\Scripts\\\\python.exe instead of .venv/bin/python.", file=sys.stderr)
        return 2

    tests = [["test_login_headers.py", "-vv", "--tb=long"]]
    if not args.quick:
        tests.append(["-q", "--tb=short"])

    failed = False
    for options in tests:
        rc = run([sys.executable, "-m", "pytest", *options], cwd=root, env=env)
        failed = failed or rc != 0

    print("\nRESULT: " + ("FAIL (see pytest output above)" if failed else "PASS"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
