param(
    [string]$ZigExe = "",
    [switch]$KeepBuildTree
)

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false
$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$upstream = Join-Path $repoRoot "vendor\uberwolf"
$adapter = Join-Path $PSScriptRoot "engaixt_wolf_native.cpp"
$patch = Join-Path $PSScriptRoot "patches\uberwolf-custom-key-pack.patch"
$output = Join-Path $repoRoot "assets\wolf_runtime\engaixt_wolf_native.exe"

if (-not (Test-Path -LiteralPath $upstream)) {
    throw "UberWolf submodule is missing. Run: git submodule update --init --recursive"
}
if (-not (Test-Path -LiteralPath $adapter) -or -not (Test-Path -LiteralPath $patch)) {
    throw "WOLF native source or patch is missing"
}
if (-not $ZigExe) {
    $candidate = Get-Command zig.exe -ErrorAction SilentlyContinue
    if ($candidate) {
        $ZigExe = $candidate.Source
    }
}
if (-not $ZigExe -or -not (Test-Path -LiteralPath $ZigExe)) {
    throw "Set -ZigExe to a Zig 0.15+ executable"
}

$buildRoot = Join-Path $env:TEMP "engaixt_wolf_native_build"
if (Test-Path -LiteralPath $buildRoot) {
    Remove-Item -LiteralPath $buildRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $buildRoot | Out-Null

Copy-Item -Path (Join-Path $upstream "*") -Destination $buildRoot -Recurse -Force
& git apply --unsafe-paths --directory=$buildRoot $patch
if ($LASTEXITCODE -ne 0) {
    throw "Could not apply the pinned UberWolf custom-key encoder patch"
}

$sources = @(
    $adapter,
    (Join-Path $buildRoot "3rdParty\DXLib\CharCode.cpp"),
    (Join-Path $buildRoot "3rdParty\DXLib\CharCodeTable.cpp"),
    (Join-Path $buildRoot "3rdParty\DXLib\DXArchive.cpp"),
    (Join-Path $buildRoot "3rdParty\DXLib\DXArchiveVer5.cpp"),
    (Join-Path $buildRoot "3rdParty\DXLib\DXArchiveVer6.cpp"),
    (Join-Path $buildRoot "3rdParty\DXLib\FileLib.cpp"),
    (Join-Path $buildRoot "3rdParty\DXLib\Huffman.cpp"),
    (Join-Path $buildRoot "UberWolfLib\Localizer.cpp"),
    (Join-Path $buildRoot "UberWolfLib\UberLog.cpp"),
    (Join-Path $buildRoot "UberWolfLib\WolfDec.cpp"),
    (Join-Path $buildRoot "UberWolfLib\WolfUtils.cpp")
)

$lz4Object = Join-Path $buildRoot "lz4.o"
& $ZigExe cc -O2 -c (Join-Path $buildRoot "3rdParty\lz4\lz4.c") -o $lz4Object
if ($LASTEXITCODE -ne 0) {
    throw "WOLF native LZ4 compilation failed"
}

& $ZigExe c++ -std=c++20 -O2 -DUNICODE -D_UNICODE `
    "-I$buildRoot" "-I$(Join-Path $buildRoot '3rdParty')" `
    $sources $lz4Object -municode -luser32 -lshell32 -lole32 -loleaut32 -lcomctl32 -ladvapi32 -lversion `
    -o $output
if ($LASTEXITCODE -ne 0) {
    throw "WOLF native bridge compilation failed"
}

$pdb = [System.IO.Path]::ChangeExtension($output, ".pdb")
Remove-Item -LiteralPath $pdb -Force -ErrorAction SilentlyContinue

if (-not $KeepBuildTree) {
    Remove-Item -LiteralPath $buildRoot -Recurse -Force
}

Write-Output "Built $output"
