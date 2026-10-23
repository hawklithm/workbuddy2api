#requires -Version 5.1
[CmdletBinding()]
param(
    [ValidateSet('ValidateAndEnable','Rollback','Status')][string]$Action = 'Status',
    [Parameter(Mandatory=$true)][string]$StateDirectory,
    [string]$TargetImage = 'D:\workspace\workbuddy2api\.venv\Scripts\python.exe',
    [string]$VenvConfig = 'D:\workspace\workbuddy2api\.venv\pyvenv.cfg'
)
$ErrorActionPreference = 'Stop'
$ifeoRoot = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options'
$speRoot = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\SilentProcessExit'
$filterName = 'Workbuddy2apiExitEvidence'
$provider = 'Microsoft-Windows-ProcessExitMonitor'
$statePath = [IO.Path]::GetFullPath($StateDirectory)
$journalPath = Join-Path $statePath 'production-registry-journal.json'
$resultPath = Join-Path $statePath 'result.json'
$script:ownedProcesses = New-Object System.Collections.Generic.List[System.Diagnostics.Process]

function Save-Json($path, $value) {
    $text = ConvertTo-Json -InputObject $value -Depth 12
    # Write replacement atomically so a partial write cannot destroy rollback state.
    $tempPath = $path + '.new'
    [IO.File]::WriteAllText($tempPath, $text, (New-Object Text.UTF8Encoding($false)))
    if (Test-Path -LiteralPath $path) { [IO.File]::Replace($tempPath, $path, ($path + '.previous')) }
    else { [IO.File]::Move($tempPath, $path) }
}

function Set-RecordedValue($path, $name, $value, $type, $changes, $backupPath) {
    $exists = Test-Path -LiteralPath $path
    $hadValue = $false
    $oldValue = $null
    $oldKind = $null
    if ($exists) {
        $key = Get-Item -LiteralPath $path
        $hadValue = $key.GetValueNames() -contains $name
        if ($hadValue) {
            $oldValue = $key.GetValue($name, $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
            $oldKind = [string]$key.GetValueKind($name)
        }
    }
    $changes.Add([pscustomobject]@{Path=$path; Name=$name; KeyExisted=$exists; HadValue=$hadValue;
        OldValue=$oldValue; OldKind=$oldKind; NewValue=$value; NewKind=$type})
    Save-Json $backupPath @($changes.ToArray())
    if (-not (Test-Path -LiteralPath $path)) { $null = New-Item -Path $path -Force }
    $null = New-ItemProperty -LiteralPath $path -Name $name -Value $value -PropertyType $type -Force
}

function Restore-RecordedValues($backupPath) {
    if (-not (Test-Path -LiteralPath $backupPath)) { return }
    # Windows PowerShell 5.1 emits a JSON array as one pipeline item. Assignment
    # avoids accidentally wrapping it in a second array and enumerating .Name.
    [object[]]$changes = Get-Content -LiteralPath $backupPath -Raw | ConvertFrom-Json
    $emptyCandidates = @()
    for ($index = $changes.Count - 1; $index -ge 0; $index--) {
        $entry = $changes[$index]
        # Restore only journaled values under the expected image-option registry roots.
        $allowed = $entry.Path.StartsWith($ifeoRoot + '\', [StringComparison]::OrdinalIgnoreCase) -or
                   $entry.Path.StartsWith($speRoot + '\', [StringComparison]::OrdinalIgnoreCase)
        if (-not $allowed) { throw "Rollback path outside expected registry roots: $($entry.Path)" }
        if (Test-Path -LiteralPath $entry.Path) {
            $key = Get-Item -LiteralPath $entry.Path
            $present = $key.GetValueNames() -contains $entry.Name
            if ($present) {
                $current = $key.GetValue($entry.Name, $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
                if ([string]$current -ne [string]$entry.NewValue) {
                    throw "Rollback conflict; value changed after setup: $($entry.Path) / $($entry.Name)"
                }
            }
            if ($entry.HadValue) {
                $null = New-ItemProperty -LiteralPath $entry.Path -Name $entry.Name -Value $entry.OldValue -PropertyType $entry.OldKind -Force
            } elseif ($present) {
                Remove-ItemProperty -LiteralPath $entry.Path -Name $entry.Name
            }
        } elseif ($entry.HadValue) {
            throw "Rollback conflict; original key was removed: $($entry.Path)"
        }
        if (-not $entry.KeyExisted) { $emptyCandidates += $entry.Path }
    }
    foreach ($path in ($emptyCandidates | Select-Object -Unique | Sort-Object { $_.Length } -Descending)) {
        if (Test-Path -LiteralPath $path) {
            $key = Get-Item -LiteralPath $path
            if ($key.GetValueNames().Count -eq 0 -and $key.GetSubKeyNames().Count -eq 0) {
                # No recursive registry deletion; never remove another tool's values/subkeys.
                Remove-Item -LiteralPath $path
            }
        }
    }
}

function Set-Scope($imagePath, $dumpFolder, $changes, $backupPath) {
    $imageName = [IO.Path]::GetFileName($imagePath)
    if ($imageName -notin @('workbuddy-spe-probe.exe','workbuddy2api-evidence.exe')) {
        throw 'Settings are restricted to our dedicated diagnostic image names; python.exe is never configured'
    }
    $baseKey = Join-Path $ifeoRoot $imageName
    $filterKey = Join-Path $baseKey $filterName
    $speKey = Join-Path $speRoot $imageName
    if (Test-Path -LiteralPath $filterKey) { throw "Named filter already exists; refuse overwrite: $filterKey" }
    if (Test-Path -LiteralPath $speKey) {
        $old = Get-Item -LiteralPath $speKey
        if ($old.GetValueNames().Count -or $old.GetSubKeyNames().Count) {
            throw "Existing silent-exit configuration requires manual review: $speKey"
        }
    }
    $globalFlags = 0
    if (Test-Path -LiteralPath $baseKey) {
        $key = Get-Item -LiteralPath $baseKey
        if ($key.GetValueNames() -contains 'GlobalFlag') {
            if ($key.GetValueKind('GlobalFlag') -ne [Microsoft.Win32.RegistryValueKind]::DWord) {
                throw 'Existing non-DWORD GlobalFlag requires manual review'
            }
            $globalFlags = [int]$key.GetValue('GlobalFlag')
            if ($globalFlags -band 0x200) { throw 'Image already has global silent-exit monitoring; refuse broadened scope' }
        }
    }
    $null = New-Item -ItemType Directory -Path $dumpFolder -Force
    Set-RecordedValue $baseKey 'GlobalFlag' ($globalFlags -bor 0x200) 'DWord' $changes $backupPath
    Set-RecordedValue $speKey 'ReportingMode' 2 'DWord' $changes $backupPath
    Set-RecordedValue $speKey 'IgnoreSelfExits' 0 'DWord' $changes $backupPath
    Set-RecordedValue $speKey 'LocalDumpFolder' $dumpFolder 'String' $changes $backupPath
    Set-RecordedValue $speKey 'DumpType' 0 'DWord' $changes $backupPath
    Set-RecordedValue $speKey 'MaxNumberOfDumpFiles' 8 'DWord' $changes $backupPath
}

function New-Probe($executable, $code) {
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = $executable
    $info.Arguments = '-c "' + $code + '"'
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.WorkingDirectory = Split-Path -Parent $executable
    $process = [Diagnostics.Process]::Start($info)
    $script:ownedProcesses.Add($process)
    return $process
}

function Read-ProbeEvents($since) {
    try {
        return @(Get-WinEvent -FilterHashtable @{LogName='Application';ProviderName=$provider;StartTime=$since} -MaxEvents 100 -ErrorAction Stop |
            Where-Object { (@($_.Properties | ForEach-Object { [string]$_.Value }) -join ' ') -match 'workbuddy-spe-(probe|control)\.exe' })
    } catch {
        if ($_.FullyQualifiedErrorId -like 'NoMatchingEventsFound*') { return @() }
        throw
    }
}

function Wait-ProbeEvent($since, $eventId) {
    $deadline = [DateTime]::UtcNow.AddSeconds(25)
    while ([DateTime]::UtcNow -lt $deadline) {
        $rows = @(Read-ProbeEvents $since | Where-Object { ($_.Id -band 65535) -eq $eventId })
        if ($rows.Count) { return $rows }
        Start-Sleep -Milliseconds 250
    }
    throw "No expected silent-exit event $eventId; validation failed"
}

function Protect-StateDirectory {
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl = New-Object Security.AccessControl.DirectorySecurity
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($identity in @($sid, [Security.Principal.SecurityIdentifier]'S-1-5-18', [Security.Principal.SecurityIdentifier]'S-1-5-32-544')) {
        $rule = New-Object Security.AccessControl.FileSystemAccessRule($identity, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $statePath -AclObject $acl
}

if ($Action -eq 'Status') {
    $rollbackResult = Join-Path $statePath 'rollback-result.json'
    if (Test-Path -LiteralPath $rollbackResult) { Get-Content -LiteralPath $rollbackResult -Raw }
    elseif (Test-Path -LiteralPath $resultPath) { Get-Content -LiteralPath $resultPath -Raw }
    else { Write-Output 'No result file; configuration has not been confirmed.' }
    exit 0
}

$principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Administrator token required. Run this script through an explicit Windows UAC prompt.'
}
if (-not [Environment]::Is64BitProcess) { throw 'Use 64-bit Windows PowerShell for the 64-bit registry view' }

if ($Action -eq 'Rollback') {
    Restore-RecordedValues $journalPath
    Restore-RecordedValues (Join-Path $statePath 'test-registry-journal.json')
    Save-Json (Join-Path $statePath 'rollback-result.json') @{status='rolled_back'; timestamp=[DateTimeOffset]::Now.ToString('o')}
    exit 0
}
if (Test-Path -LiteralPath $statePath) { throw 'Use a fresh state directory; refusing to overwrite prior rollback evidence' }
$null = New-Item -ItemType Directory -Path $statePath
Protect-StateDirectory
Start-Transcript -LiteralPath (Join-Path $statePath 'setup-transcript.txt') | Out-Null
$testBackup = Join-Path $statePath 'test-registry-journal.json'
$testChanges = New-Object System.Collections.Generic.List[object]
$productionChanges = New-Object System.Collections.Generic.List[object]
$testRestored = $false
try {
    $target = (Get-Item -LiteralPath $TargetImage).FullName
    if ([IO.Path]::GetFileName($target) -ine 'python.exe') { throw 'Target must be the explicitly selected venv python.exe' }
    $config = (Get-Item -LiteralPath $VenvConfig).FullName
    $testDirectory = Join-Path $statePath 'temporary-test'
    $monitoredDirectory = Join-Path $testDirectory 'monitored'
    $controlDirectory = Join-Path $testDirectory 'control'
    foreach ($directory in @($monitoredDirectory,$controlDirectory)) {
        $null = New-Item -ItemType Directory -Path $directory -Force
        Copy-Item -LiteralPath $target -Destination (Join-Path $directory 'workbuddy-spe-probe.exe')
        Copy-Item -LiteralPath $config -Destination (Join-Path $directory 'pyvenv.cfg')
    }
    $probe = Join-Path $monitoredDirectory 'workbuddy-spe-probe.exe'
    $control = Join-Path $controlDirectory 'workbuddy-spe-control.exe'
    Move-Item -LiteralPath (Join-Path $controlDirectory 'workbuddy-spe-probe.exe') -Destination $control
    Set-Scope $probe (Join-Path $statePath 'test-dumps') $testChanges $testBackup

    # Only our unique diagnostic image name is configured; original python.exe is untouched.
    $controlStart = Get-Date
    $process = New-Probe $control 'raise SystemExit(17)'
    if (-not $process.WaitForExit(15000)) { throw 'Temporary control process timed out' }
    if ($process.ExitCode -ne 17) { throw 'Renamed temporary launcher did not execute the control workload' }
    Start-Sleep -Seconds 3
    if (@(Read-ProbeEvents $controlStart).Count) { throw 'Unexpected diagnostic image event during control workload' }

    $selfStart = Get-Date
    $process = New-Probe $probe 'raise SystemExit(23)'
    if (-not $process.WaitForExit(15000)) { throw 'Temporary self-exit process timed out' }
    $selfEvents = @(Wait-ProbeEvent $selfStart 3000)
    Save-Json (Join-Path $statePath 'self-exit-events.json') @($selfEvents | ForEach-Object {
        @{id=$_.Id;record_id=$_.RecordId;timestamp=$_.TimeCreated.ToString('o');message=$_.Message;xml=$_.ToXml()}
    })

    $ready = Join-Path $testDirectory 'ready.txt'
    # Use a Python raw string; path contains no user-controlled shell substitutions.
    if ($ready.Contains("'")) { throw 'State directory cannot contain a single quote for the isolated readiness test' }
    $code = "import os,time; from pathlib import Path; Path(r'$ready').write_text(str(os.getpid())); time.sleep(20)"
    $killStart = Get-Date
    $process = New-Probe $probe $code
    $readyDeadline = [DateTime]::UtcNow.AddSeconds(10)
    while (-not (Test-Path -LiteralPath $ready) -and [DateTime]::UtcNow -lt $readyDeadline) { Start-Sleep -Milliseconds 100 }
    if (-not (Test-Path -LiteralPath $ready)) { throw 'Temporary cross-process workload did not become ready' }
    $killedTestPid = $process.Id
    $process.Kill() # ONLY this exact Process object created by New-Probe, never any proxy.
    if (-not $process.WaitForExit(15000)) { throw 'Temporary termination did not complete' }
    $crossEvents = @(Wait-ProbeEvent $killStart 3001)
    if (-not (@($crossEvents | ForEach-Object { $_.Message }) -join ' ').Contains('powershell.exe')) {
        throw 'Cross-process event did not identify the known PowerShell initiator'
    }
    Save-Json (Join-Path $statePath 'cross-process-events.json') @($crossEvents | ForEach-Object {
        @{id=$_.Id;record_id=$_.RecordId;timestamp=$_.TimeCreated.ToString('o');message=$_.Message;xml=$_.ToXml()}
    })
    $dumpDeadline = [DateTime]::UtcNow.AddSeconds(25)
    do {
        # Windows groups each exit's victim/initiator dumps in a per-incident subfolder.
        $dumps = @(Get-ChildItem -LiteralPath (Join-Path $statePath 'test-dumps') -Recurse -Filter '*.dmp' -File)
        $initiatorDumps = @($dumps | Where-Object { $_.Name -like '*powershell*' })
        if ($initiatorDumps.Count) { break }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $dumpDeadline)
    if (-not $dumps.Count) { throw 'No test dumps were produced; refusing production configuration' }
    if (-not @($dumps | Where-Object { $_.Name -like '*powershell*' }).Count) {
        throw 'No initiator dump found; refusing production configuration'
    }
    Restore-RecordedValues $testBackup
    $testRestored = $true
    $diagnosticImage = Join-Path (Split-Path -Parent $target) 'workbuddy2api-evidence.exe'
    if (-not (Test-Path -LiteralPath $diagnosticImage)) {
        Copy-Item -LiteralPath $target -Destination $diagnosticImage
    }
    $sourceHash = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash
    if ((Get-FileHash -LiteralPath $diagnosticImage -Algorithm SHA256).Hash -ne $sourceHash) {
        throw 'Existing diagnostic image differs from the original venv launcher; refuse overwrite'
    }
    # Keep the copy adjacent to the original so the same pyvenv.cfg and site-packages apply.
    Set-Scope $diagnosticImage (Join-Path $statePath 'dumps') $productionChanges $journalPath
    Save-Json $resultPath @{status='enabled_for_future_starts';timestamp=[DateTimeOffset]::Now.ToString('o');
        target=$diagnosticImage;original_target=$target;launcher_sha256=$sourceHash;
        scope='dedicated diagnostic image name; original python.exe not configured';dump_folder=(Join-Path $statePath 'dumps');
        control_image_ignored=$true;self_exit_verified=$true;external_initiator_verified=$true;
        test_initiator_pid=$PID;terminated_temporary_launcher_pid=$killedTestPid;
        test_dumps=@($dumps | Select-Object Name,Length);target_processes_stopped=$false;
        current_running_processes_reconfigured=$false;reporting_mode=2;dump_type=0;max_dump_files=8}
} catch {
    $failure = $_.Exception.Message
    $restoreErrors = @()
    try { Restore-RecordedValues $journalPath } catch { $restoreErrors += $_.Exception.Message }
    if (-not $testRestored) {
        try { Restore-RecordedValues $testBackup } catch { $restoreErrors += $_.Exception.Message }
    }
    Save-Json $resultPath @{status='failed';error=$failure;rollback_errors=$restoreErrors;timestamp=[DateTimeOffset]::Now.ToString('o')}
    throw
} finally {
    foreach ($process in $script:ownedProcesses) {
        if (-not $process.HasExited) { $process.Kill(); $process.WaitForExit(10000) | Out-Null }
        $process.Dispose()
    }
    Stop-Transcript | Out-Null
}
