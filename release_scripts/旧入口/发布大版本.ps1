param(
    [switch]$DryRun,
    [switch]$SkipTests,
    [switch]$SkipBuild,
    [switch]$TrialOnly,
    [switch]$ProOnly
)

$ErrorActionPreference = "Stop"
$helper = Join-Path $PSScriptRoot "engaixt_release_version_helper.ps1"
$params = @{ Bump = "major"; Kind = "major" }
if ($DryRun) { $params.DryRun = $true }
if ($SkipTests) { $params.SkipTests = $true }
if ($SkipBuild) { $params.SkipBuild = $true }
if ($TrialOnly) { $params.TrialOnly = $true }
if ($ProOnly) { $params.ProOnly = $true }
& $helper @params
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
