#!/usr/bin/env python3
"""Run project tests consistently on Windows, Linux, and WSL.

First run in an existing virtual environment:
    python scripts/run_tests.py --setup

Subsequent runs:
    python scripts/run_tests.py
    python scripts/run_tests.py --quick

The --setup option installs this repository's declared [dev] dependencies
into the *active virtual environment*, then runs the tests.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import subprocess
import sys


REQUIRED_MODULES = ("pytest", "fastapi", "httpx", "uvicorn", "requests")


def run(command: list[str], *, cwd: Path, env: dict[str, str]) -> int:
    print("\n$ " + " ".join(command), flush=True)
    try:
        result = subprocess.run(command, cwd=str(cwd), env=env, check=False)
    except OSError as exc:
        print(f"Unable to execute command: {exc}", file=sys.stderr)
        return 127
    print(f"Exit code: {result.returncode}", flush=True)
    return result.returncode


def missing_modules() -> list[str]:
    return [module for module in REQUIRED_MODULES if importlib.util.find_spec(module) is None]


def main() -> int:
    parser = argparse.ArgumentParser(description="Run repository tests with isolated Python dependencies")
    parser.add_argument("--quick", action="store_true", help="Run only test_login_headers.py")
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Install the project's [dev] dependencies into the active virtual environment before testing",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    src = root / "src"
    if not (src / "codebuddy_proxy").is_dir():
        print(f"ERROR: missing package directory: {src / 'codebuddy_proxy'}", file=sys.stderr)
        return 2

    env = os.environ.copy()
    existing_path = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(src), str(root), existing_path) if p)

    print(f"Project: {root}", flush=True)
    print(f"Python: {sys.executable}", flush=True)
    print(f"PYTHONPATH: {env['PYTHONPATH']}", flush=True)

    if args.setup:
        if sys.prefix == sys.base_prefix:
            print(
                "ERROR: --setup requires a virtual environment; refusing to install into system Python.",
                file=sys.stderr,
            )
            print("Create one first: python3 -m venv .venv", file=sys.stderr)
            return 2
        print("Installing project dependencies, including pytest, from pyproject.toml.", flush=True)
        install = run(
            [sys.executable, "-m", "pip", "install", "-e", ".[dev]"],
            cwd=root,
            env=env,
        )
        if install != 0:
            print("RESULT: SETUP FAILED (see pip output above)", file=sys.stderr)
            return install

    missing = missing_modules()
    if missing:
        print(f"\nERROR: missing Python modules: {', '.join(missing)}", file=sys.stderr)
        print("Within your virtual environment, run this one-time setup:", file=sys.stderr)
        print(f'  "{sys.executable}" scripts/run_tests.py --setup', file=sys.stderr)
        return 2

    test_options = [["test_login_headers.py", "-vv", "--tb=long"]]
    if not args.quick:
        test_options.append(["-q", "--tb=short"])

    failures = 0
    for options in test_options:
        if run([sys.executable, "-m", "pytest", *options], cwd=root, env=env) != 0:
            failures += 1

    print(f"\nRESULT: {'PASS' if failures == 0 else f'FAIL ({failures} test command(s) failed)'}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
