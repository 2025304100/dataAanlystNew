# Pure process-selection policy. Dot-sourcing this file performs no process I/O.
# Unsupported quoting or incomplete identity is rejected, never guessed from a port.
function Get-StrictServiceCommandTokens {
    param([string]$CommandLine)
    $position = 0
    $tokens = @()
    while ($position -lt $CommandLine.Length) {
        $match = [regex]::Match($CommandLine.Substring($position), '^\s*(?:"([^"\r\n]+)"|([^\s"\r\n]+))(?=\s|$)')
        if (-not $match.Success) {
            if ($CommandLine.Substring($position).Trim().Length -eq 0) { break }
            return @()
        }
        $tokens += if ($match.Groups[1].Success) { $match.Groups[1].Value } else { $match.Groups[2].Value }
        $position += $match.Length
    }
    return $tokens
}

function Test-ServiceAbsolutePath {
    param([string]$Value)
    return $Value -match '^(?:[a-zA-Z]:[\\/]|[\\/]{2}[^\\/]+[\\/][^\\/]+)'
}

function Get-CanonicalServicePath {
    param([string]$Value, [string]$BaseDirectory = '')
    if ([string]::IsNullOrWhiteSpace($Value)) { return $null }
    try {
        if (-not (Test-ServiceAbsolutePath $Value)) {
            if ([IO.Path]::IsPathRooted($Value) -or $Value.Contains(':')) { return $null }
            if (-not $BaseDirectory) { return $null }
            $Value = Join-Path $BaseDirectory $Value
        }
        return [IO.Path]::GetFullPath($Value).TrimEnd('\', '/')
    }
    catch { return $null }
}

function Get-ProjectServiceShape {
    param([string[]]$Tokens, [string]$ProjectRoot, [string]$TrustedCwd = '')
    if ($Tokens.Count -eq 8 -and $Tokens[1] -ceq '-m' -and $Tokens[2] -ceq 'uvicorn' -and
        $Tokens[3] -ceq 'app.main:app' -and $Tokens[4] -ceq '--host' -and
        $Tokens[5] -ceq '127.0.0.1' -and $Tokens[6] -ceq '--port' -and $Tokens[7] -ceq '8000' -and
        $TrustedCwd -ieq $ProjectRoot) {
        return [pscustomobject]@{ Name='backend'; Mode='uvicorn'; Port=8000; Cwd=$ProjectRoot; Executable='python.exe'; Absolute=$false }
    }
    $frontend = Join-Path $ProjectRoot 'frontend'
    if ($Tokens.Count -eq 8 -and $Tokens[2] -ceq '--config' -and $Tokens[4] -ceq '--host' -and
        $Tokens[5] -ceq '127.0.0.1' -and $Tokens[6] -ceq '--port' -and $Tokens[7] -ceq '5173' -and
        (Get-CanonicalServicePath $Tokens[1] $TrustedCwd) -ieq (Join-Path $frontend 'node_modules\vite\bin\vite.js') -and
        (Get-CanonicalServicePath $Tokens[3] $TrustedCwd) -ieq (Join-Path $frontend 'vite.codex.config.ts')) {
        return [pscustomobject]@{ Name='frontend'; Mode='vite'; Port=5173; Cwd=$frontend; Executable='node.exe';
            Absolute=((Test-ServiceAbsolutePath $Tokens[1]) -and (Test-ServiceAbsolutePath $Tokens[3])) }
    }
    if ($Tokens.Count -eq 2 -and
        (Get-CanonicalServicePath $Tokens[1] $TrustedCwd) -ieq (Join-Path $frontend 'scripts\universe-standalone-server.mjs')) {
        return [pscustomobject]@{ Name='frontend'; Mode='standalone'; Port=5173; Cwd=$ProjectRoot; Executable='node.exe';
            Absolute=(Test-ServiceAbsolutePath $Tokens[1]) }
    }
    return $null
}

function Test-ProjectServiceProcess {
    param([object]$Process, [string]$ProjectRoot, [object]$ManagedRecord = $null)
    $rootPath = Get-CanonicalServicePath $ProjectRoot
    if (-not $rootPath -or [string]::IsNullOrWhiteSpace([string]$Process.CommandLine)) { return $false }
    $tokens = @(Get-StrictServiceCommandTokens ([string]$Process.CommandLine))
    if ($tokens.Count -lt 2) { return $false }
    $cwdPath = if ($ManagedRecord) { Get-CanonicalServicePath ([string]$ManagedRecord.cwd) } else { '' }
    $shape = Get-ProjectServiceShape -Tokens $tokens -ProjectRoot $rootPath -TrustedCwd $cwdPath
    if (-not $shape -or $Process.Name -ine $shape.Executable) { return $false }
    $executableName = [IO.Path]::GetFileName($tokens[0])
    if ($executableName -ine $shape.Executable -and "$executableName.exe" -ine $shape.Executable) { return $false }
    if ((Test-ServiceAbsolutePath $tokens[0]) -and $Process.ExecutablePath -and
        (Get-CanonicalServicePath $tokens[0]) -ine (Get-CanonicalServicePath ([string]$Process.ExecutablePath))) { return $false }
    if ($shape.Absolute) { return $true }
    if (-not $ManagedRecord -or $ManagedRecord.pid -ne $Process.ProcessId -or
        $ManagedRecord.name -cne $shape.Name -or $ManagedRecord.mode -cne $shape.Mode -or
        $ManagedRecord.port -ne $shape.Port -or $cwdPath -ine $shape.Cwd) { return $false }
    $recordTokens = @($ManagedRecord.command)
    if ($recordTokens.Count -ne $tokens.Count) { return $false }
    for ($index = 0; $index -lt $tokens.Count; $index++) {
        if ([string]$recordTokens[$index] -ine $tokens[$index]) { return $false }
    }
    $runtime = Join-Path $rootPath 'tmp\dev-services'
    if ((Get-CanonicalServicePath ([string]$ManagedRecord.stdout_log)) -ine (Join-Path $runtime "$($shape.Name).out.log") -or
        (Get-CanonicalServicePath ([string]$ManagedRecord.stderr_log)) -ine (Join-Path $runtime "$($shape.Name).err.log")) { return $false }
    # Existing records store readiness time, not an instance nonce. Require a
    # queried process creation time within that launch's bounded startup window.
    # Missing timestamps, PID reuse and ambiguous old records are preserved.
    if (-not $Process.CreationDate -or -not $ManagedRecord.started_at) { return $false }
    try {
        $recordTime = [datetime]::ParseExact([string]$ManagedRecord.started_at, 'yyyy-MM-dd HH:mm:ss', [Globalization.CultureInfo]::InvariantCulture)
        $creationTime = [datetime]$Process.CreationDate
        $startupSeconds = ($recordTime - $creationTime).TotalSeconds
        return $startupSeconds -ge -1 -and $startupSeconds -le 60
    }
    catch { return $false }
}
