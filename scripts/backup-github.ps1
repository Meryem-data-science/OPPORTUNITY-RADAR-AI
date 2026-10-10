param(
    [string]$Message = 'Sauvegarde de la seance de developpement',
    [string]$Branch = 'sauvegarde-mvp-2026-10-11'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$taskRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$taskExpectedOrigin = 'https://github.com/Meryem-data-science/OPPORTUNITY-RADAR-AI.git'

function Invoke-BackupGit {
    param([string[]]$Arguments)
    $taskPreviousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $taskResult = @(& $script:taskGit -c core.quotepath=false @Arguments 2>&1)
        $taskExit = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $taskPreviousPreference
    }
    if ($taskExit -ne 0) { throw ($taskResult -join [Environment]::NewLine) }
    return $taskResult
}

try {
    $taskGitCommand = Get-Command git.exe -ErrorAction SilentlyContinue
    if ($taskGitCommand) {
        $script:taskGit = $taskGitCommand.Source
    } else {
        $script:taskGit = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\native\git\cmd\git.exe'
        if (-not (Test-Path -LiteralPath $script:taskGit -PathType Leaf)) {
            throw 'Git est introuvable. Installer Git for Windows pour utiliser ce lanceur.'
        }
    }
    Push-Location -LiteralPath $taskRoot
    try {
        $taskOrigins = @(Invoke-BackupGit -Arguments @('remote', 'get-url', '--all', 'origin'))
        $taskPushOrigins = @(Invoke-BackupGit -Arguments @('remote', 'get-url', '--push', '--all', 'origin'))
        if ($taskOrigins.Count -ne 1 -or $taskOrigins[0] -ne $taskExpectedOrigin -or
            $taskPushOrigins.Count -ne 1 -or $taskPushOrigins[0] -ne $taskExpectedOrigin) {
            throw 'La destination Git ne correspond pas uniquement au projet attendu.'
        }
        $taskCurrentBranch = (Invoke-BackupGit -Arguments @('branch', '--show-current')) -join ''
        if ($taskCurrentBranch -ne $Branch) {
            throw "La branche active est $taskCurrentBranch. Demander a l'agent de preparer la sauvegarde sur $Branch."
        }

        # Known source roots plus tracked changes. Personal files at the root
        # are not automatically added; .gitignore applies to new source files.
        Invoke-BackupGit -Arguments @('add', '-u') | Out-Null
        $taskRoots = @('apps', 'services', 'scripts', 'config', 'docs', 'migrations', 'tests', 'evaluation', '.github')
        $taskRootFiles = @('.gitignore', '.gitattributes', '.env.example', 'AGENTS.md', 'README.md',
            'pyproject.toml', 'Lancer-Radar.cmd', 'Ouvrir Radar.url', 'Sauvegarder-GitHub.cmd')
        $taskPaths = @($taskRoots + $taskRootFiles | Where-Object { Test-Path -LiteralPath (Join-Path $taskRoot $_) })
        Invoke-BackupGit -Arguments (@('add', '-A', '--') + $taskPaths) | Out-Null
        $taskStaged = @(Invoke-BackupGit -Arguments @('diff', '--cached', '--name-only', '--diff-filter=ACMR'))
        foreach ($taskPath in $taskStaged) {
            $taskRelative = [string]$taskPath
            if ($taskRelative -match '(^|/)(data|\.data|\.secrets|\.claude|\.venv|node_modules|\.next)/' -or
                $taskRelative -match '(^|/)\.env($|\.(?!example$))' -or
                $taskRelative -match '(?i)(credentials.*\.json$|oauth.*\.json$|gmail.*token|\.(db(?:-[^/]*)?|sqlite(?:3)?|pem|key)$|(^|/)TRANSFER[^/]*$|phase8a2.*_audit\.txt$)') {
                throw "Fichier local sensible present dans le commit : $taskRelative. Aucun envoi effectue."
            }
        }
        # Check only textual additions; do not print secret values if found.
        $taskAdded = @(Invoke-BackupGit -Arguments @('diff', '--cached', '--no-ext-diff', '--no-color', '--unified=0')) |
            Where-Object { $_ -match '^\+[^+]' }
        $taskSecretPattern = '(gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,}|AIza[A-Za-z0-9_-]{35}|\bAKIA[A-Z0-9]{16}\b|sk-(?:proj-)?[A-Za-z0-9_-]{40,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)'
        if (@($taskAdded | Where-Object { $_ -match $taskSecretPattern }).Count -gt 0) {
            throw 'Un secret potentiel a ete detecte dans les ajouts. Aucun commit ni envoi effectue.'
        }
        Write-Output 'Fichiers inclus dans la sauvegarde :'
        Invoke-BackupGit -Arguments @('diff', '--cached', '--stat') | Write-Output
        $taskPending = @(Invoke-BackupGit -Arguments @('diff', '--cached', '--name-only'))
        if ($taskPending.Count -gt 0) {
            Invoke-BackupGit -Arguments @('commit', '-m', $Message) | Write-Output
        } else {
            Write-Output 'Aucune nouvelle modification de code. Envoi des commits locaux en attente.'
        }
        Invoke-BackupGit -Arguments @('push', '-u', 'origin', $Branch) | Write-Output
        $taskLocalCommit = (Invoke-BackupGit -Arguments @('rev-parse', 'HEAD')) -join ''
        $taskRemoteHead = @(Invoke-BackupGit -Arguments @('ls-remote', '--heads', 'origin', "refs/heads/$Branch"))
        if ($taskRemoteHead.Count -ne 1 -or ($taskRemoteHead[0] -split '\s+')[0] -ne $taskLocalCommit) {
            throw 'Le commit distant n a pas pu etre confirme. La sauvegarde reste a verifier.'
        }
        Write-Output "Sauvegarde GitHub confirmee : $taskLocalCommit"
        Write-Output "https://github.com/Meryem-data-science/OPPORTUNITY-RADAR-AI/tree/$Branch"
    } finally {
        Pop-Location
    }
} catch {
    Write-Output "Sauvegarde interrompue : $($_.Exception.Message)"
    exit 1
}
