param(
    [switch]$Remove,
    [ValidateRange(1, 10080)][int]$IntervalMinutes = 60
)

. (Join-Path $PSScriptRoot 'runtime.ps1')
$taskProjectRoot = Get-RadarProjectRoot
$taskStartupRoot = [Environment]::GetFolderPath('Startup')
if ([string]::IsNullOrWhiteSpace($taskStartupRoot)) { throw 'Le dossier Demarrage Windows du compte courant est introuvable.' }
$taskShortcutPath = Join-Path $taskStartupRoot 'Opportunity Radar AI.lnk'
if ($Remove) {
    if (Test-Path -LiteralPath $taskShortcutPath -PathType Leaf) { Remove-Item -LiteralPath $taskShortcutPath }
    Write-Output 'Demarrage automatique desactive pour ce compte Windows.'
    exit 0
}

$taskStartScript = Join-Path $PSScriptRoot 'start-radar.ps1'
if (-not (Test-Path -LiteralPath $taskStartScript -PathType Leaf)) { throw 'Le script start-radar.ps1 est introuvable.' }
[System.IO.Directory]::CreateDirectory($taskStartupRoot) | Out-Null
$taskShell = New-Object -ComObject WScript.Shell
$taskShortcut = $null
try {
    $taskShortcut = $taskShell.CreateShortcut($taskShortcutPath)
    $taskShortcut.TargetPath = Get-RadarPowerShell
    $taskShortcut.Arguments = Join-RadarProcessArguments -Values @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', '-File', $taskStartScript, '-IntervalMinutes', [string]$IntervalMinutes)
    $taskShortcut.WorkingDirectory = $taskProjectRoot
    $taskShortcut.WindowStyle = 7
    $taskShortcut.Description = 'Opportunity Radar AI : application locale et collecte a ouverture de session.'
    $taskShortcut.Save()
} finally {
    if ($taskShortcut) { [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($taskShortcut) }
    [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($taskShell)
}
Write-Output ('Demarrage automatique configure dans : ' + $taskShortcutPath)
Write-Output 'Il sera actif a la prochaine ouverture de session Windows. Aucun lancement immediat.'
