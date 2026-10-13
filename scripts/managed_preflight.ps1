# Public fallback only. Existing host-managed preflight takes precedence.
# Sources: Microsoft Learn Checkpoint-Computer / Get-ComputerRestorePoint (PowerShell 5.1).
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][ValidateSet('gemini-cli-subagent')][string]$ActionName,
    [Parameter(Mandatory=$true)][ValidateNotNullOrEmpty()][string[]]$PlannedCommands,
    [string[]]$IncludePaths = @()
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

function Write-PreflightText {
    param([string]$Path, [string]$Text)
    [IO.File]::WriteAllText($Path, $Text, (New-Object Text.UTF8Encoding($false)))
}

function Get-PreflightFileHash {
    param([string]$Path)
    $stream = [IO.File]::OpenRead($Path)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($algorithm.ComputeHash($stream)).Replace('-', '') }
    finally { $algorithm.Dispose(); $stream.Dispose() }
}

function Get-PreflightRestoreEvidence {
    param([object[]]$Points, [datetime]$Now, [string]$Description, [object[]]$BeforeSequences)
    $recent = $false
    $created = $false
    $sequences = @()
    foreach ($point in $Points) {
        try {
            if ($point.CreationTime -is [datetime]) { $when = $point.CreationTime.ToUniversalTime() }
            else { $when = [System.Management.ManagementDateTimeConverter]::ToDateTime([string]$point.CreationTime).ToUniversalTime() }
            if ($when -le $Now.ToUniversalTime() -and $when -ge $Now.ToUniversalTime().AddDays(-7)) {
                $recent = $true
                $sequences += [int]$point.SequenceNumber
                if ($point.Description -eq $Description -and $BeforeSequences -notcontains $point.SequenceNumber) { $created = $true }
            }
        } catch { } # Unparseable or future records are never evidence of a valid restore point.
    }
    return [pscustomobject]@{ CreatedNow = $created; Recent = $recent; Sequences = $sequences }
}

function Copy-PreflightPath {
    param([string]$Source, [string]$Destination, [string]$Receipt)
    $item = Get-Item -LiteralPath $Source -Force -ErrorAction Stop
    if ($item.PSProvider.Name -ne 'FileSystem') { throw 'Backup source must use the filesystem provider.' }
    $sourceRoot = $item.FullName.TrimEnd('\')
    $receiptRoot = [IO.Path]::GetFullPath($Receipt).TrimEnd('\')
    if ($receiptRoot.Equals($sourceRoot, [StringComparison]::OrdinalIgnoreCase) -or
        $receiptRoot.StartsWith($sourceRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Backup source includes its own receipt destination.'
    }
    if ($item.FullName.TrimEnd('\') -eq [IO.Path]::GetPathRoot($item.FullName).TrimEnd('\')) { throw 'Backing up an entire volume is not supported.' }
    $children = @()
    if ($item.PSIsContainer) { $children = @(Get-ChildItem -LiteralPath $item.FullName -Recurse -Force -ErrorAction Stop) }
    foreach ($entry in @($item) + $children) {
        if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Reparse points are not supported in preflight backups.' }
    }
    $files = @($item)
    if ($item.PSIsContainer) { $files = @($children | Where-Object { -not $_.PSIsContainer }) }
    $records = @()
    foreach ($file in $files) {
        $relative = ''
        if ($item.PSIsContainer) { $relative = $file.FullName.Substring($sourceRoot.Length + 1) }
        $records += [pscustomobject]@{ RelativePath = $relative; SHA256 = (Get-PreflightFileHash $file.FullName) }
    }
    Copy-Item -LiteralPath $item.FullName -Destination $Destination -Recurse -Force -ErrorAction Stop
    foreach ($record in $records) {
        $original = $item.FullName
        $copy = $Destination
        if ($item.PSIsContainer) { $original = Join-Path $sourceRoot $record.RelativePath; $copy = Join-Path $Destination $record.RelativePath }
        if ((Get-PreflightFileHash $original) -ne $record.SHA256 -or
            (Get-PreflightFileHash $copy) -ne $record.SHA256) { throw 'Backup verification failed or the source changed during backup.' }
    }
    if ($item.PSIsContainer) {
        $after = @(Get-ChildItem -LiteralPath $item.FullName -Recurse -Force -ErrorAction Stop | Where-Object { -not $_.PSIsContainer })
        if ($after.Count -ne $records.Count) { throw 'Backup source changed during backup.' }
    }
    return [pscustomobject]@{ Source = $item.FullName; Backup = $Destination; Status = 'verified'; Files = $records }
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Managed preflight requires an administrator PowerShell process.' }
$profileRoot = [Environment]::GetFolderPath('UserProfile')
$receipt = Join-Path $profileRoot ('.codex\rollback\' + [datetime]::UtcNow.ToString('yyyyMMdd-HHmmss-fffffff') + '-' + $ActionName)
$null = New-Item -ItemType Directory -Path (Join-Path $receipt 'files') -Force
$stage = 'initialization'
$manifest = @()
try {
    Write-PreflightText (Join-Path $receipt 'planned-commands.txt') (($PlannedCommands -join [Environment]::NewLine) + [Environment]::NewLine)
    Write-PreflightText (Join-Path $receipt 'system-state.txt') (
        'Timestamp: ' + [datetime]::UtcNow.ToString('o') + "`nActionName: " + $ActionName +
        "`nAdministrator: True`nPowerShell: " + $PSVersionTable.PSVersion.ToString() + "`nImplementation: bundled-native-Windows-preflight`n")
    $stage = 'restore_point_read'
    $before = @(Get-ComputerRestorePoint -ErrorAction Stop)
    $beforeSequences = @($before | ForEach-Object { $_.SequenceNumber })
    $description = 'Antigravity subagent preflight ' + [datetime]::UtcNow.ToString('o')
    $stage = 'restore_point_create'
    try { Checkpoint-Computer -Description $description -RestorePointType MODIFY_SETTINGS -ErrorAction Stop -WarningAction SilentlyContinue }
    catch { Write-PreflightText (Join-Path $receipt 'checkpoint-attempt.txt') ('Creation attempt failed: ' + $_.Exception.GetType().Name + "`nNo system restore settings were changed.`n") }
    $stage = 'restore_point_verify'
    $after = @(Get-ComputerRestorePoint -ErrorAction Stop)
    $evidence = Get-PreflightRestoreEvidence $after ([datetime]::UtcNow) $description $beforeSequences
    if (-not $evidence.CreatedNow -and -not $evidence.Recent) { throw 'No newly created or verified restore point within seven days; preflight stopped.' }
    $stage = 'file_backups'
    $seen = @{}
    $index = 0
    foreach ($source in $IncludePaths) {
        if ([string]::IsNullOrWhiteSpace($source)) { throw 'Backup paths must be nonempty.' }
        $absolute = [IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($source))
        if ($seen.ContainsKey($absolute)) { continue }
        $seen[$absolute] = $true
        $index++
        if (-not (Test-Path -LiteralPath $absolute)) { $manifest += [pscustomobject]@{ Source = $absolute; Status = 'absent_at_preflight' }; continue }
        $leaf = [IO.Path]::GetFileName($absolute.TrimEnd('\')) -replace '[^A-Za-z0-9._-]', '_'
        $destination = Join-Path (Join-Path $receipt 'files') (('{0:D4}-' -f $index) + $leaf)
        $manifest += Copy-PreflightPath $absolute $destination $receipt
    }
    Write-PreflightText (Join-Path $receipt 'backup-manifest.json') (ConvertTo-Json -InputObject @($manifest) -Depth 8)
    $stage = 'receipt_finalize'
    Write-PreflightText (Join-Path $receipt 'ROLLBACK.md') @'
# Private rollback evidence

This directory may contain OAuth credentials, account files and private configuration. Keep it local; never upload it to GitHub, include it in prompts, or share its contents.

No rollback is executed automatically. Verify the current native CLI identity and manager selection first. Restore an account through Antigravity Tools' native agy switch and verify credential, device-profile and file-state postconditions. Filesystem copies do not export or independently restore the Windows Credential Manager identity.

Inspect backup-manifest.json and the source state before any file restoration. Preserve concurrent edits. Missing-at-preflight paths have no backup and must not be deleted automatically. This script does not close programs, restore Windows automatically, change restore settings, or alter proxy, services, registry or drivers. A system restore is a separate user-directed operation.

Microsoft documentation:
https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.management/checkpoint-computer?view=powershell-5.1
https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.management/get-computerrestorepoint?view=powershell-5.1
'@
    # Final success marker. Any earlier error leaves this file absent, so the helper refuses the receipt.
    Write-PreflightText (Join-Path $receipt 'restore-point.pending') (
        'RestorePointCreatedNow: ' + $evidence.CreatedNow.ToString() + "`nHasRecentRestorePointWithin7Days: " +
        $evidence.Recent.ToString() + "`nVerifiedSequenceNumbers: " + ($evidence.Sequences -join ',') + "`nBackupsCompleted: True`n")
    [IO.File]::Move((Join-Path $receipt 'restore-point.pending'), (Join-Path $receipt 'restore-point.txt'))
} catch {
    try {
        Write-PreflightText (Join-Path $receipt 'FAILED.txt') ('Stage: ' + $stage + "`nExceptionType: " + $_.Exception.GetType().Name + "`nNo success receipt issued; original data was not deleted. Partial backups remain private.`n")
        Write-PreflightText (Join-Path $receipt 'backup-manifest.partial.json') (ConvertTo-Json -InputObject @($manifest) -Depth 8)
    } catch { }
    throw ('Managed preflight failed at ' + $stage + '; private failure evidence: ' + $receipt)
}
Write-Output $receipt
