# Increment PerfPilot's release version as MAJOR.MINOR.0.
# Examples: 1.0.0 -> 1.1.0 -> 1.2.0

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VersionFile = Join-Path $Root "perfpilot\__init__.py"
$Text = Get-Content -LiteralPath $VersionFile -Raw -Encoding UTF8

if ($Text -notmatch '__version__\s*=\s*["''](\d+)\.(\d+)\.(\d+)["'']') {
    throw "Could not find a numeric __version__ in $VersionFile"
}

$Major = [int]$Matches[1]
$Minor = [int]$Matches[2]
$Next = "$Major.$($Minor + 1).0"
$Replacement = '__version__ = "' + $Next + '"'
$Updated = [regex]::Replace($Text, '__version__\s*=\s*["'']\d+\.\d+\.\d+["'']', $Replacement, 1)
Set-Content -LiteralPath $VersionFile -Value $Updated -Encoding UTF8

Write-Host "Version bumped to $Next"
