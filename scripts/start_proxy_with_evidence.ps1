#requires -Version 5.1
[CmdletBinding()]
param(
    [string]$StateDirectory,
    [string[]]$ProxyArguments = @('--desensitize','--host','0.0.0.0'),
    [switch]$CheckOnly
)
$ErrorActionPreference = 'Stop'
# Python subprocesses can inherit PowerShell 7's PSModulePath when invoking 5.1.
# Load the host's own utility module rather than resolving a different edition.
Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Utility\Microsoft.PowerShell.Utility.psd1') -ErrorAction Stop
if (-not $StateDirectory) {
    $stateRoot = Join-Path $HOME '.workbuddy2api\silent-exit'
    foreach ($candidate in @(Get-ChildItem -LiteralPath $stateRoot -Directory -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending)) {
        $resultPath = Join-Path $candidate.FullName 'result.json'
        if ((Test-Path $resultPath) -and -not (Test-Path (Join-Path $candidate.FullName 'rollback-result.json'))) {
            $record = Get-Content $resultPath -Raw | ConvertFrom-Json
            if ($record.status -eq 'native_runtime_enabled') { $StateDirectory = $candidate.FullName; break }
        }
    }
}
if (-not $StateDirectory) { throw 'No validated native runtime setup. Run manage_native_runtime.ps1 first; launcher-only setups are insufficient.' }
$record = Get-Content (Join-Path $StateDirectory 'result.json') -Raw | ConvertFrom-Json
if ($record.status -ne 'native_runtime_enabled' -or (Test-Path (Join-Path $StateDirectory 'rollback-result.json'))) {
    throw 'The selected native runtime setup is not enabled'
}
$runtimeRoot = (Get-Item -LiteralPath $record.runtime_root).FullName
$manifest = Get-Content (Join-Path $runtimeRoot 'manifest.json') -Raw | ConvertFrom-Json
if (-not $manifest.sources) { throw 'Missing source-runtime validation; revalidate the native setup' }
foreach ($source in $manifest.sources.PSObject.Properties) {
    if ((Get-FileHash -LiteralPath $source.Name -Algorithm SHA256).Hash -ine $source.Value) {
        throw 'Base Python or venv configuration changed; revalidate before starting'
    }
}
foreach ($entry in $manifest.files.PSObject.Properties) {
    $file = [IO.Path]::GetFullPath((Join-Path $runtimeRoot $entry.Name))
    if (-not $file.StartsWith($runtimeRoot + '\', [StringComparison]::OrdinalIgnoreCase) -or
        (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash -ine $entry.Value) {
        throw 'Runtime content changed since validation; revalidate before starting'
    }
}
foreach ($image in @($manifest.runtime,$manifest.observer)) {
    $name = [IO.Path]::GetFileName($image)
    if ($name -notin @('workbuddy2api-runtime.exe','workbuddy2api-observer.exe') -or
        [IO.Path]::GetDirectoryName($image) -ine (Join-Path $runtimeRoot 'Scripts')) { throw 'Invalid dedicated image path' }
    $ifeo = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\' + $name
    $spe = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\SilentProcessExit\' + $name
    $flags = Get-ItemProperty -LiteralPath $ifeo
    $settings = Get-ItemProperty -LiteralPath $spe
    if (-not ($flags.GlobalFlag -band 0x200) -or $settings.ReportingMode -ne 2 -or
        $settings.IgnoreSelfExits -ne 0 -or $settings.LocalDumpFolder -ine $record.dump_folder -or
        $settings.DumpType -ne 0 -or $settings.MaxNumberOfDumpFiles -ne 8) {
        throw 'Windows settings differ from the validated native setup'
    }
}
$monitorScript = Join-Path $PSScriptRoot 'watch_processes.py'
if ($CheckOnly) {
    @{status='ready';state_directory=$StateDirectory;project=$record.project;
      target=$manifest.runtime;observer=$manifest.observer;dump_folder=$record.dump_folder;
      actual_interpreter_verified=$true;proxy_started=$false} | ConvertTo-Json
    exit 0
}
$existing = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='uv.exe' OR Name='workbuddy2api-evidence.exe' OR Name='workbuddy2api-runtime.exe'" |
    Where-Object { $_.CommandLine -match '(?:^|\s)-m\s+codebuddy_proxy(?:\s|$)' })
if ($existing.Count) { throw 'A proxy module process already exists; refusing to start another or stop it.' }
function Quote-NativeArgument([string]$value) {
    return '"' + (($value -replace '(\\*)"', '$1$1\"') -replace '(\\+)$', '$1$1') + '"'
}
$launchRoot = Join-Path $StateDirectory ('launch-' + [DateTime]::Now.ToString('yyyyMMdd-HHmmss-') + [guid]::NewGuid().ToString('N').Substring(0,8))
$null = New-Item -ItemType Directory -Path $launchRoot
$arguments = @($manifest.bootstrap,'--script',$monitorScript,'--output',(Join-Path $HOME '.workbuddy2api\monitor'),
    '--launch',$manifest.runtime,$manifest.bootstrap,'-m','codebuddy_proxy') + $ProxyArguments
$argumentLine = ($arguments | ForEach-Object { Quote-NativeArgument $_ }) -join ' '
$process = Start-Process -FilePath $manifest.observer -ArgumentList $argumentLine -WorkingDirectory $record.project `
    -WindowStyle Hidden -RedirectStandardOutput (Join-Path $launchRoot 'observer-stdout.log') `
    -RedirectStandardError (Join-Path $launchRoot 'observer-stderr.log') -PassThru
# Start is intentionally detached. This check confirms process creation, not health.
Start-Sleep -Seconds 2
if ($process.HasExited) { throw "Observer exited during startup; inspect $launchRoot" }
@{status='observer_started';observer_pid=$process.Id;launch_logs=$launchRoot;
  monitor_output=([string](Get-Content -LiteralPath (Join-Path $launchRoot 'observer-stdout.log') -Raw)).Trim();
  health_verified=$false;automatic_restart=$false} | ConvertTo-Json
