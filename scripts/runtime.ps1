Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-RadarProjectRoot {
    return [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
}

function Import-RadarEnvironment {
    param([Parameter(Mandatory = $true)][string]$ProjectRoot)
    $taskEnvPath = Join-Path $ProjectRoot '.env'
    if (Test-Path -LiteralPath $taskEnvPath -PathType Leaf) {
        foreach ($taskLine in [System.IO.File]::ReadAllLines($taskEnvPath)) {
            if ($taskLine -notmatch '^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') { continue }
            $taskName = $Matches[1]
            $taskValue = $Matches[2].Trim()
            if ($taskValue.Length -ge 2 -and (($taskValue.StartsWith('"') -and $taskValue.EndsWith('"')) -or ($taskValue.StartsWith("'") -and $taskValue.EndsWith("'")))) {
                $taskValue = $taskValue.Substring(1, $taskValue.Length - 2)
            } else {
                $taskValue = ($taskValue -replace '\s+#.*$', '').Trim()
            }
            # Existing process settings take precedence. Values are data, never PowerShell code.
            if ([string]::IsNullOrEmpty([Environment]::GetEnvironmentVariable($taskName, 'Process'))) {
                [Environment]::SetEnvironmentVariable($taskName, $taskValue, 'Process')
            }
        }
    }
    if ([string]::IsNullOrWhiteSpace($env:DATABASE_BACKEND)) { $env:DATABASE_BACKEND = 'sqlite' }
    if ([string]::IsNullOrWhiteSpace($env:SQLITE_DATABASE_PATH)) { $env:SQLITE_DATABASE_PATH = Join-Path $ProjectRoot 'data\opportunity-radar.db' }
    if (-not [System.IO.Path]::IsPathRooted($env:SQLITE_DATABASE_PATH)) {
        $env:SQLITE_DATABASE_PATH = [System.IO.Path]::GetFullPath((Join-Path $ProjectRoot $env:SQLITE_DATABASE_PATH))
    }
    $env:PYTHONUNBUFFERED = '1'
    $env:PYTHONUTF8 = '1'
    $env:NEXT_TELEMETRY_DISABLED = '1'
    $env:NODE_ENV = 'production'
}

function Find-RadarPython {
    param([Parameter(Mandatory = $true)][string]$ProjectRoot)
    $taskCandidates = @(
        (Join-Path $ProjectRoot '.venv\Scripts\python.exe'),
        $env:RADAR_PYTHON,
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'),
        (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe')
    )
    foreach ($taskCandidate in $taskCandidates) {
        if ($taskCandidate -and (Test-Path -LiteralPath $taskCandidate -PathType Leaf)) { return [System.IO.Path]::GetFullPath($taskCandidate) }
    }
    $taskCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($taskCommand) { return $taskCommand.Source }
    throw 'Python est introuvable. Installer les dependances dans .venv ou definir RADAR_PYTHON.'
}

function Find-RadarNode {
    $taskCandidates = @(
        $env:RADAR_NODE,
        (Join-Path $env:ProgramFiles 'nodejs\node.exe'),
        (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe')
    )
    foreach ($taskCandidate in $taskCandidates) {
        if ($taskCandidate -and (Test-Path -LiteralPath $taskCandidate -PathType Leaf)) { return [System.IO.Path]::GetFullPath($taskCandidate) }
    }
    $taskCommand = Get-Command node.exe -ErrorAction SilentlyContinue
    if ($taskCommand) { return $taskCommand.Source }
    throw 'Node.js est introuvable. Installer Node.js ou definir RADAR_NODE.'
}

function Get-RadarPowerShell {
    return Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
}

function Join-RadarProcessArguments {
    param([string[]]$Values)
    # Parameters are fixed tokens, numbers and Windows paths (which cannot contain a quote).
    return (($Values | ForEach-Object {
        if ($_ -match '["\r\n]') { throw 'Argument de lancement invalide.' }
        if ($_ -match '\s') { '"' + $_ + '"' } else { $_ }
    }) -join ' ')
}

function Write-RadarSupervisorLog {
    param([string]$RuntimeRoot, [string]$Event, [string]$Message)
    $taskEntry = [ordered]@{ timestamp = [DateTime]::UtcNow.ToString('o'); event = $Event; message = $Message }
    $taskEntry | ConvertTo-Json -Compress | Add-Content -LiteralPath (Join-Path $RuntimeRoot 'supervisor.log') -Encoding UTF8
}
