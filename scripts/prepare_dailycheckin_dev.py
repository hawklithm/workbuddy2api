#!/usr/bin/env python3
"""Build and install the local WorkBuddy auth and DailyCheckin dev wheels.

Run from any directory: python scripts/prepare_dailycheckin_dev.py
Both projects are synced by default. No repository is published or pushed.
"""
from __future__ import annotations

import argparse
from email.parser import Parser
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from zipfile import ZipFile


class PreparationError(RuntimeError):
    pass


def run(uv: str, args: list[str], cwd: Path, environment: dict[str, str], *, capture=False) -> str:
    print(f"[{cwd.name}] uv {' '.join(args)}", flush=True)
    try:
        result = subprocess.run([uv, *args], cwd=cwd, env=environment, check=True,
                                text=True, encoding='utf-8', errors='replace',
                                stdout=subprocess.PIPE if capture else None)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise PreparationError(f"command failed in {cwd}: uv {' '.join(args)} ({exc})") from exc
    return result.stdout.strip() if capture else ''


def project_version(uv: str, project: Path, environment: dict[str, str]) -> str:
    version = run(uv, ['version', '--short', '--no-sync'], project, environment, capture=True)
    if not re.fullmatch(r'[0-9]+\.[0-9]+(?:\.[0-9]+)?(?:[a-zA-Z0-9.+_-]*)?', version):
        raise PreparationError(f"unsupported project version: {version!r}")
    return version


def wheel_for(dist: Path, name: str, version: str) -> Path:
    filename = f"{name.replace('-', '_')}-{version.replace('-', '_')}-py3-none-any.whl"
    wheel = dist / filename
    if not wheel.is_file():
        raise PreparationError(f"expected wheel was not produced: {wheel}")
    with ZipFile(wheel) as archive:
        metadata_names = [n for n in archive.namelist() if n.endswith('.dist-info/METADATA')]
        if len(metadata_names) != 1:
            raise PreparationError(f"invalid wheel metadata: {wheel}")
        metadata = Parser().parsestr(archive.read(metadata_names[0]).decode('utf-8'), headersonly=True)
        if metadata.get('Version') != version or metadata.get('Name', '').lower().replace('_', '-') != name.lower().replace('_', '-'):
            raise PreparationError(f"unexpected wheel name/version: {wheel}")
    return wheel


def verify_wheel_sources(wheel: Path, package_dirs: dict[str, Path]) -> None:
    with ZipFile(wheel) as archive:
        for name, source in package_dirs.items():
            paths = source.rglob('*.py') if name == 'buddy_agent_auth' else source.glob('*.py')
            for path in paths:
                if '__pycache__' in path.parts:
                    continue
                archive_name = f"{name}/{path.relative_to(source).as_posix()}"
                try:
                    packaged = archive.read(archive_name)
                except KeyError as exc:
                    raise PreparationError(f"missing {archive_name} in {wheel}") from exc
                if packaged != path.read_bytes():
                    raise PreparationError(f"wheel contains stale content: {archive_name}")


def relative_source(project: Path, wheel: Path) -> str:
    try:
        return Path(os.path.relpath(wheel, project)).as_posix()
    except ValueError:  # Different drives on Windows.
        return wheel.as_posix()


def upper_bound(version: str) -> str:
    match = re.match(r'^(\d+)\.(\d+)', version)
    if match is None:
        raise PreparationError(f"cannot choose a compatibility bound for {version!r}")
    major, minor = map(int, match.groups())
    return f'{major + 1}.0' if major else f'0.{minor + 1}'



def update_daily_auth_source(project: Path, auth_wheel: Path, auth_version: str) -> None:
    path = project / 'pyproject.toml'
    raw = path.read_bytes()
    try:
        text = raw.decode('utf-8').replace('\r\n', '\n')
    except UnicodeDecodeError as exc:
        raise PreparationError('DailyCheckin pyproject.toml must be UTF-8') from exc
    dependency = f'    "buddy-agent-auth[trae]>={auth_version},<{upper_bound(auth_version)}",'
    text, dependency_count = re.subn(
        r'(?m)^[ \t]*"buddy-agent-auth\[trae\][^"]*"[ \t]*,[ \t]*$',
        lambda _: dependency, text)
    if dependency_count != 1:
        raise PreparationError(f'expected exactly one Trae authentication dependency in {path}')
    extra = (f'workbuddy = ["buddy-agent-auth>={auth_version},<{upper_bound(auth_version)}"]')
    text, count = re.subn(r'(?m)^workbuddy[ \t]*=[ \t]*\[[ \t]*"buddy-agent-auth[^"]*"[ \t]*\][ \t]*$',
                          lambda _: extra, text)
    if count != 1:
        raise PreparationError(f'expected exactly one WorkBuddy extra in {path}')
    source_line = f'buddy-agent-auth = {{ path = "{relative_source(project, auth_wheel)}" }}'
    section_pattern = r'(?ms)^\[tool\.uv\.sources\][ \t]*\n(?:(?!^\[)[^\n]*\n)*'
    sections = list(re.finditer(section_pattern, text))
    if len(sections) > 1:
        raise PreparationError(f'multiple uv sources sections in {path}')
    if sections:
        section = sections[0]
        body = section.group()
        body, replaced = re.subn(r'(?m)^buddy-agent-auth[ \t]*=[ \t]*\{[^\n]*\}[ \t]*$',
                                 lambda _: source_line, body)
        if replaced > 1:
            raise PreparationError(f'multiple WorkBuddy auth sources in {path}')
        if replaced == 0:
            body += source_line + '\n'
        text = text[:section.start()] + body + text[section.end():]
    else:
        text = text.rstrip('\n') + '\n\n[tool.uv.sources]\n' + source_line + '\n'
    newline = '\r\n' if b'\r\n' in raw else '\n'
    result = text.replace('\n', newline).encode('utf-8')
    if result != raw:
        fd, temp = tempfile.mkstemp(prefix='.dailycheckin-', suffix='.toml', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(result)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
        print(f'Updated local authentication wheel source in {path}', flush=True)


def update_workbuddy_sources(project: Path, daily_wheel: Path, daily_version: str,
                             auth_wheel: Path, auth_version: str) -> None:
    path = project / 'pyproject.toml'
    raw = path.read_bytes()
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise PreparationError('WorkBuddy pyproject.toml must be UTF-8') from exc
    newline = '\r\n' if b'\r\n' in raw else '\n'
    text = text.replace('\r\n', '\n')
    replacements = [
        (r'(?m)^daily-checkin[ \t]*=[ \t]*\{[^\n]*\}[ \t]*$',
         f'daily-checkin = {{ path = "{relative_source(project, daily_wheel)}" }}'),
        (r'(?m)^buddy-agent-auth[ \t]*=[ \t]*\{[^\n]*\}[ \t]*$',
         f'buddy-agent-auth = {{ path = "{relative_source(project, auth_wheel)}" }}'),
        (r'(?m)^[ \t]*"daily-checkin\[workbuddy\][^"]*"[ \t]*,[ \t]*$',
         f'    "daily-checkin[workbuddy]>={daily_version},<{upper_bound(daily_version)}",'),
        (r'(?m)^[ \t]*"buddy-agent-auth[^"]*"[ \t]*,[ \t]*$',
         f'    "buddy-agent-auth>={auth_version},<{upper_bound(auth_version)}",'),
    ]
    for pattern, replacement in replacements:
        text, count = re.subn(pattern, lambda _: replacement, text)
        if count != 1:
            raise PreparationError(f"expected exactly one matching entry in {path}: {pattern}")
    result = text.replace('\n', newline).encode('utf-8')
    if result != raw:
        fd, temp = tempfile.mkstemp(prefix='.dailycheckin-', suffix='.toml', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(result)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
        print(f'Updated local wheel sources in {path}', flush=True)


def verify_installed(uv: str, project: Path, environment: dict[str, str],
                     wheel: Path, package: str, file_name: str, version: str,
                     *, distribution: str | None = None) -> None:
    distribution = distribution or package.replace('_', '-')
    program = (
        'import hashlib,importlib,importlib.metadata,json,pathlib; '
        f'm=importlib.import_module({package!r}); '
        f'p=pathlib.Path(m.__file__).parent/{file_name!r}; '
        f'print(json.dumps({{"version":importlib.metadata.version({distribution!r}),'
        '"file":str(p),"sha256":hashlib.sha256(p.read_bytes()).hexdigest()}))'
    )
    output = run(uv, ['run', '--no-sync', 'python', '-c', program], project, environment, capture=True)
    try:
        actual = json.loads(output.splitlines()[-1])
        with ZipFile(wheel) as archive:
            expected = hashlib.sha256(archive.read(f"{package.replace('.', '/')}/{file_name}")).hexdigest()
    except (ValueError, KeyError, IndexError) as exc:
        raise PreparationError(f"could not verify installed {package} in {project}") from exc
    if actual['version'] != version or actual['sha256'] != expected:
        raise PreparationError(f"installed {package} does not match the built wheel in {project}")
    print(f"Verified {package} {version}: {actual['file']}", flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dailycheckin', type=Path, help='DailyCheckin project directory (default: sibling trae2api or dailycheckin)')
    parser.add_argument('--daily-env', type=Path, help='optional isolated uv environment for DailyCheckin')
    parser.add_argument('--workbuddy-env', type=Path, help='optional isolated uv environment for WorkBuddy2API')
    args = parser.parse_args(argv)
    workbuddy = Path(__file__).resolve().parents[1]
    candidates = [workbuddy.parent / 'trae2api', workbuddy.parent / 'dailycheckin']
    daily = args.dailycheckin.expanduser().resolve() if args.dailycheckin else next(
        (p.resolve() for p in candidates if (p / 'daily_checkin' / '__init__.py').is_file()), None)
    auth = workbuddy / 'packages' / 'buddy-agent-auth'
    uv = shutil.which('uv')
    if uv is None:
        raise PreparationError('uv was not found on PATH')
    if daily is None or not (daily / 'pyproject.toml').is_file() or not (daily / 'daily_checkin').is_dir():
        raise PreparationError('DailyCheckin not found; use --dailycheckin to specify its project directory')
    if not (auth / 'pyproject.toml').is_file():
        raise PreparationError(f'buddy-agent-auth project not found: {auth}')
    daily_env = os.environ.copy()
    workbuddy_env = os.environ.copy()
    for environment, path in ((daily_env, args.daily_env), (workbuddy_env, args.workbuddy_env)):
        if path is not None:
            environment['UV_PROJECT_ENVIRONMENT'] = str(path.expanduser().resolve())
    if (daily_env.get('UV_PROJECT_ENVIRONMENT') and
            daily_env.get('UV_PROJECT_ENVIRONMENT') == workbuddy_env.get('UV_PROJECT_ENVIRONMENT')):
        raise PreparationError('DailyCheckin and WorkBuddy2API must use separate uv environments')
    dist = daily / 'dist'
    dist.mkdir(parents=True, exist_ok=True)
    auth_version = project_version(uv, auth, daily_env)
    daily_version = project_version(uv, daily, daily_env)
    run(uv, ['build', str(auth), '--out-dir', str(dist), '--no-build-logs'], daily, daily_env)
    auth_wheel = wheel_for(dist, 'buddy-agent-auth', auth_version)
    verify_wheel_sources(auth_wheel, {'buddy_agent_auth': auth / 'src' / 'buddy_agent_auth'})
    update_daily_auth_source(daily, auth_wheel, auth_version)
    run(uv, ['lock', '--upgrade-package', 'buddy-agent-auth'], daily, daily_env)
    run(uv, ['sync', '--locked', '--inexact', '--extra', 'workbuddy', '--reinstall-package', 'buddy-agent-auth'], daily, daily_env)
    verify_installed(uv, daily, daily_env, auth_wheel, 'buddy_agent_auth', 'client.py', auth_version)
    verify_installed(uv, daily, daily_env, auth_wheel, 'buddy_agent_auth.providers.codebuddy', 'client.py',
                     auth_version, distribution='buddy-agent-auth')
    verify_installed(uv, daily, daily_env, auth_wheel, 'buddy_agent_auth.providers.trae', 'native_identity.py',
                     auth_version, distribution='buddy-agent-auth')
    run(uv, ['build', '--out-dir', str(dist), '--no-build-logs'], daily, daily_env)
    daily_wheel = wheel_for(dist, 'daily-checkin', daily_version)
    verify_wheel_sources(daily_wheel, {'daily_checkin': daily / 'daily_checkin'})
    with ZipFile(daily_wheel) as archive:
        if any(name.startswith('trae2api/') for name in archive.namelist()):
            raise PreparationError('DailyCheckin wheel contains legacy trae2api code')
    update_workbuddy_sources(workbuddy, daily_wheel, daily_version, auth_wheel, auth_version)
    run(uv, ['lock', '--upgrade-package', 'daily-checkin', '--upgrade-package', 'buddy-agent-auth'], workbuddy, workbuddy_env)
    run(uv, ['sync', '--locked', '--inexact', '--reinstall-package', 'daily-checkin', '--reinstall-package', 'buddy-agent-auth'], workbuddy, workbuddy_env)
    verify_installed(uv, workbuddy, workbuddy_env, auth_wheel, 'buddy_agent_auth', 'client.py', auth_version)
    verify_installed(uv, workbuddy, workbuddy_env, auth_wheel, 'buddy_agent_auth.providers.codebuddy', 'client.py',
                     auth_version, distribution='buddy-agent-auth')
    verify_installed(uv, workbuddy, workbuddy_env, auth_wheel, 'buddy_agent_auth.providers.trae', 'native_identity.py',
                     auth_version, distribution='buddy-agent-auth')
    verify_installed(uv, workbuddy, workbuddy_env, daily_wheel, 'daily_checkin', 'workbuddy.py', daily_version)
    verify_installed(uv, workbuddy, workbuddy_env, daily_wheel, 'daily_checkin', 'trae.py', daily_version)
    verify_installed(uv, workbuddy, workbuddy_env, auth_wheel, 'buddy_agent_auth.providers.codebuddy', 'login_jobs.py',
                     auth_version, distribution='buddy-agent-auth')
    verify_installed(uv, workbuddy, workbuddy_env, auth_wheel, 'buddy_agent_auth.providers.codebuddy', 'bootstrap.py',
                     auth_version, distribution='buddy-agent-auth')
    verify_installed(uv, workbuddy, workbuddy_env, auth_wheel, 'buddy_agent_auth.providers.trae', 'accounts.py',
                     auth_version, distribution='buddy-agent-auth')
    verify_installed(uv, workbuddy, workbuddy_env, daily_wheel, 'daily_checkin', 'trae_accounts.py', daily_version)
    print(f'Local development dependencies are ready. DailyCheckin wheel: {daily_wheel}', flush=True)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except PreparationError as exc:
        print(f'Preparation failed: {exc}', file=sys.stderr)
        raise SystemExit(1)
