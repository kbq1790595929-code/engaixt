param(
    [switch]$SkipTests,
    [switch]$SkipBuild,
    [switch]$NoUpload,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

function New-TextFromCodepoints([int[]]$Codepoints) {
    return -join ($Codepoints | ForEach-Object { [string][char]$_ })
}

$mainName = (New-TextFromCodepoints @(0x4E00, 0x952E, 0x6253, 0x5305, 0x5168, 0x90E8, 0x53D1, 0x5E03, 0x6587, 0x4EF6)) + ".ps1"
$main = Join-Path $PSScriptRoot $mainName
$params = @{ Bump = "patch"; Kind = "small" }
if ($SkipTests) { $params.SkipTests = $true }
if ($SkipBuild) { $params.SkipBuild = $true }
if ($NoUpload) { $params.NoUpload = $true }
if ($DryRun) { $params.DryRun = $true }
& $main @params
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
