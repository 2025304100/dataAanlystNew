# Database backup + rollback point script for SQLite + MySQL (Windows PowerShell)
# Usage: powershell -ExecutionPolicy Bypass -File scripts\backup_db.ps1 [-Tag pre_migration_0031] [-DbType auto|sqlite|mysql]
param(
    [string]$Tag = "backup_$(Get-Date -Format 'yyyyMMdd_HHmmss')",
    [ValidateSet("auto","sqlite","mysql")][string]$DbType = "auto"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$BackupDir = Join-Path $ProjectRoot "backups"
$DbConfigPath = Join-Path $ProjectRoot "config" "db_config.json"

if (-not (Test-Path $BackupDir)) { New-Item -ItemType Directory -Path $BackupDir | Out-Null }

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
    if ($url.StartsWith("mysql") -or $url.StartsWith("MariaDB")) {
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

function Find-MySqlDump {
    $inPath = Get-Command mysqldump.exe -ErrorAction SilentlyContinue
    if ($inPath) { return $inPath.Source }
    $searchPaths = @(
        "C:\Program Files\MySQL\MySQL Server *\bin\mysqldump.exe",
        "C:\Program Files (x86)\MySQL\MySQL Server *\bin\mysqldump.exe",
        "C:\Program Files\MySQL\MySQL Workbench*\mysqldump.exe",
        "C:\Program Files\MariaDB*\bin\mysqldump.exe"
    )
    foreach ($p in $searchPaths) {
        $found = Get-ChildItem -Path $p -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($found) { return $found.FullName }
    }
    return $null
}

function Get-AlembicHead {
    Push-Location $ProjectRoot
    try {
        $py = Join-Path $ProjectRoot ".venv" "Scripts" "python.exe"
        if (Test-Path $py) {
            $out = & $py -m alembic heads 2>&1
        } else {
            $out = & python -m alembic heads 2>&1
        }
        return ($out | Out-String).Trim()
    } catch { return "" }
    finally { Pop-Location }
}

function Indent-YamlBlock($text, $spaces = 2) {
    $pad = " " * $spaces
    if ([string]::IsNullOrWhiteSpace($text)) { return "${pad}(none)" }
    $lines = $text -split "`r?`n"
    ($lines | ForEach-Object { "$pad$($_)" }) -join "`n"
}

try {
    $cfg = Read-DbConfig
    $envUrl = $env:DATABASE_URL
    $urlParsed = Parse-DatabaseUrl $envUrl

    $resolvedDbType = $DbType
    if ($resolvedDbType -eq "auto") {
        if ($urlParsed.use_mysql) {
            $resolvedDbType = "mysql"
        } elseif ($cfg -and $cfg.use_mysql) {
            $resolvedDbType = "mysql"
        } else {
            $resolvedDbType = "sqlite"
        }
    }

    $alembicHead = Get-AlembicHead

    if ($resolvedDbType -eq "sqlite") {
        $DbPath = Resolve-SqlitePath $cfg $urlParsed
        if (-not (Test-Path $DbPath)) { throw "SQLite database not found at $DbPath" }

        $BackupFile = Join-Path $BackupDir ("quant_workbench_{0}.db" -f $Tag)
        Copy-Item -Path $DbPath -Destination $BackupFile -Force
        $sourceDbRef = $DbPath
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

        $mysqldump = Find-MySqlDump
        if (-not $mysqldump) {
            Write-Error "mysqldump.exe not found. Please add MySQL bin directory to PATH or install MySQL Client."
            Write-Error "Typical location: C:\Program Files\MySQL\MySQL Server X.Y\bin\"
            Exit 1
        }

        $BackupFile = Join-Path $BackupDir ("quant_workbench_{0}.sql" -f $Tag)
        $sourceDbRef = "mysql://${host}:${port}/${database}"

        $env:MYSQL_PWD = $password
        $args = @(
            "--single-transaction",
            "--routines",
            "--triggers",
            "--default-character-set=utf8mb4",
            "-h", $host,
            "-P", $port,
            "-u", $user,
            $database
        )
        & $mysqldump @args | Out-File -FilePath $BackupFile -Encoding utf8
        $env:MYSQL_PWD = $null

        if ($LASTEXITCODE -ne 0) {
            throw "mysqldump failed with exit code $LASTEXITCODE"
        }
    }

    if (-not (Test-Path $BackupFile)) { throw "Backup file was not created: $BackupFile" }
    $BackupFile = [System.IO.Path]::GetFullPath($BackupFile)
    $Size = (Get-Item $BackupFile).Length

    $md5 = (Get-FileHash -Path $BackupFile -Algorithm MD5).Hash.ToLowerInvariant()
    $sha256 = (Get-FileHash -Path $BackupFile -Algorithm SHA256).Hash.ToLowerInvariant()

    $StampFile = Join-Path $BackupDir ("quant_workbench_{0}.stamp" -f $Tag)
    $Stamp = @"
tag: $Tag
created_at: $(Get-Date -Format 'o')
db_type: $resolvedDbType
source_db: $sourceDbRef
backup_file: $BackupFile
backup_size_bytes: $Size
alembic_head: |
$(Indent-YamlBlock $alembicHead 2)
hashes:
  md5: $md5
  sha256: $sha256
"@
    Set-Content -Path $StampFile -Value $Stamp -Encoding UTF8

    Write-Host "== Backup OK =="
    Write-Host "  Tag           : $Tag"
    Write-Host "  DbType        : $resolvedDbType"
    Write-Host "  Source DB     : $sourceDbRef"
    Write-Host "  Size          : $Size bytes"
    Write-Host "  Backup File   : $BackupFile"
    Write-Host "  Stamp File    : $StampFile"
    Write-Host "  MD5           : $md5"
    Write-Host "  SHA256        : $sha256"
    Write-Host "  Alembic Head  : $($alembicHead -split "`r?`n" | Select-Object -First 1)"
    Exit 0
} catch {
    Write-Error "Backup FAILED: $_"
    Write-Error $_.ScriptStackTrace
    Exit 1
}
