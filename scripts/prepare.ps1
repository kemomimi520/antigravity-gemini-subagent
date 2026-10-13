[CmdletBinding()]
param([switch]$Elevated, [string]$ManagerDir, [string]$SettingsFile)

$ErrorActionPreference = 'Stop'
$profileRoot = [Environment]::GetFolderPath('UserProfile')
if (-not $ManagerDir) {
    $ManagerDir = if ($env:AGY_SUBAGENT_MANAGER_DIR) { $env:AGY_SUBAGENT_MANAGER_DIR } else { Join-Path $profileRoot '.antigravity_tools' }
}
if (-not $SettingsFile) {
    $SettingsFile = if ($env:AGY_SUBAGENT_SETTINGS) { $env:AGY_SUBAGENT_SETTINGS } else { Join-Path $profileRoot '.gemini\antigravity-cli\settings.json' }
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
$isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    if ($Elevated) { throw 'gsudo did not provide an elevated process.' }
    $gsudo = (Get-Command gsudo -ErrorAction Stop).Source
    $elevationArgs = @('powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $PSCommandPath, '-Elevated')
    if ($ManagerDir) { $elevationArgs += @('-ManagerDir', $ManagerDir) }
    if ($SettingsFile) { $elevationArgs += @('-SettingsFile', $SettingsFile) }
    & $gsudo @elevationArgs
    exit $LASTEXITCODE
}

$preflight = Join-Path $profileRoot '.codex\managed-preflight.ps1'
if (-not (Test-Path -LiteralPath $preflight -PathType Leaf)) {
    $preflight = Join-Path $PSScriptRoot 'managed_preflight.ps1'
    if (-not (Test-Path -LiteralPath $preflight -PathType Leaf)) {
        throw 'Neither a host managed preflight nor the bundled Windows preflight was found.'
    }
}
$backupPaths = @(
    '.codex\skills\antigravity-gemini-subagent',
    '.gemini\oauth_creds.json',
    '.gemini\google_accounts.json',
    '.gemini\antigravity-cli\settings.json',
    '.gemini\antigravity-cli\antigravity-oauth-token',
    '.gemini\antigravity-cli\jetski_state.pbtxt'
) | ForEach-Object { Join-Path $profileRoot $_ }
$helper = Join-Path $PSScriptRoot 'workflow.py'
$discoveryCode = @'
import importlib.util,json,sys
spec=importlib.util.spec_from_file_location('w',sys.argv[1])
worker=importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)
paths=worker.sibling_module('runtime_paths')
manager=worker.Manager(sys.argv[2] or None)
settings=paths.settings_path(sys.argv[3] or None)
print(json.dumps([str(manager.directory),str(settings),*[str(path.parent) for path in worker.fingerprint_storage_paths(manager)]]))
'@
$fingerprintJson = & python -B -c $discoveryCode $helper $ManagerDir $SettingsFile
if ($LASTEXITCODE -ne 0) { throw 'Native fingerprint path discovery failed before preflight.' }
$discoveredPaths = $fingerprintJson | ConvertFrom-Json
foreach ($discoveredPath in $discoveredPaths) {
    $backupPaths += [string]$discoveredPath
}
$backupPaths = @($backupPaths | Select-Object -Unique)
& $preflight -ActionName 'gemini-cli-subagent' `
    -PlannedCommands @(
        'Use installed agy for bounded Gemini worker tasks in the caller-authorized workspace',
        'Update the installed Gemini subagent skill only after staged regression and real CLI verification; preserve a reversible source snapshot',
        'Refresh existing account quotas through the local Antigravity Tools manager when needed',
        'Use headless manager for quota refresh and native desktop backend for switching when explicitly requested; no simulated clicks',
        'Invoke the installed manager native one-click POST /api/accounts/switch with explicit targetIde agy; verify OS/file credentials and bound fingerprint storage/database',
        'Preserve account metadata and restore a failed completed switch; leave a submitted switch with unknown outcome running for inspection',
        'Do not upgrade software, modify system proxy or permissions, or close IDE/2.0 without task authorization'
    ) -IncludePaths $backupPaths
