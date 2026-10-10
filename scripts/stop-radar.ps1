. (Join-Path $PSScriptRoot 'runtime.ps1')
$taskRuntimeRoot = Join-Path (Get-RadarProjectRoot) 'data\runtime'
[System.IO.Directory]::CreateDirectory($taskRuntimeRoot) | Out-Null
[System.IO.File]::WriteAllText((Join-Path $taskRuntimeRoot 'stop.request'), [DateTime]::UtcNow.ToString('o'))
Write-Output 'Arret demande. Le superviseur ferme ses trois processus dans quelques secondes.'
