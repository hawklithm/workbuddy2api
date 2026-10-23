param([Parameter(Mandatory=$true)][string]$Since,
      [Parameter(Mandatory=$true)][string]$Until,
      [string]$ProcessIds = '')
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$start = [DateTimeOffset]::Parse($Since).LocalDateTime
$end = [DateTimeOffset]::Parse($Until).LocalDateTime
$pidsToMatch = @($ProcessIds.Split(',') | Where-Object { $_ } | ForEach-Object { [long]$_ })
$channels = @('System', 'Application', 'Security', 'Microsoft-Windows-Sysmon/Operational')
$results = @()
function Read-WindowEvents($arguments) {
    try { Get-WinEvent @arguments -MaxEvents 201 -ErrorAction Stop }
    catch {
        if ($_.FullyQualifiedErrorId -notlike 'NoMatchingEventsFound*') { throw }
    }
}
foreach ($channel in $channels) {
    try {
        $null = Get-WinEvent -ListLog $channel -ErrorAction Stop
        $filter = @{LogName=$channel; StartTime=$start; EndTime=$end}
        if ($channel -eq 'Security') { $filter.Id = @(4688,4689) }
        elseif ($channel -like '*Sysmon*') { $filter.Id = @(1,5) }
        else { $filter.Level = @(1,2,3) }
        # Filter target IDs BEFORE the cap; busy audit logs must not crowd out our processes.
        $items = @()
        if ($channel -eq 'Security' -or $channel -like '*Sysmon*') {
            if ($pidsToMatch.Count -eq 0) {
                $results += @{channel=$channel; status='readable'; truncated=$false; events=@()}
                continue
            }
            $idFilter = if ($channel -eq 'Security') { '(EventID=4688 or EventID=4689)' } else { '(EventID=1 or EventID=5)' }
            $from = $start.ToUniversalTime().ToString('o')
            $to = $end.ToUniversalTime().ToString('o')
            # Small batches stay under Windows Event Log XPath expression limits.
            for ($offset = 0; $offset -lt $pidsToMatch.Count; $offset += 4) {
                $pidFilters = @()
                $last = [Math]::Min($offset + 3, $pidsToMatch.Count - 1)
                foreach ($targetId in $pidsToMatch[$offset..$last]) {
                    $fields = if ($channel -eq 'Security') { @('ProcessId','NewProcessId') } else { @('ProcessId') }
                    $text = if ($channel -eq 'Security') { '0x{0:x}' -f $targetId } else { [string]$targetId }
                    foreach ($field in $fields) {
                        $pidFilters += "Data[@Name='$field']='$text'"
                    }
                }
                $query = "*[System[$idFilter and TimeCreated[@SystemTime >= '$from' and @SystemTime <= '$to']]] and *[EventData[$($pidFilters -join ' or ')]]"
                $items += @(Read-WindowEvents @{LogName=$channel; FilterXPath=$query})
            }
            $items = @($items | Sort-Object RecordId -Descending -Unique)
        } else {
            $items = @(Read-WindowEvents @{FilterHashtable=$filter})
            if ($channel -eq 'Application') {
                # Silent Process Exit reports are informational/classic events, not errors.
                $exitFilter = @{LogName=$channel; ProviderName='Microsoft-Windows-ProcessExitMonitor'; StartTime=$start; EndTime=$end}
                $items += @(Read-WindowEvents @{FilterHashtable=$exitFilter})
                $items = @($items | Sort-Object RecordId -Descending -Unique)
            }
        }
        $rows = @()
        foreach ($item in ($items | Select-Object -First 200)) {
            $xml = [xml]$item.ToXml()
            $data = @{}
            foreach ($node in $xml.Event.EventData.Data) {
                if ($node.Name -in @('ProcessId','ProcessID','NewProcessId','CreatorProcessId',
                    'ParentProcessId','ExitStatus','Status','Image','ParentImage','ProcessName',
                    'NewProcessName','SourceProcessId','TargetProcessId','ProcessGuid','ParentProcessGuid',
                    'AppName','AppVersion','ModuleName','ModuleVersion','ExceptionCode','FaultingOffset',
                    'AppPath','ModulePath','ReportId')) {
                    $data[$node.Name] = [string]$node.'#text'
                }
            }
            if ($item.ProviderName -eq 'Microsoft-Windows-ProcessExitMonitor') {
                # Only this provider's documented image/status/creation-time parameters.
                $values = @($item.Properties | ForEach-Object { [string]$_.Value })
                $data['report_type'] = if (($item.Id -band 65535) -eq 3001) { 'cross_process' } else { 'self_exit' }
                $data['parameters'] = $values
            }
            if ($channel -eq 'Security' -or $channel -like '*Sysmon*') {
                $matchesTarget = $false
                foreach ($key in @('ProcessId','ProcessID','NewProcessId')) {
                    if ($data.ContainsKey($key)) {
                        try {
                            $value = $data[$key]
                            $number = if ($value.StartsWith('0x')) { [Convert]::ToInt64($value.Substring(2),16) } else { [long]$value }
                            if ($number -in $pidsToMatch) { $matchesTarget = $true }
                        } catch { }
                    }
                }
                if (-not $matchesTarget) { continue }
            }
            $rows += @{record_id=$item.RecordId; id=$item.Id; provider=$item.ProviderName;
                timestamp=$item.TimeCreated.ToUniversalTime().ToString('o'); level=$item.Level; data=$data}
        }
        $results += @{channel=$channel; status='readable'; truncated=($items.Count -gt 200); events=$rows}
    } catch {
        $results += @{channel=$channel; status='unavailable'; error=$_.Exception.Message; events=@()}
    }
}
@{since=$Since; until=$Until; channels=$results; audit_settings_changed=$false} | ConvertTo-Json -Depth 8 -Compress
