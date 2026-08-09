<#
.SYNOPSIS
    Links the repo's typings/ folder to the installed Fusion 360 API definitions.

.DESCRIPTION
    The Fusion API is not an installable package: it lives in
    %LOCALAPPDATA%\Autodesk\webdeploy\production\<build-hash>\, and that hash
    changes with every Fusion update.

    Rather than baking an absolute path (which would leak a username and go
    stale on each update) into the committed VS Code settings, this script
    creates a directory junction at

        <repo>\typings\adsk  ->  <fusion build>\Api\Python\packages\adsk\defs\adsk

    so .vscode/settings.json only ever refers to the relative path "./typings".
    typings/ is gitignored, so nothing machine-specific is committed.

    Run this once after cloning, and again after any Fusion update.
    No administrator rights are required - directory junctions do not need them.

.EXAMPLE
    pwsh -File tools\Refresh-FusionPaths.ps1
#>

[CmdletBinding()]
param(
    [string]$RepoRoot = (Join-Path $PSScriptRoot '..')
)

$ErrorActionPreference = 'Stop'

$RepoRoot = [System.IO.Path]::GetFullPath($RepoRoot)

$base = Join-Path $env:LOCALAPPDATA 'Autodesk\webdeploy\production'
if (-not (Test-Path $base)) {
    throw "Fusion webdeploy folder not found at $base. Is Fusion 360 installed for this user?"
}

# A usable build is one that actually carries the typed API definitions. The
# SWIG runtime copy under Api\Python\packages\adsk has no type annotations, so
# it is deliberately not what we link to.
$builds = Get-ChildItem -Path $base -Directory |
    Where-Object { Test-Path (Join-Path $_.FullName 'Api\Python\packages\adsk\defs\adsk\__init__.py') } |
    Sort-Object LastWriteTime -Descending

if (-not $builds) {
    throw "No Fusion build under $base contains Api\Python\packages\adsk\defs. Fusion may still be updating."
}

$build = $builds[0]
$target = Join-Path $build.FullName 'Api\Python\packages\adsk\defs\adsk'
$python = Join-Path $build.FullName 'Python\python.exe'

Write-Host "Fusion build   : $($build.Name)  (updated $($build.LastWriteTime))"
if (Test-Path $python) {
    Write-Host "Bundled Python : $(& $python --version 2>&1)"
}
if ($builds.Count -gt 1) {
    Write-Host "Note: $($builds.Count) builds present; using the most recently updated one." -ForegroundColor DarkYellow
}

$typings = Join-Path $RepoRoot 'typings'
$link = Join-Path $typings 'adsk'

if (-not (Test-Path $typings)) {
    New-Item -ItemType Directory -Path $typings -Force | Out-Null
}

if (Test-Path $link) {
    $item = Get-Item $link -Force
    if ($item.LinkType) {
        # Delete the reparse point only. Remove-Item -Recurse on a junction has
        # historically followed it into the target, which would damage the
        # Fusion install.
        $item.Delete()
    } else {
        throw "$link exists and is a real directory, not a junction. Move or delete it, then re-run."
    }
}

New-Item -ItemType Junction -Path $link -Target $target | Out-Null

$probe = Join-Path $link 'core.py'
if (-not (Test-Path $probe)) {
    throw "Junction created but $probe is not readable. Check the Fusion install."
}

Write-Host ""
Write-Host "Linked typings\adsk -> Fusion API definitions" -ForegroundColor Green
Write-Host "VS Code resolves these via the relative path './typings' in .vscode/settings.json,"
Write-Host "so no machine-specific path is ever committed."
Write-Host "Reload the window (Ctrl+Shift+P -> 'Developer: Reload Window') to pick it up."
