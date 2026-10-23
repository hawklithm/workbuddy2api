#requires -Version 5.1
[CmdletBinding()]
param(
    [ValidateSet('ValidateAndEnable','Rollback','Status')][string]$Action = 'Status',
    [Parameter(Mandatory=$true)][string]$StateDirectory,
    [string]$Venv = 'D:\workspace\workbuddy2api\.venv'
)
$ErrorActionPreference = 'Stop'
$statePath = [IO.Path]::GetFullPath($StateDirectory)
$journalPath = Join-Path $statePath 'registry-journal.json'
$resultPath = Join-Path $statePath 'result.json'
$ifeoRoot = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options'
$speRoot = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\SilentProcessExit'
$filterName = 'Workbuddy2apiExitEvidence'
$provider = 'Microsoft-Windows-ProcessExitMonitor'
# Reuse the reviewed journal/rollback functions without executing the legacy setup.
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot 'manage_silent_exit.ps1'),[ref]$tokens,[ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
foreach ($definition in $ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst]},$true)) {
    . ([scriptblock]::Create($definition.Extent.Text))
}
if ($Action -eq 'Status') {
    if (Test-Path (Join-Path $statePath 'rollback-result.json')) { Get-Content (Join-Path $statePath 'rollback-result.json') -Raw }
    elseif (Test-Path $resultPath) { Get-Content $resultPath -Raw }
    else { Write-Output 'No validated setup.' }
    exit 0
}
$principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator) -or -not [Environment]::Is64BitProcess) {
    throw '64-bit administrator PowerShell required for the dedicated image configuration'
}
$testJournal = Join-Path $statePath 'test-registry-journal.json'
if ($Action -eq 'Rollback') {
    Restore-RecordedValues $journalPath
    Restore-RecordedValues $testJournal
    Save-Json (Join-Path $statePath 'rollback-result.json') @{status='rolled_back';timestamp=[DateTimeOffset]::Now.ToString('o')}
    exit 0
}
if (Test-Path -LiteralPath $statePath) { throw 'Use a fresh state directory; refusing to overwrite prior evidence' }
$null = New-Item -ItemType Directory -Path $statePath
Protect-StateDirectory
Start-Transcript -LiteralPath (Join-Path $statePath 'setup-transcript.txt') | Out-Null
$owned = New-Object System.Collections.Generic.List[System.Diagnostics.Process]
$testChanges = New-Object System.Collections.Generic.List[object]
$productionChanges = New-Object System.Collections.Generic.List[object]

function Start-NativeTest($image, $mode, $payload) {
    if ($payload.Contains('"')) { throw 'Test payload must use single quotes internally' }
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = $image
    $info.Arguments = '"' + $manifest.bootstrap + '" ' + $mode + ' "' + $payload + '"'
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $process = [Diagnostics.Process]::Start($info)
    $owned.Add($process)
    return $process
}
function Wait-NativeEvent($since, $id, $image, $expectedCode) {
    $deadline = [DateTime]::UtcNow.AddSeconds(25)
    do {
        $rows = @()
        try {
            $rows = @(Get-WinEvent -FilterHashtable @{LogName='Application';ProviderName=$provider;StartTime=$since} -MaxEvents 100 -ErrorAction Stop |
                Where-Object { ($_.Id -band 65535) -eq $id -and
                    $_.Properties[0].Value -ieq $image -and
                    [string]$_.Properties[$(if ($id -eq 3000) {1} else {2})].Value -eq [string]$expectedCode })
        } catch { if ($_.FullyQualifiedErrorId -notlike 'NoMatchingEventsFound*') { throw } }
        if ($rows.Count) { return $rows }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "No native event $id for the exact temporary image"
}
try {
    $venvPath = (Get-Item -LiteralPath $Venv).FullName
    $original = Join-Path $venvPath 'Scripts\python.exe'
    $runtimeRoot = Join-Path $statePath 'runtime'
    & $original (Join-Path $PSScriptRoot 'prepare_native_runtime.py') $runtimeRoot $venvPath
    if ($LASTEXITCODE -ne 0) { throw 'Dedicated runtime preparation failed' }
    $manifest = Get-Content (Join-Path $runtimeRoot 'manifest.json') -Raw | ConvertFrom-Json
    # Both executables must prove that OS PID and image are the actual workload.
    $identities = @()
    foreach ($image in @($manifest.runtime,$manifest.observer)) {
        $identityPath = Join-Path $statePath (([IO.Path]::GetFileName($image)) + '.identity.json')
        $process = Start-NativeTest $image '--identity' $identityPath
        $completed = $process.WaitForExit(15000)
        if (-not $completed -or $process.ExitCode -ne 0 -or -not (Test-Path $identityPath)) {
            $diagnostic = @{image=$image;pid=$process.Id;completed=$completed;identity_exists=(Test-Path $identityPath)}
            if ($completed) {
                $diagnostic.exit_code=$process.ExitCode
                $diagnostic.stderr=$process.StandardError.ReadToEnd()
                $diagnostic.stdout=$process.StandardOutput.ReadToEnd()
            }
            Save-Json (Join-Path $statePath 'identity-failure.json') $diagnostic
            throw 'Dedicated interpreter import/identity check failed; see identity-failure.json'
        }
        $identity = Get-Content $identityPath -Raw | ConvertFrom-Json
        if ($identity.pid -ne $process.Id -or $identity.image -ine $image -or $identity.executable -ine $image) {
            throw 'Workload is running in a redirector child; refusing configuration'
        }
        $expectedProxy = Join-Path (Split-Path -Parent $venvPath) 'src\codebuddy_proxy\__main__.py'
        if ($identity.proxy -ine $expectedProxy -or $identity.base_prefix -ine $manifest.base) {
            throw 'Dedicated interpreter resolved a different project or base Python'
        }
        $identities += $identity
    }
    Set-Scope $manifest.probe (Join-Path $statePath 'test-dumps') $testChanges $testJournal
    $reports = @()
    foreach ($test in @(@{name='normal';code='raise SystemExit(23)';exit=23},
                        @{name='abrupt_self';code='import os; os._exit(29)';exit=29})) {
        $since = Get-Date
        $process = Start-NativeTest $manifest.probe '-c' $test.code
        if (-not $process.WaitForExit(15000) -or $process.ExitCode -ne $test.exit) { throw 'Temporary self-exit test failed' }
        $events = @(Wait-NativeEvent $since 3000 $manifest.probe $test.exit)
        $reports += @{case=$test.name;pid=$process.Id;events=@($events | ForEach-Object { @{id=$_.Id;record_id=$_.RecordId;xml=$_.ToXml();message=$_.Message} })}
    }
    $ready = Join-Path $statePath 'temporary-ready.json'
    if ($ready.Contains("'")) { throw 'Test state path cannot contain single quotes' }
    $code = "import os,time,json; from pathlib import Path; Path(r'$ready').write_text(json.dumps(dict(pid=os.getpid()))); time.sleep(45)"
    $since = Get-Date
    $process = Start-NativeTest $manifest.probe '-c' $code
    $deadline = [DateTime]::UtcNow.AddSeconds(15)
    while (-not (Test-Path $ready) -and [DateTime]::UtcNow -lt $deadline) { Start-Sleep -Milliseconds 100 }
    if (-not (Test-Path $ready) -or (Get-Content $ready -Raw | ConvertFrom-Json).pid -ne $process.Id) {
        throw 'Temporary workload PID does not match the exact owned process'
    }
    $killedPid = $process.Id
    $process.Kill() # Only this test Process object; never search or kill by name/PID.
    if (-not $process.WaitForExit(15000)) { throw 'Temporary termination timed out' }
    $events = @(Wait-NativeEvent $since 3001 $manifest.probe -1)
    if (-not (@($events | ForEach-Object {$_.Message}) -join ' ').Contains('powershell.exe')) {
        throw 'Cross-process report did not identify the known test PowerShell initiator'
    }
    $reports += @{case='external_termination';pid=$killedPid;initiator_pid=$PID;
        events=@($events | ForEach-Object { @{id=$_.Id;record_id=$_.RecordId;xml=$_.ToXml();message=$_.Message} })}
    Save-Json (Join-Path $statePath 'validation-events.json') $reports
    $deadline = [DateTime]::UtcNow.AddSeconds(25)
    do {
        $dumps = @(Get-ChildItem (Join-Path $statePath 'test-dumps') -Recurse -File -Filter '*.dmp')
        $victim = @($dumps | Where-Object {$_.Name -like "*probe.exe-(PID-$killedPid).*"})
        $initiator = @($dumps | Where-Object {$_.Name -like "*powershell.exe-(PID-$PID).*"})
        if ($victim.Count -and $initiator.Count) { break }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    if (-not $victim.Count -or -not $initiator.Count) { throw 'Exact temporary victim/initiator dumps missing' }
    Restore-RecordedValues $testJournal
    foreach ($image in @($manifest.runtime,$manifest.observer)) {
        Set-Scope $image (Join-Path $statePath 'dumps') $productionChanges $journalPath
    }
    $productionReports = @()
    foreach ($image in @($manifest.runtime,$manifest.observer)) {
        $since = Get-Date
        $identityPath = Join-Path $statePath (([IO.Path]::GetFileName($image)) + '.monitored-identity.json')
        $process = Start-NativeTest $image '--identity' $identityPath
        if (-not $process.WaitForExit(15000) -or $process.ExitCode -ne 0) { throw 'Configured image import test failed' }
        if ((Get-Content $identityPath -Raw | ConvertFrom-Json).pid -ne $process.Id) { throw 'Configured workload PID mismatch' }
        $events = @(Wait-NativeEvent $since 3000 $image 0)
        $productionReports += @{image=$image;temporary_pid=$process.Id;
            events=@($events | ForEach-Object { @{id=$_.Id;record_id=$_.RecordId;xml=$_.ToXml();message=$_.Message} })}
    }
    Save-Json (Join-Path $statePath 'production-image-validation.json') $productionReports
    Save-Json $resultPath @{status='native_runtime_enabled';timestamp=[DateTimeOffset]::Now.ToString('o');
        runtime_root=$runtimeRoot;venv=$venvPath;project=(Split-Path -Parent $venvPath);
        identities=$identities;dump_folder=(Join-Path $statePath 'dumps');
        normal_exit_verified=$true;abrupt_self_exit_verified=$true;external_initiator_verified=$true;
        terminated_temporary_pid=$killedPid;test_initiator_pid=$PID;running_proxy_changed=$false;
        shared_python_configured=$false;both_configured_images_verified=$true}
} catch {
    $failure = $_.Exception.Message
    $rollbackErrors = @()
    try { Restore-RecordedValues $journalPath } catch { $rollbackErrors += $_.Exception.Message }
    try { Restore-RecordedValues $testJournal } catch { $rollbackErrors += $_.Exception.Message }
    Save-Json $resultPath @{status='failed';error=$failure;rollback_errors=$rollbackErrors}
    throw
} finally {
    foreach ($process in $owned) {
        if (-not $process.HasExited) { $process.Kill(); $null=$process.WaitForExit(10000) }
        $process.Dispose()
    }
    Stop-Transcript | Out-Null
}
