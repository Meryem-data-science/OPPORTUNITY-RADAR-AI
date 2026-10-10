param(
    [ValidateRange(1, 10080)][int]$IntervalMinutes = 60,
    [ValidateRange(1024, 65535)][int]$ApiPort = 8000,
    [ValidateRange(1024, 65535)][int]$WebPort = 3000,
    [ValidateRange(180, 900)][int]$StartupTimeoutSeconds = 180
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$taskProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$taskRuntimeRoot = Join-Path $taskProjectRoot 'data\runtime'
$taskLaunchId = [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss-fff') + '-' + $PID
$taskLauncherLog = Join-Path $taskRuntimeRoot ('manual-launch-' + $taskLaunchId + '.log')
$taskSupervisorStderr = Join-Path $taskRuntimeRoot ('supervisor-' + $taskLaunchId + '-errors.log')
$taskSupervisorProcess = $null
$taskLauncherLock = $null

function Write-RadarLauncherLog {
    param([string]$Event, [string]$Message)
    [ordered]@{ timestamp = [DateTime]::UtcNow.ToString('o'); event = $Event; message = $Message } |
        ConvertTo-Json -Compress | Add-Content -LiteralPath $taskLauncherLog -Encoding UTF8
}

function Test-RadarHttpReady {
    param([string]$Uri, [int]$MinimumStatus, [int]$MaximumStatus)
    try {
        $taskResponse = Invoke-WebRequest -Uri $Uri -UseBasicParsing -TimeoutSec 3 -MaximumRedirection 0
        $taskStatus = [int]$taskResponse.StatusCode
        return ($taskStatus -ge $MinimumStatus -and $taskStatus -le $MaximumStatus)
    } catch {
        # Windows PowerShell reports redirects as WebExceptions when redirects are disabled.
        if ($_.Exception -is [System.Net.WebException] -and $null -ne $_.Exception.Response) {
            $taskStatus = [int]$_.Exception.Response.StatusCode
            return ($taskStatus -ge $MinimumStatus -and $taskStatus -le $MaximumStatus)
        }
        return $false
    }
}

function Test-RadarInterfaceNeedsBuild {
    $taskWebRoot = Join-Path $taskProjectRoot 'apps\web'
    $taskBuildId = Join-Path $taskWebRoot '.next\BUILD_ID'
    if (-not (Test-Path -LiteralPath $taskBuildId -PathType Leaf)) { return $true }
    $taskBuiltAt = (Get-Item -LiteralPath $taskBuildId).LastWriteTimeUtc
    foreach ($taskSourceDirectory in @('app', 'components', 'lib', 'public')) {
        $taskSourcePath = Join-Path $taskWebRoot $taskSourceDirectory
        if (Test-Path -LiteralPath $taskSourcePath -PathType Container) {
            foreach ($taskSourceFile in (Get-ChildItem -LiteralPath $taskSourcePath -File -Recurse)) {
                if ($taskSourceFile.LastWriteTimeUtc -gt $taskBuiltAt) { return $true }
            }
        }
    }
    foreach ($taskConfigName in @('package.json', 'package-lock.json', 'next.config.ts', 'tsconfig.json')) {
        $taskConfigPath = Join-Path $taskWebRoot $taskConfigName
        if ((Test-Path -LiteralPath $taskConfigPath -PathType Leaf) -and (Get-Item -LiteralPath $taskConfigPath).LastWriteTimeUtc -gt $taskBuiltAt) { return $true }
    }
    return $false
}

function Update-RadarInterface {
    $taskWebRoot = Join-Path $taskProjectRoot 'apps\web'
    $taskNextCli = Join-Path $taskWebRoot 'node_modules\next\dist\bin\next'
    if (-not (Test-Path -LiteralPath $taskNextCli -PathType Leaf)) {
        throw 'Les dependances de l''interface doivent etre installees avant son lancement.'
    }
    $taskNode = Find-RadarNode
    Write-Host 'Actualisation de l''interface...'
    Write-RadarLauncherLog 'interface_update_requested' 'Les sources ont change depuis la derniere compilation.'
    # Let the existing Windows supervisor stop its own children before rebuilding.
    [System.IO.File]::WriteAllText((Join-Path $taskRuntimeRoot 'stop.request'), [DateTime]::UtcNow.ToString('o'))
    $taskSupervisorLockPath = Join-Path $taskRuntimeRoot 'supervisor.lock'
    $taskStopDeadline = [DateTime]::UtcNow.AddSeconds(35)
    $taskStopped = $false
    while ([DateTime]::UtcNow -lt $taskStopDeadline) {
        try {
            $taskUpdateLock = [System.IO.File]::Open($taskSupervisorLockPath, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
            $taskStopped = $true
            break
        } catch [System.IO.IOException] { Start-Sleep -Seconds 1 }
    }
    if (-not $taskStopped) { throw 'Radar ne s''est pas arrete a temps pour actualiser l''interface.' }
    try {
        Push-Location -LiteralPath $taskWebRoot
        try {
            & $taskNode $taskNextCli build
            if ($LASTEXITCODE -ne 0) { throw 'La compilation de l''interface a echoue.' }
        } finally { Pop-Location }
    } finally { $taskUpdateLock.Dispose() }
    Write-RadarLauncherLog 'interface_updated' 'Nouvelle interface compilee.'
}

try {
    [System.IO.Directory]::CreateDirectory($taskRuntimeRoot) | Out-Null
    try {
        $taskLauncherLock = [System.IO.File]::Open((Join-Path $taskRuntimeRoot 'manual-launch.lock'), [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
    } catch [System.IO.IOException] {
        throw 'Un lancement de Radar est deja en cours. Attendez la fin de sa fenetre.'
    }
    Write-RadarLauncherLog 'bootstrap_started' 'Lancement manuel de Radar.'
    Write-Host 'Demarrage de Radar...'
    . (Join-Path $PSScriptRoot 'runtime.ps1')
    Set-Location -LiteralPath $taskProjectRoot
    Import-RadarEnvironment -ProjectRoot $taskProjectRoot
    if (Test-RadarInterfaceNeedsBuild) { Update-RadarInterface }
    $taskSupervisorStdout = Join-Path $taskRuntimeRoot ('supervisor-' + $taskLaunchId + '.log')
    $taskSupervisorArguments = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden',
        '-File', (Join-Path $PSScriptRoot 'start-radar.ps1'),
        '-IntervalMinutes', [string]$IntervalMinutes, '-ApiPort', [string]$ApiPort, '-WebPort', [string]$WebPort
    )
    $taskSupervisorProcess = Start-Process -FilePath (Get-RadarPowerShell) -ArgumentList (Join-RadarProcessArguments -Values $taskSupervisorArguments) -WorkingDirectory $taskProjectRoot -WindowStyle Hidden -RedirectStandardOutput $taskSupervisorStdout -RedirectStandardError $taskSupervisorStderr -PassThru
    Write-RadarLauncherLog 'supervisor_requested' ('PID=' + $taskSupervisorProcess.Id)
    Write-Host ('Attente de l''API et de l''interface (jusqu''a ' + $StartupTimeoutSeconds + ' secondes)...')
    $taskApiUrl = 'http://127.0.0.1:' + $ApiPort + '/api/health'
    $taskWebUrl = 'http://127.0.0.1:' + $WebPort
    $taskDeadline = [DateTime]::UtcNow.AddSeconds($StartupTimeoutSeconds)
    $taskReady = $false
    while ([DateTime]::UtcNow -lt $taskDeadline) {
        $taskSupervisorProcess.Refresh()
        if ($taskSupervisorProcess.HasExited -and $taskSupervisorProcess.ExitCode -ne 0) {
            throw ('Le superviseur Radar a echoue (code ' + $taskSupervisorProcess.ExitCode + ').')
        }
        $taskApiReady = Test-RadarHttpReady -Uri $taskApiUrl -MinimumStatus 200 -MaximumStatus 200
        $taskWebReady = Test-RadarHttpReady -Uri $taskWebUrl -MinimumStatus 200 -MaximumStatus 399
        if ($taskApiReady -and $taskWebReady) {
            $taskReady = $true
            break
        }
        Start-Sleep -Seconds 1
    }
    if (-not $taskReady) {
        throw ('Radar ne repond pas apres ' + $StartupTimeoutSeconds + ' secondes. Consulter preflight.log, supervisor.log et les journaux API/interface dans data\runtime.')
    }
    Write-RadarLauncherLog 'services_ready' 'API et interface disponibles.'
    Start-Process -FilePath $taskWebUrl | Out-Null
    Write-RadarLauncherLog 'browser_opened' 'Interface ouverte dans le navigateur.'
    Write-Host 'Radar est pret. Le navigateur est ouvert.'
    exit 0
} catch {
    $taskFailure = $_.Exception.Message
    try { Write-RadarLauncherLog 'launch_failed' $taskFailure } catch { }
    Write-Host ('Echec du lancement : ' + $taskFailure) -ForegroundColor Red
    if (Test-Path -LiteralPath $taskSupervisorStderr -PathType Leaf) {
        Write-Host 'Details du superviseur :'
        Get-Content -LiteralPath $taskSupervisorStderr -Tail 12 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host $_ }
    }
    Write-Host ('Journal du lancement : ' + $taskLauncherLog)
    exit 1
} finally {
    # The hidden supervisor keeps running after this short manual launcher exits.
    if ($null -ne $taskSupervisorProcess) { $taskSupervisorProcess.Dispose() }
    if ($null -ne $taskLauncherLock) { $taskLauncherLock.Dispose() }
}
