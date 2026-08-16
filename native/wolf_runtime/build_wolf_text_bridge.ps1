param(
    [Parameter(Mandatory = $true)]
    [string]$UberWolfSource,
    [Parameter(Mandatory = $true)]
    [string]$ZigExe,
    [string]$Output = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not $Output) {
    $Output = Join-Path $root "assets\wolf_runtime\wolf_text_bridge.exe"
}

$source = Join-Path $PSScriptRoot "wolf_text_bridge.cpp"
$lz4 = Join-Path $UberWolfSource "3rdParty\lz4\lz4.c"
$includeLib = Join-Path $UberWolfSource "UberWolfLib"
$includeThirdParty = Join-Path $UberWolfSource "3rdParty"
$buildDir = Join-Path $env:TEMP "engaixt-wolf-runtime-build"
$lz4Object = Join-Path $buildDir "lz4.o"

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Output) | Out-Null
New-Item -ItemType Directory -Force -Path $buildDir | Out-Null
& $ZigExe cc -O2 -c $lz4 -o $lz4Object
if ($LASTEXITCODE -ne 0) {
    throw "LZ4 build failed with exit code $LASTEXITCODE"
}

& $ZigExe c++ `
    -std=c++20 `
    -O2 `
    -g0 `
    -D_UNICODE `
    -DUNICODE `
    "-I$includeLib" `
    "-I$includeThirdParty" `
    $source `
    $lz4Object `
    -lshell32 `
    -o $Output

if ($LASTEXITCODE -ne 0) {
    throw "wolf_text_bridge build failed with exit code $LASTEXITCODE"
}

Write-Host "Built $Output"
