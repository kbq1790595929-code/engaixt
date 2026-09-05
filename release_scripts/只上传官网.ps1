param(
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

function New-TextFromCodepoints([int[]]$Codepoints) {
    return -join ($Codepoints | ForEach-Object { [string][char]$_ })
}

$internalName = New-TextFromCodepoints @(0x5185, 0x90E8, 0x811A, 0x672C)
$uploadName = (New-TextFromCodepoints @(0x5FEB, 0x901F, 0x4E0A, 0x4F20, 0x5B98, 0x7F51)) + ".ps1"
$upload = Join-Path (Join-Path $PSScriptRoot $internalName) $uploadName
$releaseFolderName = "EngAixt" + (New-TextFromCodepoints @(0x53D1, 0x5E03, 0x5305))
$deployFolderName = New-TextFromCodepoints @(0x5B98, 0x7F51, 0x4E0A, 0x4F20, 0x76EE, 0x5F55)
$deployDir = Join-Path (Join-Path "C:\games" $releaseFolderName) $deployFolderName

$args = @("-ExecutionPolicy", "Bypass", "-File", $upload, "-DeployDir", $deployDir)
if ($DryRun) { $args += "-DryRun" }
& powershell @args
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
