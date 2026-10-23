"""Real isolated interpreter checks; no proxy server or forced termination."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Native Windows runtime")
ROOT = Path(__file__).parent
spec = importlib.util.spec_from_file_location("prepare_native_runtime", ROOT / "scripts" / "prepare_native_runtime.py")
prepare_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare_module)


@pytest.fixture
def runtime(tmp_path):
    return prepare_module.prepare(tmp_path / "runtime with spaces", sys.prefix)


@pytest.mark.parametrize("kind", ["runtime", "observer"])
def test_actual_image_and_pid_run_original_dependencies(runtime, tmp_path, kind):
    target = tmp_path / "identity.json"
    env = dict(os.environ, PATH=str(Path(os.environ['SystemRoot']) / 'System32'))
    process = subprocess.Popen([runtime[kind], runtime["bootstrap"], "--identity", str(target)], env=env)
    assert process.wait(timeout=15) == 0
    identity = json.loads(target.read_text(encoding="utf-8"))
    assert identity["pid"] == process.pid  # A redirector would report a different PID.
    assert Path(identity["image"]) == Path(runtime[kind])
    assert Path(identity["executable"]) == Path(runtime[kind])
    assert Path(identity["fastapi"]).is_relative_to(Path(sys.prefix))
    import codebuddy_proxy.__main__ as proxy
    assert Path(identity["proxy"]) == Path(proxy.__file__)


def test_dedicated_runtime_does_not_overwrite_existing_directory(runtime):
    with pytest.raises(ValueError, match="already exists"):
        prepare_module.prepare(Path(runtime["bootstrap"]).parent, sys.prefix)


def test_script_arguments_and_explicit_exit_are_preserved(runtime, tmp_path):
    script = tmp_path / "argv script.py"
    output = tmp_path / "argv.json"
    script.write_text("import sys,json; from pathlib import Path\n"
                      "Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))\n"
                      "raise SystemExit(31)\n", encoding="utf-8")
    args = ["space value", 'literal"quote', "trailing\\", ""]
    result = subprocess.run([runtime["runtime"], runtime["bootstrap"], "--script",
                             str(script), str(output), *args], timeout=15)
    assert result.returncode == 31
    assert json.loads(output.read_text()) == args


def test_powershell_argument_quoting_roundtrips(runtime, tmp_path):
    output = tmp_path / "quoted args.json"
    script = tmp_path / "print args.py"
    script.write_text("import sys,json; from pathlib import Path\n"
                      "Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))\n", encoding="utf-8")
    def quote(value):
        return "'" + str(value).replace("'", "''") + "'"
    args = [runtime["bootstrap"], "--script", str(script), str(output),
            "space value", 'literal"quote', "trailing\\", ""]
    code = """
$ErrorActionPreference='Stop'
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile(SCRIPT,[ref]$tokens,[ref]$errors)
if($errors.Count){throw ($errors|Out-String)}
$fn=$ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Quote-NativeArgument'},$true)[0]
. ([scriptblock]::Create($fn.Extent.Text))
$arguments=@(ARGUMENTS)
$info=New-Object Diagnostics.ProcessStartInfo
$info.FileName=IMAGE
$info.Arguments=($arguments|ForEach-Object {Quote-NativeArgument $_}) -join ' '
$info.UseShellExecute=$false
$info.CreateNoWindow=$true
$process=[Diagnostics.Process]::Start($info)
if(-not $process.WaitForExit(15000) -or $process.ExitCode -ne 0){throw 'argument test failed'}
""".replace("SCRIPT", quote(ROOT / "scripts" / "start_proxy_with_evidence.ps1")).replace(
        "ARGUMENTS", ",".join(quote(a) for a in args)).replace("IMAGE", quote(runtime["runtime"]))
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", code],
                            capture_output=True, text=True, timeout=25)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(output.read_text()) == args[4:]


def test_console_free_observer_records_only_temporary_workload(runtime, tmp_path):
    stdout, stderr = tmp_path / 'observer.out', tmp_path / 'observer.err'
    with stdout.open('w') as out, stderr.open('w') as err:
        process = subprocess.Popen([runtime['observer'], runtime['bootstrap'], '--script',
            str(ROOT / 'scripts' / 'watch_processes.py'), '--output', str(tmp_path / 'monitor'),
            '--no-health', '--no-system-events', '--no-business-log', '--launch',
            runtime['runtime'], runtime['bootstrap'], '-c', 'import time; time.sleep(2)'],
            stdout=out, stderr=err, creationflags=subprocess.CREATE_NO_WINDOW)
        assert process.wait(timeout=20) == 0
    run_dir = Path(stdout.read_text().strip())
    rows = [json.loads(line) for line in (run_dir / 'events.jsonl').read_text().splitlines()]
    launched = next(row for row in rows if row['event'] == 'target_launched')
    exited = next(row for row in rows if row['event'] == 'process_exit' and row['pid'] == launched['pid'])
    assert exited['exit_code'] == 0
    assert stderr.read_text() == ''


def test_start_refuses_tampered_runtime_before_launching(runtime, tmp_path):
    state = tmp_path / 'state'
    state.mkdir()
    (state / 'result.json').write_text(json.dumps(dict(status='native_runtime_enabled',
        runtime_root=str(Path(runtime['bootstrap']).parent))))
    with Path(runtime['bootstrap']).open('a') as output:
        output.write('\n# tampered\n')
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-File',
        str(ROOT / 'scripts' / 'start_proxy_with_evidence.ps1'), '-StateDirectory', str(state),
        '-CheckOnly'], capture_output=True, text=True, timeout=15)
    assert result.returncode != 0
    assert 'Runtime content changed' in result.stderr
