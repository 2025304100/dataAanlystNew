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

# State can be missing after an interrupted stop. Clean the exact project
# process commands first, then verify both managed ports are really released.
try {
    $orphanProcesses = @(Get-CimInstance Win32_Process | Where-Object {
        $commandLine = [string]$_.CommandLine
        $isBackend = $_.Name -ieq "python.exe" -and
            $commandLine -match "-m\s+uvicorn\s+app\.main:app" -and
            $commandLine -match "--port\s+8000"
        $isVite = $_.Name -ieq "node.exe" -and
            $commandLine -match "node_modules[\\/]vite[\\/]bin[\\/]vite\.js" -and
            $commandLine -match "vite\.codex\.config\.ts" -and
            $commandLine -match "--port\s+5173"
        $isStandalone = $_.Name -ieq "node.exe" -and
            $commandLine -match "universe-standalone-server\.mjs"
        $isBackend -or $isVite -or $isStandalone
    })

    # Some elevated/detached Windows processes expose an empty CommandLine.
    # Fall back only for the dedicated project ports and expected executable type.
    $knownPids = @($orphanProcesses | ForEach-Object { [int]$_.ProcessId })
    $portOwners = @(Get-NetTCPConnection -State Listen -LocalPort 8000, 5173 -ErrorAction SilentlyContinue)
    foreach ($connection in $portOwners) {
        $ownerPid = [int]$connection.OwningProcess
        if ($knownPids -contains $ownerPid) {
            continue
        }
        $ownerProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$ownerPid"
        if ($null -eq $ownerProcess) {
            throw "could not inspect process $ownerPid on port $($connection.LocalPort)"
        }
        $isExpectedOwner =
            ($connection.LocalPort -eq 5173 -and $ownerProcess.Name -ieq "node.exe") -or
            ($connection.LocalPort -eq 8000 -and $ownerProcess.Name -ieq "python.exe")
        if (-not $isExpectedOwner) {
            throw "refusing to stop unexpected $($ownerProcess.Name) process $ownerPid on port $($connection.LocalPort)"
        }
        $orphanProcesses += $ownerProcess
        $knownPids += $ownerPid
    }
    foreach ($orphan in $orphanProcesses) {
        $pidValue = [int]$orphan.ProcessId
        $message = "Stopping stale project process PID $pidValue..."
        Write-Host $message -ForegroundColor Yellow
        Write-AdminLog $message
        $previousErrorAction = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        $killOutput = @(& taskkill.exe /PID $pidValue /T /F 2>&1)
        $killExitCode = $LASTEXITCODE
        $ErrorActionPreference = $previousErrorAction
        Write-AdminOutput -Lines $killOutput
        if ($killExitCode -ne 0) {
            $targetStillRunning = $null -ne (Get-Process -Id $pidValue -ErrorAction SilentlyContinue)
            if ($targetStillRunning) {
                $retryMessage = "Process-tree termination was incomplete; retrying target PID $pidValue directly..."
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

$stateFile = Join-Path $runtimeDir "state.json"
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
