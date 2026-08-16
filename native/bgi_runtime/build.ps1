param(
    [string]$ToolchainRoot = "",
    [string]$OutDir = "",
    [ValidateSet("x86", "x64")]
    [string]$Arch = "x86"
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent (Split-Path -Parent $ScriptDir)
if (-not $OutDir) {
    $OutDir = Join-Path $RepoRoot "assets\bgi_native_runtime\$Arch"
}

$Triplet = if ($Arch -eq "x64") { "x86_64-w64-mingw32" } else { "i686-w64-mingw32" }
$ArchHint = if ($Arch -eq "x64") { "x86_64" } else { "i686" }

if (-not $ToolchainRoot) {
    $candidates = @(
        (Join-Path $RepoRoot "native\toolchains\llvm-mingw-20260616-msvcrt-i686"),
        (Join-Path $RepoRoot "native\toolchains\llvm-mingw-20260616-ucrt-i686"),
        (Join-Path $RepoRoot "native\toolchains\llvm-mingw-20260616-msvcrt-x86_64"),
        (Join-Path $RepoRoot "native\toolchains\llvm-mingw-20260616-ucrt-x86_64")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path (Join-Path $candidate "bin\$Triplet-clang++.exe")) {
            $ToolchainRoot = $candidate
            break
        }
    }
}

$cxx = $null
if ($ToolchainRoot) {
    $candidate = Join-Path $ToolchainRoot "bin\$Triplet-clang++.exe"
    if (Test-Path $candidate) {
        $cxx = $candidate
    }
}
if (-not $cxx) {
    $cmd = Get-Command "$Triplet-clang++.exe" -ErrorAction SilentlyContinue
    if ($cmd) {
        $cxx = $cmd.Source
    }
}
if (-not $cxx) {
    throw "$ArchHint llvm-mingw compiler not found. Download llvm-mingw $ArchHint zip and extract it to native\toolchains, or pass -ToolchainRoot."
}

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$common = @(
    "-std=c++17",
    "-municode",
    "-O2",
    "-Wall",
    "-Wextra",
    "-static-libgcc",
    "-static-libstdc++"
)

& $cxx @common "-shared" "-o" (Join-Path $OutDir "bgi_native_hook.dll") (Join-Path $ScriptDir "bgi_native_hook.cpp") "-lgdi32" "-luser32"
if ($LASTEXITCODE -ne 0) {
    throw "Failed to build bgi_native_hook.dll"
}
& $cxx @common "-mwindows" "-o" (Join-Path $OutDir "bgi_native_launcher.exe") (Join-Path $ScriptDir "bgi_native_launcher.cpp") "-lshell32"
if ($LASTEXITCODE -ne 0) {
    throw "Failed to build bgi_native_launcher.exe"
}

Write-Host "Built BGI native runtime:"
Get-ChildItem -LiteralPath $OutDir -Filter "bgi_native_*" | Select-Object FullName, Length | Format-Table -AutoSize
