"""Run with the dedicated, real CPython image (never a venv redirector)."""
import json
import os
from pathlib import Path
import runpy
import site
import sys


def main():
    root = Path(__file__).resolve().parent
    config = json.loads((root / "runtime.json").read_text(encoding="utf-8"))
    # Anaconda's extensions can depend on DLLs outside CPython's DLLs directory.
    # Retain handles for the whole workload; closing them removes the search paths.
    handles = [os.add_dll_directory(str(path)) for path in
               (Path(config["base"]), Path(config["base"]) / "DLLs",
                Path(config["base"]) / "Library" / "bin") if path.is_dir()]
    site.addsitedir(config["site_packages"])
    args = sys.argv[1:]
    if len(args) < 2 or args[0] not in ("-m", "--script", "-c", "--identity"):
        raise SystemExit("Expected -m MODULE, --script PATH, -c CODE, or --identity FILE")
    mode, target, *rest = args
    sys.argv = [target, *rest]
    if mode == "--identity":
        import ctypes
        import importlib
        import ssl
        import sqlite3
        import fastapi
        proxy = importlib.import_module("codebuddy_proxy.__main__")
        image = ctypes.create_unicode_buffer(32768)
        if not ctypes.windll.kernel32.GetModuleFileNameW(None, image, len(image)):
            raise OSError("Cannot identify actual process image")
        Path(target).write_text(json.dumps(dict(pid=os.getpid(), image=image.value,
            executable=sys.executable, prefix=sys.prefix, base_prefix=sys.base_prefix,
            fastapi=fastapi.__file__, proxy=proxy.__file__)), encoding="utf-8")
    elif mode == "-m":
        runpy.run_module(target, run_name="__main__", alter_sys=True)
    elif mode == "--script":
        sys.path.insert(0, str(Path(target).resolve().parent))
        runpy.run_path(target, run_name="__main__")
    else:
        exec(compile(target, "<isolated-native-test>", "exec"), {"__name__": "__main__"})
    # 'handles' deliberately remains referenced until workload completion.


if __name__ == "__main__":
    main()
