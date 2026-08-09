<#
.SYNOPSIS
    Links this repo to the installed Fusion 360 Python API and interpreter.

.DESCRIPTION
    The Fusion API is not an installable package: it lives in
    %LOCALAPPDATA%\Autodesk\webdeploy\production\<build-hash>\, and that hash
    changes with every Fusion update. Older builds are gutted in place, so any
    absolute path baked into VS Code settings goes stale silently.

    Rather than committing such a path (which would also leak a username), this
    script creates two directory junctions:

        <repo>\typings\adsk      -> <build>\Api\Python\packages\adsk\defs\adsk
        <repo>\.fusion-python    -> <build>\Python

    so .vscode/settings.json only ever refers to the relative paths
    "./typings" and "${workspaceFolder}/.fusion-python/python.exe". Both link
    folders are gitignored, so nothing machine-specific is committed.

    The interpreter junction is deliberately kept out of typings/, because that
    folder is a Pylance analysis root and a "python" folder inside it would be
    indexed as a package.

    Run once after cloning, and again after any Fusion update.
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

function Set-Junction {
    param([string]$Link, [string]$Target)

    $parent = Split-Path $Link -Parent
    if (-not (Test-Path $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    if (Test-Path $Link) {
        $item = Get-Item $Link -Force
        if ($item.LinkType) {
            # Delete the reparse point only. Remove-Item -Recurse on a junction
            # has historically followed it into the target, which would damage
            # the Fusion install.
            $item.Delete()
        } else {
            throw "$Link exists and is a real directory, not a junction. Move or delete it, then re-run."
        }
    }
    New-Item -ItemType Junction -Path $Link -Target $Target | Out-Null
}

$base = Join-Path $env:LOCALAPPDATA 'Autodesk\webdeploy\production'
if (-not (Test-Path $base)) {
    throw "Fusion webdeploy folder not found at $base. Is Fusion 360 installed for this user?"
}

# A usable build carries both the typed API definitions and the interpreter.
# Updated builds gut the previous one, so presence is what matters, not date.
$builds = Get-ChildItem -Path $base -Directory | Where-Object {
    (Test-Path (Join-Path $_.FullName 'Api\Python\packages\adsk\defs\adsk\__init__.py')) -and
    (Test-Path (Join-Path $_.FullName 'Python\python.exe'))
} | Sort-Object LastWriteTime -Descending

if (-not $builds) {
    throw "No Fusion build under $base has both Api\Python\packages\adsk\defs and Python\python.exe. Fusion may still be updating."
}

$build = $builds[0]
$defs = Join-Path $build.FullName 'Api\Python\packages\adsk\defs\adsk'
$python = Join-Path $build.FullName 'Python'
$exe = Join-Path $python 'python.exe'

Write-Host "Fusion build   : $($build.Name)  (updated $($build.LastWriteTime))"
Write-Host "Bundled Python : $(& $exe --version 2>&1)"
if ($builds.Count -gt 1) {
    Write-Host "Note: $($builds.Count) usable builds present; using the most recently updated one." -ForegroundColor DarkYellow
}

Set-Junction -Link (Join-Path $RepoRoot 'typings\adsk') -Target $defs
Set-Junction -Link (Join-Path $RepoRoot '.fusion-python') -Target $python

foreach ($probe in @((Join-Path $RepoRoot 'typings\adsk\core.py'),
                     (Join-Path $RepoRoot '.fusion-python\python.exe'))) {
    if (-not (Test-Path $probe)) {
        throw "Junction created but $probe is not readable. Check the Fusion install."
    }
}

Write-Host ""
Write-Host "Linked typings\adsk    -> Fusion API definitions" -ForegroundColor Green
Write-Host "Linked .fusion-python  -> Fusion's bundled interpreter" -ForegroundColor Green
Write-Host ""
Write-Host "Reload the window (Ctrl+Shift+P -> 'Developer: Reload Window')."
Write-Host "If VS Code does not switch interpreters, run 'Python: Select Interpreter'"
Write-Host "and choose the one under .fusion-python - an interpreter already chosen"
Write-Host "for this workspace overrides python.defaultInterpreterPath."
