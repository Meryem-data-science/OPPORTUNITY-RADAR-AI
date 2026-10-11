Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

try {
    . (Join-Path $PSScriptRoot 'runtime.ps1')
    $taskProjectRoot = Get-RadarProjectRoot
    Set-Location -LiteralPath $taskProjectRoot
    Import-RadarEnvironment -ProjectRoot $taskProjectRoot
    $taskPython = Find-RadarPython -ProjectRoot $taskProjectRoot

    Write-Host 'Connexion des alertes LinkedIn a Opportunity Radar AI'
    Write-Host ''
    Write-Host 'Ce lanceur autorise la lecture des alertes LinkedIn recues dans Gmail.'
    Write-Host 'Le navigateur Google va s''ouvrir : choisis ton compte et autorise la lecture seule.'
    Write-Host 'Cette connexion ne lance pas de collecte.'
    Write-Host ''

    & $taskPython '-m' 'services.collector.cli.gmail_authorize'
    $taskExitCode = $LASTEXITCODE
    if ($taskExitCode -ne 0) {
        Write-Host ''
        Write-Host 'La connexion a echoue. Pour une configuration manquante, voir docs\connexion-linkedin.md.'
    }
    exit $taskExitCode
} catch {
    Write-Host ''
    Write-Host 'La connexion n''a pas pu demarrer. Voir docs\connexion-linkedin.md.'
    exit 1
}
