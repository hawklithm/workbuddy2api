"""Exercise the actual setup functions against private HKCU scratch keys, never HKLM."""
import json
import os
from pathlib import Path
import subprocess
import uuid

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell and registry required")
SCRIPT = Path(__file__).parent / "scripts" / "manage_silent_exit.ps1"


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def run_functions(tmp_path, body):
    root = "HKCU:\\Software\\Workbuddy2apiSilentExitTest_" + uuid.uuid4().hex
    setup = """
$ErrorActionPreference = 'Stop'
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile(SCRIPT,[ref]$tokens,[ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
$definitions=$ast.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst]},$true)
foreach ($definition in $definitions) { . ([scriptblock]::Create($definition.Extent.Text)) }
$testRoot=TESTROOT
$ifeoRoot=$testRoot+'\\IFEO'
$speRoot=$testRoot+'\\SPE'
$filterName='Workbuddy2apiExitEvidence'
$backupPath=BACKUP
$changes=New-Object System.Collections.Generic.List[object]
try {
BODY
} finally {
  # Fixed generated HKCU test key; never remove any actual IFEO configuration.
  if ($testRoot -notlike 'HKCU:\\Software\\Workbuddy2apiSilentExitTest_*') { throw 'Bad cleanup root' }
  if (Test-Path -LiteralPath $testRoot) { Remove-Item -LiteralPath $testRoot -Recurse }
}
"""
    code = setup.replace("SCRIPT", quote(SCRIPT)).replace("TESTROOT", quote(root)).replace(
        "BACKUP", quote(tmp_path / "journal.json")).replace("BODY", body)
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", code],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_atomic_journal_retains_previous_revision(tmp_path):
    output = run_functions(tmp_path, """
Save-Json $backupPath @{value=1}
Save-Json $backupPath @{value=2}
Save-Json $backupPath @{value=3}
@{current=(Get-Content $backupPath -Raw|ConvertFrom-Json).value;
  previous=(Get-Content ($backupPath+'.previous') -Raw|ConvertFrom-Json).value}|ConvertTo-Json
""")
    assert json.loads(output) == dict(current=3, previous=2)


def test_scope_preserves_existing_values_and_rolls_back(tmp_path):
    output = run_functions(tmp_path, """
$base=$ifeoRoot+'\\workbuddy2api-evidence.exe'
$null=New-Item -Path $base -Force
$null=New-ItemProperty -Path $base -Name UseFilter -Value 0 -PropertyType DWord
$null=New-ItemProperty -Path $base -Name GlobalFlag -Value 2 -PropertyType DWord
$null=New-ItemProperty -Path $base -Name Sentinel -Value 'keep' -PropertyType String
Set-Scope 'C:\\isolated-test\\workbuddy2api-evidence.exe' (Split-Path $backupPath -Parent) $changes $backupPath
$configured=(Get-ItemProperty -LiteralPath $base).GlobalFlag
Restore-RecordedValues $backupPath
$restored=Get-ItemProperty -LiteralPath $base
@{configured=$configured;use_filter=$restored.UseFilter;global_flag=$restored.GlobalFlag;
  sentinel=$restored.Sentinel;filter_exists=(Test-Path ($base+'\\'+$filterName));
  spe_exists=(Test-Path ($speRoot+'\\workbuddy2api-evidence.exe'))}|ConvertTo-Json
""")
    assert json.loads(output) == dict(configured=514, use_filter=0, global_flag=2,
                                     sentinel="keep", filter_exists=False, spe_exists=False)


def test_existing_configuration_is_not_overwritten(tmp_path):
    output = run_functions(tmp_path, """
$existing=$speRoot+'\\workbuddy2api-evidence.exe'
$null=New-Item -Path $existing -Force
$null=New-ItemProperty -Path $existing -Name ReportingMode -Value 4 -PropertyType DWord
$rejected=$false
try { Set-Scope 'C:\\isolated-test\\workbuddy2api-evidence.exe' (Split-Path $backupPath -Parent) $changes $backupPath }
catch { $rejected=$true }
@{rejected=$rejected;reporting_mode=(Get-ItemProperty $existing).ReportingMode;
  journal_written=(Test-Path $backupPath)}|ConvertTo-Json
""")
    assert json.loads(output) == dict(rejected=True, reporting_mode=4, journal_written=False)


def test_rollback_does_not_overwrite_later_external_change(tmp_path):
    output = run_functions(tmp_path, """
Set-Scope 'C:\\isolated-test\\workbuddy2api-evidence.exe' (Split-Path $backupPath -Parent) $changes $backupPath
$path=$speRoot+'\\workbuddy2api-evidence.exe'
Set-ItemProperty -LiteralPath $path -Name ReportingMode -Value 4
$conflict=$false
try { Restore-RecordedValues $backupPath } catch { $conflict=$_.Exception.Message -like '*Rollback conflict*' }
@{conflict=$conflict;external_value=(Get-ItemProperty $path).ReportingMode}|ConvertTo-Json
""")
    assert json.loads(output) == dict(conflict=True, external_value=4)


def test_shared_python_image_is_refused_before_registry_changes(tmp_path):
    output = run_functions(tmp_path, """
$refused=$false
try { Set-Scope 'C:\\some-venv\\python.exe' (Split-Path $backupPath -Parent) $changes $backupPath }
catch { $refused=$_.Exception.Message -like '*dedicated diagnostic image*' }
@{refused=$refused;journal_written=(Test-Path $backupPath);registry_written=(Test-Path $testRoot)}|ConvertTo-Json
""")
    assert json.loads(output) == dict(refused=True, journal_written=False, registry_written=False)
