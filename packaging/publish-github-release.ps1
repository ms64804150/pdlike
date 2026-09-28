# Build the Windows package and publish it to the GitHub Release matching
# perfpilot.__version__. Run this only after committing and pushing the version.
# Usage: powershell -ExecutionPolicy Bypass -File .\packaging\publish-github-release.ps1

[CmdletBinding()]
param(
    [switch]$SkipBuild,
    [switch]$Draft
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root
$Repository = "ms64804150/pdlike"

function Invoke-Checked {
    param([string]$File, [string[]]$Arguments)
    Write-Host ("+ {0} {1}" -f $File, ($Arguments -join " "))
    & $File @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $File"
    }
}

$Gh = Get-Command gh -ErrorAction SilentlyContinue
if (-not $Gh) {
    throw "GitHub CLI (gh) is required. Install it with: winget install --id GitHub.cli  Then run: gh auth login"
}
Invoke-Checked $Gh.Source @("auth", "status", "--hostname", "github.com")

$VersionFile = Join-Path $Root "perfpilot\__init__.py"
$VersionText = Get-Content -LiteralPath $VersionFile -Raw -Encoding UTF8
if ($VersionText -notmatch '__version__\s*=\s*["'']([^"'']+)["'']') {
    throw "Could not find __version__ in $VersionFile"
}
$Version = $Matches[1]
$Tag = "v$Version"
$Asset = Join-Path $Root "dist\PerfPilot-portable-x64.zip"

$WorkingTree = git status --porcelain
if ($LASTEXITCODE -ne 0) { throw "Unable to read git status." }
if ($WorkingTree) {
    throw "Working tree is not clean. Commit or stash changes before publishing.\n$WorkingTree"
}
Invoke-Checked "git" @("fetch", "origin", "main")
$LocalHead = (git rev-parse HEAD).Trim()
$RemoteHead = (git rev-parse origin/main).Trim()
if ($LocalHead -ne $RemoteHead) {
    throw "Current commit has not been pushed to origin/main. Run: git push origin main"
}

if (-not $SkipBuild) {
    Invoke-Checked "powershell" @("-ExecutionPolicy", "Bypass", "-File", (Join-Path $Root "packaging\build.ps1"))
}
if (-not (Test-Path -LiteralPath $Asset)) {
    throw "Release asset is missing: $Asset"
}

$ReleaseExists = $true
$PreviousErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    & $Gh.Source release view $Tag --repo $Repository 2>$null
    $ReleaseExists = ($LASTEXITCODE -eq 0)
} finally {
    $ErrorActionPreference = $PreviousErrorActionPreference
}
if ($ReleaseExists) {
    Invoke-Checked $Gh.Source @("release", "upload", $Tag, $Asset, "--repo", $Repository, "--clobber")
    Write-Host "Updated GitHub Release $Tag with $Asset"
} else {
    $CreateArgs = @("release", "create", $Tag, $Asset, "--repo", $Repository, "--title", "PerfPilot $Tag", "--generate-notes")
    if ($Draft) { $CreateArgs += "--draft" }
    Invoke-Checked $Gh.Source $CreateArgs
    Write-Host "Created GitHub Release $Tag with $Asset"
}
Write-Host "Release URL: https://github.com/$Repository/releases/tag/$Tag"
