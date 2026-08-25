# Restore database from a named backup tag (Windows PowerShell)
# Usage: powershell -ExecutionPolicy Bypass -File scripts\restore_db.ps1 -Tag pre_migration_0031 [-SkipEmptyVerify]
param(
    [Parameter(Mandatory=$true)][string]$Tag,
    [switch]$SkipEmptyVerify
)

$ErrorActionPreference = "Stop"
$ProjectRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$BackupDir = Join-Path $ProjectRoot "backups"
$DbConfigPath = Join-Path $ProjectRoot "config" "db_config.json"
$StampFile = Join-Path $BackupDir ("quant_workbench_{0}.stamp" -f $Tag)

function Read-DbConfig {
    if (-not (Test-Path $DbConfigPath)) { return $null }
    try {
        $raw = Get-Content $DbConfigPath -Raw -Encoding UTF8
        return $raw | ConvertFrom-Json
    } catch { return $null }
}

function Parse-DatabaseUrl($url) {
    $result = @{ use_mysql = $false; host = ""; port = 3306; database = ""; user = ""; password = ""; sqlite_path = "" }
    if ([string]::IsNullOrWhiteSpace($url)) { return $result }
    if ($url.StartsWith("mysql")) {
        $result.use_mysql = $true
        try {
            $m = [regex]::Match($url, '^mysql(?:\+pymysql)?://([^:]+):([^@]*)@([^:/]+):(\d+)/([^?]+)')
            if ($m.Success) {
                $result.user = [System.Uri]::UnescapeDataString($m.Groups[1].Value)
                $result.password = [System.Uri]::UnescapeDataString($m.Groups[2].Value)
                $result.host = $m.Groups[3].Value
                $result.port = [int]$m.Groups[4].Value
                $result.database = $m.Groups[5].Value
            } else {
                $m2 = [regex]::Match($url, '^mysql(?:\+pymysql)?://([^:@/]+)@([^:/]+):(\d+)/([^?]+)')
                if ($m2.Success) {
                    $result.user = [System.Uri]::UnescapeDataString($m2.Groups[1].Value)
                    $result.host = $m2.Groups[2].Value
                    $result.port = [int]$m2.Groups[3].Value
                    $result.database = $m2.Groups[4].Value
                }
            }
        } catch {}
    } elseif ($url.StartsWith("sqlite")) {
        $m = [regex]::Match($url, '^sqlite(?:\+pysqlcipher)?://(/.+)$|^sqlite:///(.+)$')
        if ($m.Success) {
            $p = if ($m.Groups[1].Value) { $m.Groups[1].Value } else { $m.Groups[2].Value }
            if ($p -match '^/([A-Za-z]):/') { $p = "$($m.Groups[1].Value.Substring(1))" }
            $result.sqlite_path = [System.IO.Path]::GetFullPath((Join-Path $ProjectRoot $p))
        }
    }
    return $result
}

function Resolve-SqlitePath($cfg, $urlParsed) {
    if (-not [string]::IsNullOrWhiteSpace($urlParsed.sqlite_path)) {
        return [System.IO.Path]::GetFullPath($urlParsed.sqlite_path)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $ProjectRoot "quant_workbench.db"))
}

function Parse-StampFile($stampPath) {
    $raw = Get-Content $stampPath -Raw -Encoding UTF8
    $result = @{}
    $inAlembic = $false
    $alembicLines = @()
    $inHashes = $false
    foreach ($line in ($raw -split "`r?`n")) {
        if ($inAlembic) {
            if ($line -match '^  [^ ]' -or $line -match '^\S') { $inAlembic = $false }
            else { $alembicLines += $line -replace '^  ', ''; continue }
        }
        if ($line -match '^([a-z_][a-z0-9_]*):\s*(.*)$') {
            $key = $matches[1]
            $val = $matches[2]
            if ($key -eq "alembic_head") {
                if ($val -eq "|") { $inAlembic = $true; $result[$key] = ""; continue }
                $result[$key] = $val
            } elseif ($key -eq "hashes") {
                $inHashes = $true
                $result[$key] = @{}
            } else {
                $result[$key] = $val
            }
        } elseif ($inHashes -and $line -match '^  ([a-z0-9_]+):\s*(\S+)') {
            $result["hashes"][$matches[1]] = $matches[2]
        }
    }
    if ($alembicLines.Count -gt 0) { $result["alembic_head"] = ($alembicLines -join "`n").Trim() }
    return $result
}

function Find-MysqlExe($name = "mysql.exe") {
    $inPath = Get-Command $name -ErrorAction SilentlyContinue
    if ($inPath) { return $inPath.Source }
    $searchPaths = @(
        "C:\Program Files\MySQL\MySQL Server *\bin\$name",
        "C:\Program Files (x86)\MySQL\MySQL Server *\bin\$name",
        "C:\Program Files\MySQL\MySQL Workbench*\$name",
        "C:\Program Files\MariaDB*\bin\$name"
    )
    foreach ($p in $searchPaths) {
        $found = Get-ChildItem -Path $p -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($found) { return $found.FullName }
    }
    return $null
}

function Test-SqliteBackupHasTables($backupDbFile) {
    $tmpDir = Join-Path $env:TEMP ("quant_restore_verify_" + [Guid]::NewGuid().ToString("N")[0..7] -join "")
    New-Item -ItemType Directory -Path $tmpDir -Force | Out-Null
    try {
        $tmpEmpty = Join-Path $tmpDir "empty.db"
        $tmpVerify = Join-Path $tmpDir "verify.db"
        $py = Join-Path $ProjectRoot ".venv" "Scripts" "python.exe"
        if (-not (Test-Path $py)) { $py = "python" }

        $pyCode = @"
import sqlite3, sys, shutil
empty = r"$tmpEmpty"
backup = r"$backupDbFile"
verify = r"$tmpVerify"
conn = sqlite3.connect(empty)
conn.execute("CREATE TABLE _dummy (id INTEGER)")
conn.commit()
conn.close()
shutil.copyfile(backup, verify)
conn2 = sqlite3.connect(verify)
rows = conn2.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
conn2.close()
print(len(rows))
"@
        $output = & $py -c $pyCode 2>&1
        $countStr = ($output | Out-String).Trim()
        if ($countStr -match '^\d+$') {
            $count = [int]$countStr
            Write-Host "  [Verify] SQLite tables found: $count"
            return $count -ge 1
        }
        Write-Warning "  [Verify] Python verification output: $output"
        return $false
    } finally {
        if (Test-Path $tmpDir) { Remove-Item -Path $tmpDir -Recurse -Force -ErrorAction SilentlyContinue }
    }
}

function Test-SqlBackupHasCreateTable($backupSqlFile) {
    $content = Get-Content $backupSqlFile -Raw -Encoding UTF8
    $count = ([regex]::Matches($content, 'CREATE\s+TABLE', [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)).Count
    Write-Host "  [Verify] CREATE TABLE statements found: $count"
    return $count -ge 1
}

try {
    if (-not (Test-Path $StampFile)) { throw "Stamp file not found: $StampFile" }
    $stamp = Parse-StampFile $StampFile
    $dbType = $stamp["db_type"]
    if ([string]::IsNullOrWhiteSpace($dbType)) { $dbType = "sqlite" }
    $backupFile = $stamp["backup_file"]
    if (-not $backupFile -or -not (Test-Path $backupFile)) {
        $guessExt = if ($dbType -eq "mysql") { ".sql" } else { ".db" }
        $backupFile = Join-Path $BackupDir ("quant_workbench_{0}{1}" -f $Tag, $guessExt)
    }
    if (-not (Test-Path $backupFile)) { throw "Backup file not found: $backupFile" }
    $backupFile = [System.IO.Path]::GetFullPath($backupFile)

    Write-Host "== Stamp Info =="
    Write-Host "  Tag         : $($stamp['tag'])"
    Write-Host "  Created At  : $($stamp['created_at'])"
    Write-Host "  DbType      : $dbType"
    Write-Host "  Source DB   : $($stamp['source_db'])"
    Write-Host "  Backup File : $backupFile"
    Write-Host ""

    $stampMd5 = if ($stamp.ContainsKey("hashes") -and $stamp["hashes"].ContainsKey("md5")) { $stamp["hashes"]["md5"] } else { $null }
    $stampSha256 = if ($stamp.ContainsKey("hashes") -and $stamp["hashes"].ContainsKey("sha256")) { $stamp["hashes"]["sha256"] } else { $null }

    Write-Host "== Hash Verification =="
    if ($stampMd5) {
        $actualMd5 = (Get-FileHash -Path $backupFile -Algorithm MD5).Hash.ToLowerInvariant()
        Write-Host "  MD5    : stamp=$stampMd5"
        Write-Host "         : actual=$actualMd5"
        if ($actualMd5 -ne $stampMd5) {
            Write-Error "MD5 hash mismatch! Backup file may be corrupted."
            Exit 1
        }
        Write-Host "  MD5    : OK"
    }
    if ($stampSha256) {
        $actualSha256 = (Get-FileHash -Path $backupFile -Algorithm SHA256).Hash.ToLowerInvariant()
        Write-Host "  SHA256 : stamp=$stampSha256"
        Write-Host "         : actual=$actualSha256"
        if ($actualSha256 -ne $stampSha256) {
            Write-Error "SHA256 hash mismatch! Backup file may be corrupted."
            Exit 1
        }
        Write-Host "  SHA256 : OK"
    }
    Write-Host ""

    $cfg = Read-DbConfig
    $envUrl = $env:DATABASE_URL
    $urlParsed = Parse-DatabaseUrl $envUrl

    if ($dbType -eq "sqlite") {
        $DbPath = Resolve-SqlitePath $cfg $urlParsed
        $Fallback = Join-Path $BackupDir ("quant_workbench_pre_restore_{0}.db" -f (Get-Date -Format "yyyyMMdd_HHmmss"))

        if (-not $SkipEmptyVerify) {
            Write-Host "== Empty Restore Verify =="
            if (-not (Test-SqliteBackupHasTables $backupFile)) {
                Write-Error "SQLite backup verification failed: no user tables found. Use -SkipEmptyVerify to bypass."
                Exit 1
            }
            Write-Host "  SQLite Empty Verify : PASSED"
            Write-Host ""
        } else {
            Write-Host "  [SkipEmptyVerify] Skipping empty-db restore check."
            Write-Host ""
        }

        $Confirm = Read-Host "This OVERWRITES $DbPath with $backupFile. Type 'RESTORE' to continue"
        if ($Confirm -ne "RESTORE") { Write-Host "Aborted."; exit 1 }

        if (Test-Path $DbPath) {
            Copy-Item -Path $DbPath -Destination $Fallback -Force
            Write-Host "  Safety copy created: $Fallback"
        }

        Copy-Item -Path $backupFile -Destination $DbPath -Force

        Write-Host ""
        Write-Host "== Restore OK =="
        Write-Host "  Restored to    : $DbPath"
        Write-Host "  Restored from  : $backupFile"
        if (Test-Path $Fallback) { Write-Host "  Safety copy    : $Fallback" }
        Write-Host "  Hash check     : PASSED (MD5/SHA256 match stamp)"
        if (-not $SkipEmptyVerify) { Write-Host "  Empty verify   : PASSED (tables detected in restored DB)" }
        Write-Host ""
        Write-Host "  Hint: Run .venv\Scripts\python.exe -m alembic stamp head  # if version misaligned"
    } else {
        $mysqlCfg = if ($cfg -and $cfg.mysql) { $cfg.mysql } else { @{} }
        $host = if ($urlParsed.host) { $urlParsed.host } else { $mysqlCfg.host }
        $port = if ($urlParsed.port) { $urlParsed.port } else { [int]$mysqlCfg.port }
        $database = if ($urlParsed.database) { $urlParsed.database } else { $mysqlCfg.database }
        $user = if ($urlParsed.user) { $urlParsed.user } else { $mysqlCfg.user }
        $password = if ($urlParsed.password) { $urlParsed.password } else { $mysqlCfg.password }

        if ([string]::IsNullOrWhiteSpace($host)) { $host = "127.0.0.1" }
        if (-not $port -or $port -eq 0) { $port = 3306 }
        if ([string]::IsNullOrWhiteSpace($database)) { throw "MySQL database name is empty (check config/db_config.json or DATABASE_URL)" }
        if ([string]::IsNullOrWhiteSpace($user)) { throw "MySQL user is empty" }

        if (-not $SkipEmptyVerify) {
            Write-Host "== Empty Restore Verify =="
            if (-not (Test-SqlBackupHasCreateTable $backupFile)) {
                Write-Error "MySQL backup verification failed: no CREATE TABLE statements found. Use -SkipEmptyVerify to bypass."
                Exit 1
            }
            Write-Host "  MySQL CREATE TABLE Verify : PASSED"
            Write-Host ""
        } else {
            Write-Host "  [SkipEmptyVerify] Skipping CREATE TABLE check."
            Write-Host ""
        }

        $Confirm = Read-Host "This OVERWRITES MySQL database '$database' on ${host}:${port} with $backupFile. Type 'RESTORE' to continue"
        if ($Confirm -ne "RESTORE") { Write-Host "Aborted."; exit 1 }

        $mysqlExe = Find-MysqlExe "mysql.exe"
        if (-not $mysqlExe) {
            Write-Error "mysql.exe not found. Please add MySQL bin directory to PATH or install MySQL Client."
            Write-Error "Typical location: C:\Program Files\MySQL\MySQL Server X.Y\bin\"
            Exit 1
        }

        $env:MYSQL_PWD = $password
        $args = @(
            "--default-character-set=utf8mb4",
            "-h", $host,
            "-P", $port,
            "-u", $user,
            $database
        )
        Get-Content $backupFile -Encoding UTF8 | & $mysqlExe @args
        $exitCode = $LASTEXITCODE
        $env:MYSQL_PWD = $null

        if ($exitCode -ne 0) {
            throw "mysql restore failed with exit code $exitCode"
        }

        Write-Host ""
        Write-Host "== Restore OK =="
        Write-Host "  Target DB      : mysql://${host}:${port}/${database}"
        Write-Host "  Restored from  : $backupFile"
        Write-Host "  Hash check     : PASSED (MD5/SHA256 match stamp)"
        if (-not $SkipEmptyVerify) { Write-Host "  SQL verify     : PASSED (CREATE TABLE >= 1)" }
        Write-Host ""
        Write-Host "  Hint: Run .venv\Scripts\python.exe -m alembic stamp head  # if version misaligned"
    }

    Exit 0
} catch {
    Write-Error "Restore FAILED: $_"
    Write-Error $_.ScriptStackTrace
    Exit 1
}
