"""Build an isolated CPython image using the existing venv's dependencies."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys


def prepare(destination, venv):
    destination, venv = Path(destination), Path(venv).resolve()
    base = Path(sys.base_prefix).resolve()
    if destination.exists():
        raise ValueError("Runtime directory already exists; use a fresh state directory")
    scripts = destination / "Scripts"
    scripts.mkdir(parents=True)
    (destination / "Lib" / "site-packages").mkdir(parents=True)
    # Copy the actual executable, NOT .venv/Scripts/python.exe (a redirector).
    for name, source in (("workbuddy2api-runtime.exe", "python.exe"),
                         ("workbuddy2api-observer.exe", "pythonw.exe"),
                         ("workbuddy-native-probe.exe", "python.exe")):
        shutil.copy2(base / source, scripts / name)
    for source in base.iterdir():
        if source.is_file() and source.suffix.lower() == ".dll" and (
                source.name.lower().startswith("python") or
                source.name.lower().startswith("vcruntime") or
                source.name.lower().startswith("zlib")):
            shutil.copy2(source, scripts / source.name)
    (destination / "pyvenv.cfg").write_text(
        f"home = {base}\ninclude-system-site-packages = false\n", encoding="utf-8")
    config = dict(base=str(base), venv=str(venv), site_packages=str(venv / "Lib" / "site-packages"))
    (destination / "runtime.json").write_text(json.dumps(config), encoding="utf-8")
    shutil.copy2(Path(__file__).with_name("native_runtime_bootstrap.py"),
                 destination / "bootstrap.py")
    files = {str(p.relative_to(destination)): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in destination.rglob("*") if p.is_file()}
    sources = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in
               [base / "python.exe", base / "pythonw.exe", venv / "pyvenv.cfg",
                *[base / p.name for p in scripts.glob('*.dll')]]}
    manifest = dict(config, files=files, sources=sources, python_version=sys.version,
                    runtime=str(scripts / "workbuddy2api-runtime.exe"),
                    observer=str(scripts / "workbuddy2api-observer.exe"),
                    probe=str(scripts / "workbuddy-native-probe.exe"),
                    bootstrap=str(destination / "bootstrap.py"))
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("venv", type=Path)
    args = parser.parse_args()
    prepare(args.destination, args.venv)
