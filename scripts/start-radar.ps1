param(
    [ValidateRange(1, 10080)][int]$IntervalMinutes = 60,
    [ValidateRange(1024, 65535)][int]$ApiPort = 8000,
    [ValidateRange(1024, 65535)][int]$WebPort = 3000
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$taskProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$taskRuntimeRoot = Join-Path $taskProjectRoot 'data\runtime'
$taskBootstrapLog = Join-Path $taskRuntimeRoot 'bootstrap.log'
try {
    [System.IO.Directory]::CreateDirectory($taskRuntimeRoot) | Out-Null
    [ordered]@{ timestamp = [DateTime]::UtcNow.ToString('o'); event = 'bootstrap_started'; pid = $PID } |
        ConvertTo-Json -Compress | Add-Content -LiteralPath $taskBootstrapLog -Encoding UTF8
    . (Join-Path $PSScriptRoot 'runtime.ps1')
    Set-Location -LiteralPath $taskProjectRoot
    Import-RadarEnvironment -ProjectRoot $taskProjectRoot
    $taskLockPath = Join-Path $taskRuntimeRoot 'supervisor.lock'
    try {
        $taskSupervisorLock = [System.IO.File]::Open($taskLockPath, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
    } catch [System.IO.IOException] {
        Write-RadarSupervisorLog $taskRuntimeRoot 'already_running' 'Le superviseur Radar fonctionne deja.'
        exit 0
    }
} catch {
    $taskBootstrapFailure = $_.Exception.Message
    try {
        [ordered]@{ timestamp = [DateTime]::UtcNow.ToString('o'); event = 'bootstrap_failed'; pid = $PID; message = $taskBootstrapFailure } |
            ConvertTo-Json -Compress | Add-Content -LiteralPath $taskBootstrapLog -Encoding UTF8
    } catch { }
    [Console]::Error.WriteLine('Echec du demarrage Radar : ' + $taskBootstrapFailure)
    exit 1
}

$taskStopPath = Join-Path $taskRuntimeRoot 'stop.request'
$taskStatePath = Join-Path $taskRuntimeRoot 'supervisor-state.json'
$taskChildren = @{}
$taskRetries = @{}

function Test-RadarPortOccupied {
    param([int]$Port)
    $taskSocket = New-Object System.Net.Sockets.TcpClient
    try {
        $taskConnection = $taskSocket.BeginConnect('127.0.0.1', $Port, $null, $null)
        if (-not $taskConnection.AsyncWaitHandle.WaitOne(500)) { return $false }
        $taskSocket.EndConnect($taskConnection)
        return $true
    } catch { return $false } finally { $taskSocket.Close() }
}

function Start-RadarChild {
    param([string]$Name, [string]$Executable, [string[]]$Values, [string]$WorkingDirectory)
    $taskTimestamp = [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss-fff')
    $taskStdout = Join-Path $taskRuntimeRoot ($Name + '-' + $taskTimestamp + '.log')
    $taskStderr = Join-Path $taskRuntimeRoot ($Name + '-' + $taskTimestamp + '-errors.log')
    $taskChild = Start-Process -FilePath $Executable -ArgumentList (Join-RadarProcessArguments -Values $Values) -WorkingDirectory $WorkingDirectory -WindowStyle Hidden -RedirectStandardOutput $taskStdout -RedirectStandardError $taskStderr -PassThru
    $taskChildren[$Name] = $taskChild
    Write-RadarSupervisorLog $taskRuntimeRoot 'process_started' ($Name + ' demarre. PID=' + $taskChild.Id)
}

function Write-RadarSupervisorState {
    $taskProcesses = @{}
    foreach ($taskName in @($taskChildren.Keys)) {
        $taskChild = $taskChildren[$taskName]
        $taskChild.Refresh()
        if (-not $taskChild.HasExited) {
            $taskProcesses[$taskName] = @{ pid = $taskChild.Id; started_at = $taskChild.StartTime.ToUniversalTime().ToString('o') }
        }
    }
    [ordered]@{ state = 'RUNNING'; supervisor_pid = $PID; updated_at = [DateTime]::UtcNow.ToString('o'); processes = $taskProcesses } |
        ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $taskStatePath -Encoding UTF8
}

try {
    # Old stop markers are harmless; only this supervisor's owned children can be stopped.
    if (Test-Path -LiteralPath $taskStopPath) { Remove-Item -LiteralPath $taskStopPath }
    $taskPython = Find-RadarPython -ProjectRoot $taskProjectRoot
    $taskNode = Find-RadarNode
    $taskWebRoot = Join-Path $taskProjectRoot 'apps\web'
    $taskNextCli = Join-Path $taskWebRoot 'node_modules\next\dist\bin\next'
    if (-not (Test-Path -LiteralPath $taskNextCli -PathType Leaf) -or -not (Test-Path -LiteralPath (Join-Path $taskWebRoot '.next\BUILD_ID') -PathType Leaf)) {
        throw 'Le frontend doit etre installe et construit avant son lancement.'
    }
    if ($ApiPort -eq $WebPort) { throw 'Les ports API et interface doivent etre differents.' }
    if ((Test-RadarPortOccupied $ApiPort) -or (Test-RadarPortOccupied $WebPort)) {
        throw 'Le port 8000 ou 3000 choisi est deja utilise. Arreter le lancement precedent avant de relancer Radar.'
    }
    & $taskPython -m services.operations.refresh --check --log-dir $taskRuntimeRoot *> (Join-Path $taskRuntimeRoot 'preflight.log')
    if ($LASTEXITCODE -ne 0) { throw 'La configuration Python, la base existante ou le profil ne sont pas prets. Consulter preflight.log.' }
    $env:OPPORTUNITY_API_BASE_URL = 'http://127.0.0.1:' + $ApiPort
    $taskDefinitions = @{
        api = @{ executable = $taskPython; arguments = @('-m', 'uvicorn', 'services.api.main:app', '--host', '127.0.0.1', '--port', [string]$ApiPort); working = $taskProjectRoot; port = $ApiPort }
        web = @{ executable = $taskNode; arguments = @($taskNextCli, 'start', '-H', '127.0.0.1', '-p', [string]$WebPort); working = $taskWebRoot; port = $WebPort }
        refresh = @{ executable = $taskPython; arguments = @('-m', 'services.operations.refresh', '--apply', '--watch', '--interval-minutes', [string]$IntervalMinutes, '--log-dir', $taskRuntimeRoot); working = $taskProjectRoot; port = 0 }
    }
    foreach ($taskName in @('api', 'web', 'refresh')) {
        $taskDefinition = $taskDefinitions[$taskName]
        Start-RadarChild $taskName $taskDefinition.executable $taskDefinition.arguments $taskDefinition.working
        $taskRetries[$taskName] = [DateTime]::UtcNow.AddSeconds(60)
    }
    Write-RadarSupervisorLog $taskRuntimeRoot 'supervisor_started' ('Radar actif; actualisation toutes les ' + $IntervalMinutes + ' minutes.')
    while (-not (Test-Path -LiteralPath $taskStopPath)) {
        foreach ($taskName in @('api', 'web', 'refresh')) {
            $taskChild = $taskChildren[$taskName]
            $taskChild.Refresh()
            if ($taskChild.HasExited -and [DateTime]::UtcNow -ge $taskRetries[$taskName]) {
                $taskDefinition = $taskDefinitions[$taskName]
                if ($taskDefinition.port -eq 0 -or -not (Test-RadarPortOccupied $taskDefinition.port)) {
                    Write-RadarSupervisorLog $taskRuntimeRoot 'process_restarting' ($taskName + ' termine; tentative de relance.')
                    Start-RadarChild $taskName $taskDefinition.executable $taskDefinition.arguments $taskDefinition.working
                }
                $taskRetries[$taskName] = [DateTime]::UtcNow.AddSeconds(60)
            }
        }
        Write-RadarSupervisorState
        Start-Sleep -Seconds 5
    }
} catch {
    Write-RadarSupervisorLog $taskRuntimeRoot 'supervisor_failed' $_.Exception.Message
    Write-Error $_ -ErrorAction Continue
    exit 1
} finally {
    foreach ($taskChild in @($taskChildren.Values)) {
        try {
            $taskChild.Refresh()
            if (-not $taskChild.HasExited) { & (Join-Path $env:SystemRoot 'System32\taskkill.exe') /PID $taskChild.Id /T /F 2>$null | Out-Null }
            $taskChild.Dispose()
        } catch { }
    }
    [ordered]@{ state = 'STOPPED'; updated_at = [DateTime]::UtcNow.ToString('o') } | ConvertTo-Json | Set-Content -LiteralPath $taskStatePath -Encoding UTF8
    if (Test-Path -LiteralPath $taskStopPath) { Remove-Item -LiteralPath $taskStopPath }
    $taskSupervisorLock.Dispose()
}
