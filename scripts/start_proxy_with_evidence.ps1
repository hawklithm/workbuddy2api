#requires -Version 5.1
[CmdletBinding()]
param(
    [string]$StateDirectory,
    [string[]]$ProxyArguments = @('--desensitize','--host','0.0.0.0'),
    [switch]$CheckOnly
)
$ErrorActionPreference = 'Stop'
if (-not $StateDirectory) {
    $stateRoot = Join-Path $HOME '.workbuddy2api\silent-exit'
    $candidates = @(Get-ChildItem -LiteralPath $stateRoot -Directory -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending)
    foreach ($candidate in $candidates) {
        $resultPath = Join-Path $candidate.FullName 'result.json'
        if ((Test-Path -LiteralPath $resultPath) -and
            -not (Test-Path -LiteralPath (Join-Path $candidate.FullName 'rollback-result.json'))) {
            $record = Get-Content -LiteralPath $resultPath -Raw | ConvertFrom-Json
            if ($record.status -eq 'enabled_for_future_starts') { $StateDirectory = $candidate.FullName; break }
        }
    }
}
if (-not $StateDirectory) { throw 'No validated Silent Process Exit setup found. Run manage_silent_exit.ps1 first.' }
$record = Get-Content -LiteralPath (Join-Path $StateDirectory 'result.json') -Raw | ConvertFrom-Json
if ($record.status -ne 'enabled_for_future_starts' -or
    (Test-Path -LiteralPath (Join-Path $StateDirectory 'rollback-result.json'))) {
    throw 'The selected setup is not enabled'
}
$target = (Get-Item -LiteralPath $record.target).FullName
$python = (Get-Item -LiteralPath $record.original_target).FullName
if ([IO.Path]::GetFileName($target) -ine 'workbuddy2api-evidence.exe' -or
    [IO.Path]::GetDirectoryName($target) -ine [IO.Path]::GetDirectoryName($python)) {
    throw 'Diagnostic launcher must be adjacent to the original venv interpreter'
}
foreach ($image in @($target,$python)) {
    if ((Get-FileHash -LiteralPath $image -Algorithm SHA256).Hash -ne $record.launcher_sha256) {
        throw 'Launcher changed since validation; revalidate before starting'
    }
}
$ifeo = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\workbuddy2api-evidence.exe'
$spe = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\SilentProcessExit\workbuddy2api-evidence.exe'
$flags = Get-ItemProperty -LiteralPath $ifeo
$settings = Get-ItemProperty -LiteralPath $spe
if (-not ($flags.GlobalFlag -band 0x200) -or -not ($settings.ReportingMode -band 2) -or
    $settings.IgnoreSelfExits -ne 0 -or $settings.LocalDumpFolder -ine $record.dump_folder) {
    throw 'Windows settings differ from the validated setup'
}
$projectRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $python))
$monitorScript = Join-Path $PSScriptRoot 'watch_processes.py'
if ($CheckOnly) {
    @{status='ready';state_directory=$StateDirectory;project=$projectRoot;target=$target;
      monitor=$monitorScript;dump_folder=$record.dump_folder;proxy_started=$false} | ConvertTo-Json
    exit 0
}
$existing = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='uv.exe' OR Name='workbuddy2api-evidence.exe'" |
    Where-Object { $_.CommandLine -match '(?:^|\s)-m\s+codebuddy_proxy(?:\s|$)' })
if ($existing.Count) { throw 'A proxy module process already exists; refusing to start another or stop it.' }
Write-Host 'Starting proxy with independent observation and Windows silent-exit evidence.'
Write-Host 'The next line is the evidence directory; native proxy output is saved there in target-stdout.log / target-stderr.log.'
Push-Location -LiteralPath $projectRoot
try {
    & $python $monitorScript --launch $target -m codebuddy_proxy @ProxyArguments
    $status = $LASTEXITCODE
} finally { Pop-Location }
exit $status
