param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("stop", "restart")]
    [string]$Action,
    [switch]$Elevated
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root
$runtimeDir = Join-Path $root "tmp\dev-services"
$adminLog = Join-Path $runtimeDir "admin-action.log"
New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null

function Write-AdminLog {
    param([Parameter(Mandatory = $true)][string]$Message)
    # Multiple elevation wrappers can overlap briefly. Share the file handle
    # and retry so a transient Windows lock does not abort service control.
    for ($attempt = 1; $attempt -le 5; $attempt++) {
        $stream = $null
        $writer = $null
        try {
            $stream = [System.IO.FileStream]::new(
                $adminLog,
                [System.IO.FileMode]::OpenOrCreate,
                [System.IO.FileAccess]::Write,
                [System.IO.FileShare]::ReadWrite
            )
            $stream.Seek(0, [System.IO.SeekOrigin]::End) | Out-Null
            $writer = [System.IO.StreamWriter]::new($stream, [System.Text.UTF8Encoding]::new($false))
            $writer.WriteLine($Message)
            $writer.Flush()
            return
        }
        catch {
            if ($attempt -eq 5) { throw }
            Start-Sleep -Milliseconds (50 * $attempt)
        }
        finally {
            if ($writer) { $writer.Dispose() }
            elseif ($stream) { $stream.Dispose() }
        }
    }
}

$isAdministrator = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)

if (-not $Elevated -and -not $isAdministrator) {
    Write-AdminLog "Requesting administrator permission for service $Action."
    Write-Host "Requesting Windows administrator permission for service $Action..." -ForegroundColor Yellow
    $arguments = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", $PSCommandPath,
        "-Action", $Action,
        "-Elevated"
    )

    try {
        $startArgs = @{
            FilePath = "powershell.exe"
            ArgumentList = $arguments
            Verb = "RunAs"
            Wait = $true
            PassThru = $true
        }
        $process = Start-Process @startArgs
        exit $process.ExitCode
    }
    catch {
        Write-Host "Administrator permission was cancelled or unavailable: $($_.Exception.Message)" -ForegroundColor Red
        exit 1
    }
}

Write-Host "Running service $Action with administrator permission..." -ForegroundColor Cyan
$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Write-AdminLog "=== [$timestamp] elevated $Action ==="

function Test-LocalPortOpen {
    param([Parameter(Mandatory = $true)][int]$Port)
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $pending = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        if (-not $pending.AsyncWaitHandle.WaitOne(500)) {
            return $false
        }
        $client.EndConnect($pending)
        return $true
    }
    catch {
        return $false
    }
    finally {
        $client.Close()
    }
}

function Write-AdminOutput {
    param([object[]]$Lines)
    foreach ($line in $Lines) {
        $text = [string]$line
        Write-Host $text
        Write-AdminLog $text
    }
}

function Test-ConfirmedAdminTarget {
    param([object]$Original, [object]$Record)
    $pidValue = [int]$Original.ProcessId
    $currentProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$pidValue"
    if ($null -eq $currentProcess) { return $false }
    if (-not (Test-ProjectServiceProcess -Process $currentProcess -ProjectRoot $root -ManagedRecord $Record) -or
        ($Original.CreationDate -and $currentProcess.CreationDate -ne $Original.CreationDate)) {
        throw "refusing to stop PID ${pidValue}: process identity changed since inspection"
    }
    return $true
}

# A port or generic uvicorn/vite command is not project identity. Relative
# commands require a matching managed record; missing identity is preserved.
. (Join-Path $PSScriptRoot "dev_services_process_policy.ps1")
$stateFile = Join-Path $runtimeDir "state.json"
try {
    $managedRecords = @()
    if (Test-Path -LiteralPath $stateFile) {
        $managedState = Get-Content -LiteralPath $stateFile -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($serviceName in @("backend", "frontend")) {
            if ($managedState.$serviceName) { $managedRecords += $managedState.$serviceName }
        }
    }
    else {
        Write-AdminLog "No managed state. Only exact absolute project service commands may be selected."
    }
    $orphanProcesses = @(Get-CimInstance Win32_Process | Where-Object {
        $inspected = $_
        $matchingRecords = @($managedRecords | Where-Object { $_.pid -eq $inspected.ProcessId })
        $record = if ($matchingRecords.Count -eq 1) { $matchingRecords[0] } else { $null }
        Test-ProjectServiceProcess -Process $inspected -ProjectRoot $root -ManagedRecord $record
    })

    # Validate every occupied port before terminating anything. An unrelated or
    # unreadable owner is retained, even when its executable is python/node.
    $knownPids = @($orphanProcesses | ForEach-Object { [int]$_.ProcessId })
    $portOwners = @(Get-NetTCPConnection -State Listen -LocalPort 8000, 5173 -ErrorAction SilentlyContinue)
    foreach ($connection in $portOwners) {
        $ownerPid = [int]$connection.OwningProcess
        if ($knownPids -contains $ownerPid) {
            continue
        }
        throw "refusing to stop unconfirmed process PID $ownerPid on port $($connection.LocalPort); project identity could not be verified. Preserve it and use the normal dev_services.py stop command with a verified managed record."
    }
    foreach ($orphan in $orphanProcesses) {
        $pidValue = [int]$orphan.ProcessId
        $matchingRecords = @($managedRecords | Where-Object { $_.pid -eq $pidValue })
        $record = if ($matchingRecords.Count -eq 1) { $matchingRecords[0] } else { $null }
        if (-not (Test-ConfirmedAdminTarget -Original $orphan -Record $record)) { continue }
        $message = "Stopping stale project process PID $pidValue..."
        Write-Host $message -ForegroundColor Yellow
        Write-AdminLog $message
        $previousErrorAction = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        # Stop only the confirmed PID. Descendants need their own identity check.
        $killOutput = @(& taskkill.exe /PID $pidValue /F 2>&1)
        $killExitCode = $LASTEXITCODE
        $ErrorActionPreference = $previousErrorAction
        Write-AdminOutput -Lines $killOutput
        if ($killExitCode -ne 0) {
            $targetStillRunning = $null -ne (Get-Process -Id $pidValue -ErrorAction SilentlyContinue)
            if ($targetStillRunning) {
                if (-not (Test-ConfirmedAdminTarget -Original $orphan -Record $record)) { continue }
                $retryMessage = "PID termination was incomplete; retrying confirmed target PID $pidValue..."
                Write-Host $retryMessage -ForegroundColor Yellow
                Write-AdminLog $retryMessage
                $ErrorActionPreference = "Continue"
                $retryOutput = @(& taskkill.exe /PID $pidValue /F 2>&1)
                $retryExitCode = $LASTEXITCODE
                $ErrorActionPreference = $previousErrorAction
                Write-AdminOutput -Lines $retryOutput
                $targetStillRunning = $null -ne (Get-Process -Id $pidValue -ErrorAction SilentlyContinue)
                if ($retryExitCode -ne 0 -and $targetStillRunning) {
                    try {
                        if (-not (Test-ConfirmedAdminTarget -Original $orphan -Record $record)) { continue }
                        Stop-Process -Id $pidValue -Force -ErrorAction Stop
                        Start-Sleep -Milliseconds 250
                    }
                    catch {
                        $stopProcessMessage = "Stop-Process fallback failed for PID ${pidValue}: $($_.Exception.Message)"
                        Write-Host $stopProcessMessage -ForegroundColor Yellow
                        Write-AdminLog $stopProcessMessage
                    }
                    $targetStillRunning = $null -ne (Get-Process -Id $pidValue -ErrorAction SilentlyContinue)
                    if ($targetStillRunning) {
                        throw "taskkill and Stop-Process failed for PID $pidValue"
                    }
                }
            }
        }
    }
    if ($orphanProcesses) {
        Start-Sleep -Seconds 1
    }

    $deadline = (Get-Date).AddSeconds(10)
    while ((Test-LocalPortOpen -Port 8000) -or (Test-LocalPortOpen -Port 5173)) {
        if ((Get-Date) -ge $deadline) {
            throw "ports did not close cleanly (8000=$(Test-LocalPortOpen -Port 8000), 5173=$(Test-LocalPortOpen -Port 5173))"
        }
        Start-Sleep -Milliseconds 250
    }
}
catch {
    $message = "Could not inspect or clean stale project processes: $($_.Exception.Message)"
    Write-Host $message -ForegroundColor Red
    Write-AdminLog $message
    exit 1
}

if (Test-Path -LiteralPath $stateFile) {
    Remove-Item -LiteralPath $stateFile -Force
    Write-AdminLog "Removed stale service state."
}

$resultMessage = if ($Action -eq "restart") {
    "Administrator cleanup complete. Returning to normal user mode for startup."
} else {
    "Project services stopped; ports 8000 and 5173 are closed."
}
Write-Host $resultMessage -ForegroundColor Green
Write-AdminLog $resultMessage
exit 0
