param(
    [switch]$Watch,
    [switch]$SkipCollection,
    [switch]$Check,
    [ValidateRange(1, 10080)][int]$IntervalMinutes = 60,
    [string[]]$Source
)

. (Join-Path $PSScriptRoot 'runtime.ps1')
$taskProjectRoot = Get-RadarProjectRoot
Set-Location -LiteralPath $taskProjectRoot
Import-RadarEnvironment -ProjectRoot $taskProjectRoot
$taskPython = Find-RadarPython -ProjectRoot $taskProjectRoot
$taskArguments = @('-m', 'services.operations.refresh', '--log-dir', (Join-Path $taskProjectRoot 'data\runtime'))
if ($Check) {
    if ($Watch) { throw '-Check et -Watch ne sont pas compatibles.' }
    $taskArguments += '--check'
} else {
    $taskArguments += '--apply'
    if ($Watch) { $taskArguments += @('--watch', '--interval-minutes', [string]$IntervalMinutes) }
    if ($SkipCollection) { $taskArguments += '--skip-collection' }
    foreach ($taskSource in $Source) { $taskArguments += @('--source', $taskSource) }
}
& $taskPython @taskArguments
exit $LASTEXITCODE
