param(
    [string]$ToolchainRoot = "",
    [string]$OutDir = ""
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent (Split-Path -Parent $ScriptDir)
if (-not $OutDir) {
    $OutDir = Join-Path $RepoRoot "assets\kirikiri_native_runtime\x86"
}

if (-not $ToolchainRoot) {
    $candidates = @(
        (Join-Path $RepoRoot "native\toolchains\llvm-mingw-20260616-msvcrt-i686"),
        (Join-Path $RepoRoot "native\toolchains\llvm-mingw-20260616-ucrt-i686")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path (Join-Path $candidate "bin\i686-w64-mingw32-clang++.exe")) {
            $ToolchainRoot = $candidate
            break
        }
    }
}

$cxx = $null
if ($ToolchainRoot) {
    $candidate = Join-Path $ToolchainRoot "bin\i686-w64-mingw32-clang++.exe"
    if (Test-Path $candidate) {
        $cxx = $candidate
    }
}
if (-not $cxx) {
    $cmd = Get-Command i686-w64-mingw32-clang++.exe -ErrorAction SilentlyContinue
    if ($cmd) {
        $cxx = $cmd.Source
    }
}
if (-not $cxx) {
    throw "i686 llvm-mingw compiler not found. Download llvm-mingw i686 zip and extract it to native\toolchains, or pass -ToolchainRoot."
}

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$ResolvedOutDir = (Resolve-Path -LiteralPath $OutDir).Path
$BuildTag = [Guid]::NewGuid().ToString("N")

function Publish-BuildArtifact {
    param(
        [Parameter(Mandatory=$true)][string]$TempPath,
        [Parameter(Mandatory=$true)][string]$FinalPath
    )

    if (-not (Test-Path -LiteralPath $TempPath)) {
        throw "Build artifact was not created: $TempPath"
    }

    $finalParent = Split-Path -Parent $FinalPath
    $resolvedParent = (Resolve-Path -LiteralPath $finalParent).Path
    if ($resolvedParent -ne $ResolvedOutDir) {
        throw "Refusing to publish outside output directory: $FinalPath"
    }

    $backupPath = "$FinalPath.old-$BuildTag"
    if (Test-Path -LiteralPath $FinalPath) {
        Move-Item -LiteralPath $FinalPath -Destination $backupPath -Force
    }
    try {
        Move-Item -LiteralPath $TempPath -Destination $FinalPath -Force
    } catch {
        if ((Test-Path -LiteralPath $backupPath) -and -not (Test-Path -LiteralPath $FinalPath)) {
            Move-Item -LiteralPath $backupPath -Destination $FinalPath -Force
        }
        throw
    }
    if (Test-Path -LiteralPath $backupPath) {
        Remove-Item -LiteralPath $backupPath -Force
    }
}

$common = @(
    "-std=c++17",
    "-municode",
    "-fms-extensions",
    "-O2",
    "-Wall",
    "-Wextra",
    "-static-libgcc",
    "-static-libstdc++"
)

$hookTemp = Join-Path $OutDir "kirikiri_native_hook.$BuildTag.tmp.dll"
$hookFinal = Join-Path $OutDir "kirikiri_native_hook.dll"
& $cxx @common "-shared" "-o" $hookTemp `
    (Join-Path $ScriptDir "kirikiri_native_hook.cpp") `
    (Join-Path $ScriptDir "kirikiri_embed_runtime.cpp") `
    (Join-Path $ScriptDir "kirikiri_embed_trace.cpp") `
    (Join-Path $ScriptDir "kirikiri_patch_stream.cpp") `
    (Join-Path $ScriptDir "kirikiri_psb_runtime.cpp") `
    "-lgdi32" "-luser32"
if ($LASTEXITCODE -ne 0) {
    throw "Failed to build kirikiri_native_hook.dll"
}
Publish-BuildArtifact -TempPath $hookTemp -FinalPath $hookFinal

$launcherTemp = Join-Path $OutDir "kirikiri_native_launcher.$BuildTag.tmp.exe"
$launcherFinal = Join-Path $OutDir "kirikiri_native_launcher.exe"
& $cxx @common "-mwindows" "-o" $launcherTemp (Join-Path $ScriptDir "kirikiri_native_launcher.cpp") "-lshell32"
if ($LASTEXITCODE -ne 0) {
    throw "Failed to build kirikiri_native_launcher.exe"
}
Publish-BuildArtifact -TempPath $launcherTemp -FinalPath $launcherFinal

Write-Host "Built KiriKiri native runtime:"
Get-ChildItem -LiteralPath $OutDir -Filter "kirikiri_native_*" | Select-Object FullName, Length | Format-Table -AutoSize
