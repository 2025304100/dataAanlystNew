$ErrorActionPreference = 'Stop'
$repairRoot = (Resolve-Path -LiteralPath 'D:\ai_project\dataAanlystNew').Path
$statePath = Join-Path $repairRoot 'tmp\dev-services\state.json'
$serviceState = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
$backendProcessId = [int]$serviceState.backend.pid
$backendProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$backendProcessId"
if (!$backendProcess -or $backendProcess.CommandLine -notmatch '-m uvicorn app\.main:app --host 127\.0\.0\.1 --port 8000') { throw 'Managed backend identity changed; no process was stopped.' }
$listener = Get-NetTCPConnection -LocalPort 8000 -State Listen | Select-Object -First 1
if ($listener.OwningProcess -ne $backendProcessId) { throw 'Port owner differs from the managed backend; no process was stopped.' }
& 'C:\Python312\python.exe' (Join-Path $repairRoot 'tmp\inspection-etf-repair-2026-10-24\check_idle.py')
if ($LASTEXITCODE -ne 0) { throw 'Idle/schema check failed; no process was stopped.' }
Stop-Process -Id $backendProcessId -ErrorAction Stop
for ($attempt = 0; $attempt -lt 20; $attempt++) {
  if (!(Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue)) { break }
  Start-Sleep -Milliseconds 250
}
if (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue) { throw 'Backend port did not close.' }
$stdoutPath = Join-Path $repairRoot 'tmp\inspection-etf-repair-2026-10-24\backend.out.log'
$stderrPath = Join-Path $repairRoot 'tmp\inspection-etf-repair-2026-10-24\backend.err.log'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$started = Start-Process -FilePath 'C:\Python312\python.exe' -ArgumentList @('scripts/serve_inspection_acceptance.py','--no-background-jobs') -WorkingDirectory $repairRoot -WindowStyle Hidden -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -PassThru
$ready = $false
for ($attempt = 0; $attempt -lt 50; $attempt++) {
  if ($started.HasExited) { throw 'Controlled backend exited; inspect the saved stderr log.' }
  try {
    $status = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/v1/inspection-acceptance/status' -Method Get -TimeoutSec 1
    if ($status.mode -eq 'inspection_acceptance' -and !$status.background_jobs_enabled -and !$status.automatic_trading_enabled -and !$status.trading_mutations_allowed -and !$status.final_reviews_allowed) { $ready = $true; break }
  } catch { }
  Start-Sleep -Milliseconds 400
}
if (!$ready) { throw 'Controlled backend did not become ready; ordinary startup was not retried.' }
$serviceState.backend.pid = $started.Id
$serviceState.backend.mode = 'inspection_acceptance'
$serviceState.backend.command = @('C:\Python312\python.exe','scripts/serve_inspection_acceptance.py','--no-background-jobs')
$serviceState.backend.stdout_log = $stdoutPath
$serviceState.backend.stderr_log = $stderrPath
$serviceState.backend.started_at = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
[System.IO.File]::WriteAllText($statePath, ($serviceState | ConvertTo-Json -Depth 8), [System.Text.UTF8Encoding]::new($false))
[pscustomobject]@{old_backend_pid=$backendProcessId;new_backend_pid=$started.Id;status=$status;frontend_unchanged=$serviceState.frontend.pid} | ConvertTo-Json -Depth 5
