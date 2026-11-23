"""Offline checks for the local dev-wheel preparation script."""
import importlib.util
from pathlib import Path
from zipfile import ZipFile

import pytest


SCRIPT = Path(__file__).parent / 'scripts' / 'prepare_dailycheckin_dev.py'
spec = importlib.util.spec_from_file_location('prepare_dailycheckin_dev', SCRIPT)
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


def test_updates_only_expected_sources_and_is_idempotent(tmp_path):
    workbuddy = tmp_path / 'workbuddy2api'
    workbuddy.mkdir()
    daily = tmp_path / 'daily' / 'dist'
    daily.mkdir(parents=True)
    config = workbuddy / 'pyproject.toml'
    original = ('[project]\r\ndependencies = [\r\n'
                '    "buddy-agent-auth>=0.1.0,<0.2",\r\n'
                '    "daily-checkin[workbuddy]>=0.1.1.dev1,<0.2",\r\n'
                '    "other-package>=3",\r\n]\r\n\r\n[tool.uv.sources]\r\n'
                'buddy-agent-auth = { path = "packages/buddy-agent-auth", editable = true }\r\n'
                'daily-checkin = { path = "../old" }\r\n')
    config.write_bytes(original.encode())
    daily_wheel = daily / 'daily_checkin-0.1.2.dev1-py3-none-any.whl'
    auth_wheel = daily / 'buddy_agent_auth-0.1.1-py3-none-any.whl'
    prepare.update_workbuddy_sources(workbuddy, daily_wheel, '0.1.2.dev1', auth_wheel, '0.1.1')
    data = config.read_bytes()
    assert b'other-package>=3' in data
    assert b'daily-checkin[workbuddy]>=0.1.2.dev1,<0.2' in data
    assert b'buddy-agent-auth>=0.1.1,<0.2' in data
    assert b'../daily/dist/daily_checkin-0.1.2.dev1-py3-none-any.whl' in data
    assert b'../daily/dist/buddy_agent_auth-0.1.1-py3-none-any.whl' in data
    assert b'\r\n' in data and b'\n' not in data.replace(b'\r\n', b'')
    prepare.update_workbuddy_sources(workbuddy, daily_wheel, '0.1.2.dev1', auth_wheel, '0.1.1')
    assert config.read_bytes() == data


def test_unexpected_configuration_is_not_partially_overwritten(tmp_path):
    config = tmp_path / 'pyproject.toml'
    config.write_text('[project]\nname = "custom"\n', encoding='utf-8')
    before = config.read_bytes()
    with pytest.raises(prepare.PreparationError, match='expected exactly one'):
        prepare.update_workbuddy_sources(tmp_path, tmp_path / 'daily.whl', '0.1.1.dev1',
                                        tmp_path / 'auth.whl', '0.1.0')
    assert config.read_bytes() == before


def test_wheel_must_match_source(tmp_path):
    source = tmp_path / 'pkg'
    source.mkdir()
    (source / 'example.py').write_bytes(b'current')
    wheel = tmp_path / 'demo.whl'
    with ZipFile(wheel, 'w') as archive:
        archive.writestr('pkg/example.py', b'old')
    with pytest.raises(prepare.PreparationError, match='stale content'):
        prepare.verify_wheel_sources(wheel, {'pkg': source})


def test_separate_environments_required(tmp_path, monkeypatch):
    project = tmp_path / 'daily'
    (project / 'daily_checkin').mkdir(parents=True)
    (project / 'pyproject.toml').write_text('')
    monkeypatch.setattr(prepare.shutil, 'which', lambda name: 'uv')
    with pytest.raises(prepare.PreparationError, match='separate uv environments'):
        prepare.main(['--dailycheckin', str(project), '--daily-env', str(tmp_path / 'env'),
                      '--workbuddy-env', str(tmp_path / 'env')])


def test_daily_auth_source_is_created_and_other_uv_sources_are_preserved(tmp_path):
    config = tmp_path / 'pyproject.toml'
    config.write_text('[project]\ndependencies = [\n    "buddy-agent-auth[trae]>=0.1.0,<0.2",\n]\n'
                      '[project.optional-dependencies]\nworkbuddy = ["buddy-agent-auth>=0.1.0,<0.2"]\n\n'
                      '[tool.uv.sources]\nother = { path = "other" }\n', encoding='utf-8')
    wheel = tmp_path / 'dist' / 'buddy_agent_auth-0.1.2-py3-none-any.whl'
    prepare.update_daily_auth_source(tmp_path, wheel, '0.1.2')
    content = config.read_text(encoding='utf-8')
    assert 'other = { path = "other" }' in content
    assert 'buddy-agent-auth>=0.1.2,<0.2' in content
    assert 'buddy-agent-auth = { path = "dist/buddy_agent_auth-0.1.2-py3-none-any.whl" }' in content
    prepare.update_daily_auth_source(tmp_path, wheel, '0.1.2')
    assert config.read_text(encoding='utf-8') == content


def test_daily_auth_source_refuses_to_replace_unrecognized_extra(tmp_path):
    config = tmp_path / 'pyproject.toml'
    config.write_text('[project]\ndependencies = [\n    "buddy-agent-auth[trae]>=0.1.0,<0.2",\n]\n[project.optional-dependencies]\nworkbuddy = ["buddy-agent-auth", "other"]\n')
    before = config.read_bytes()
    with pytest.raises(prepare.PreparationError, match='exactly one'):
        prepare.update_daily_auth_source(tmp_path, tmp_path / 'x.whl', '0.1.0')
    assert config.read_bytes() == before


def test_wheel_checks_nested_brand_provider_sources(tmp_path):
    root = tmp_path / 'buddy_agent_auth'
    provider = root / 'providers' / 'trae'
    provider.mkdir(parents=True)
    (root / '__init__.py').write_bytes(b'root')
    (provider / 'native_identity.py').write_bytes(b'new-provider')
    wheel = tmp_path / 'auth.whl'
    with ZipFile(wheel, 'w') as archive:
        archive.writestr('buddy_agent_auth/__init__.py', b'root')
        archive.writestr('buddy_agent_auth/providers/trae/native_identity.py', b'old-provider')
    with pytest.raises(prepare.PreparationError, match='stale content'):
        prepare.verify_wheel_sources(wheel, {'buddy_agent_auth': root})
