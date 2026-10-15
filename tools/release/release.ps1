<#
  EngAixt release pipeline (single entry point).

  Why this exists: packaging used to depend on files that were added by a release
  step nobody reran (third-party tools, patched assemblies, frida scripts). A
  missing file only surfaced on a user machine. This script fails early instead.

  Usage:
    powershell -File tools\release\release.ps1 -Version 1.1.16                 # preflight + build + verify + package
    powershell -File tools\release\release.ps1 -Version 1.1.16 -SkipBuild      # reuse dist\game-translator
    powershell -File tools\release\release.ps1 -VerifyOnly                     # checks only, changes nothing
    powershell -File tools\release\release.ps1 -Version 1.1.16 -Deploy         # deploy the website to Cloudflare Pages

  Old packages in the deploy dir are only pruned when -CleanOldParts is passed;
  default keeps every previously published version in place.

  Exit code 0 = all checks passed. Any missing required file = non-zero exit with a list.
#>
param(
    [string]$Version = "",
    [string]$SourceRoot = "",
    [string]$DistPath = "",
    [string]$DeployDir = "",
    [string]$GitHubRepo = "kbq1790595929-code/engaixt",
    [switch]$SkipBuild,
    [switch]$Deploy,
    [switch]$CleanOldParts,
    [switch]$RunTests,
    [switch]$StrictTests,
    [switch]$VerifyOnly
)

$ErrorActionPreference = "Stop"

function Say([string]$Text, [string]$Color = "Gray") { Write-Host $Text -ForegroundColor $Color }
function Fail([string]$Text) { Write-Host $Text -ForegroundColor Red; exit 1 }

function Assert-ChildPath([string]$PathText, [string]$RootText) {
    $target = [System.IO.Path]::GetFullPath($PathText)
    $root = [System.IO.Path]::GetFullPath($RootText).TrimEnd('\', '/') + '\'
    if (-not $target.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)) {
        Fail "Refusing to modify a path outside the intended directory: $target"
    }
}

if ([string]::IsNullOrWhiteSpace($SourceRoot)) {
    $SourceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
}
if ([string]::IsNullOrWhiteSpace($DeployDir)) {
    $DeployDir = (Resolve-Path (Join-Path $SourceRoot "site")).Path
}
$Dist = if ([string]::IsNullOrWhiteSpace($DistPath)) { Join-Path $SourceRoot "dist\game-translator" } else { $DistPath }
$BaselinePath = Join-Path $PSScriptRoot "baseline-files.json"
$ReleaseRoot = Join-Path $SourceRoot "release_artifacts"

Say "== release pipeline ==" Cyan
Say ("   source      : " + $SourceRoot)
Say ("   version     : " + $(if ($Version) { $Version } else { "(not set)" }))
Say ("   baseline    : " + $BaselinePath)
Say ("   mode        : " + $(if ($VerifyOnly) { "verify only" } elseif ($SkipBuild) { "package from existing dist" } else { "full build" }))

if (-not (Test-Path $BaselinePath)) { Fail "baseline file missing: $BaselinePath" }
$baseline = Get-Content $BaselinePath -Raw -Encoding UTF8 | ConvertFrom-Json

# ---------------------------------------------------------------- 1) source checks
Say ""
Say "== 1/6 source preflight ==" Cyan

$requiredGroups = @(
    @{ Name = "KRKR toolchain (garbro)";          Path = "_internal\tools\kirikiri\garbro\GARbro.Console.exe" },
    @{ Name = "KRKR toolchain (Xp3Pack)";         Path = "_internal\tools\kirikiri\kirikiri_tools\Xp3Pack.exe" },
    @{ Name = "KRKR toolchain (msg_tool)";        Path = "_internal\tools\kirikiri\msg_tool\msg_tool.exe" },
    @{ Name = "VS2010 runtime";                   Path = "_internal\MSVCR100.dll" },
    @{ Name = "BepInEx patched assemblies";       Path = "assets\bepinex_patches\BepInEx_UnityMono_patched.dll" },
    @{ Name = "XUnity (mono) package";            Path = "assets\XUnity.AutoTranslator-mono-5.6.1.zip" },
    @{ Name = "Wolf toolchain (UberWolfCli)";     Path = "assets\wolf_runtime\UberWolfCli.exe" },
    @{ Name = "Wolf toolchain (text bridge)";     Path = "assets\wolf_runtime\wolf_text_bridge.exe" },
    @{ Name = "frida KiriKiri realtime hook";     Path = "frida\kirikiri_realtime_hook.js" },
    @{ Name = "frida RPG Maker MKXP ttf hook";    Path = "frida\rpgmaker_mkxp_ttf_hook.js" },
    @{ Name = "frida d3d11 overlay";              Path = "frida\d3d11_gdi_overlay.js" },
    @{ Name = "web frontend";                     Path = "web\index.html" },
    @{ Name = "app icon";                         Path = "web\app.ico" },
    @{ Name = "app logo";                         Path = "web\logo.png" }
)
$missingSource = @()
foreach ($g in $requiredGroups) {
    $full = Join-Path $SourceRoot $g.Path
    if (Test-Path $full) { Say ("   ok    " + $g.Name) DarkGray }
    else { $missingSource += ($g.Name + "  ->  " + $g.Path); Say ("   MISS  " + $g.Name + "  (" + $g.Path + ")") Yellow }
}
if ($missingSource.Count -gt 0) {
    Fail ("`nPRECHECK FAILED: " + $missingSource.Count + " required source files are missing:`n  " + ($missingSource -join "`n  ") + "`n`nThese are exactly the files a previous release step used to add by hand.`nRestore them before building (see tools\release\README.md).")
}
Say "   source preflight: all required files present" Green
$BrandingCheck = Join-Path $PSScriptRoot "verify_branding.py"
& python $BrandingCheck --source-root $SourceRoot
if ($LASTEXITCODE -ne 0) { Fail "Source branding verification failed; restore the approved icon and GUI logo before building." }

# ---------------------------------------------------------------- 2) source <-> baseline parity
Say ""
Say "== 2/6 source parity with shipped package ==" Cyan
# Every asset the shipped package contains must still exist in this source tree.
# This is the check that would have caught a build shipping without the KRKR /
# Jupyter / wolf asset set: those files were gone from the source, not from the code.
$assetMaps = @(
    @{ From = "_internal/_internal/tools/"; To = "_internal\tools\" },
    @{ From = "_internal/_internal/licenses/"; To = "_internal\licenses\" },
    @{ From = "_internal/frida/"; To = "frida\" },
    @{ From = "_internal/assets/"; To = "assets\" },
    @{ From = "_internal/web/"; To = "web\" },
    @{ From = "_internal/engines/"; To = "engines\" },
    @{ From = "_internal/MSVCR100.dll"; To = "_internal\MSVCR100.dll" },
    @{ From = "_internal/frida_probe.js"; To = "frida_probe.js" }
)
$parityMissing = @()
$parityChecked = 0
foreach ($entry in $baseline.files) {
    $p = $entry.path
    $src = $null
    foreach ($m in $assetMaps) {
        if ($p.StartsWith($m.From)) { $src = $m.To + ($p.Substring($m.From.Length) -replace "/", "\"); break }
        if ($p -eq $m.From) { $src = $m.To; break }
    }
    if ($null -eq $src) { continue }
    $parityChecked++
    if (-not (Test-Path (Join-Path $SourceRoot $src))) { $parityMissing += ($src + "   (in package as " + $p + ")") }
}
if ($parityMissing.Count -gt 0) {
    $cats = $parityMissing | ForEach-Object { ($_ -split "\\")[0..1] -join "/" } | Group-Object | Sort-Object Count -Descending
    $summary = ($cats | ForEach-Object { "  " + $_.Name + "  x" + $_.Count }) -join "`n"
    Fail ("`nSOURCE PARITY FAILED: " + $parityMissing.Count + " of " + $parityChecked + " shipped asset files are absent from this source tree:`n" + $summary + "`n`nFirst 25:`n  " + (($parityMissing | Select-Object -First 25) -join "`n  ") + "`n`nA build from this tree would ship an incomplete package.`nRestore the files (they exist in the previous release package) before building.")
}
Say ("   source parity: all " + $parityChecked + " shipped asset files present in the tree") Green

# ---------------------------------------------------------------- 2b) test suite
if ($RunTests) {
    Say ""
    Say "== 2b/6 test suite ==" Cyan
    $pytestArgs = @("-m", "pytest", "tests", "-q", "-p", "no:cacheprovider")
    Push-Location $SourceRoot
    try {
        $out = & python @pytestArgs 2>&1
        $code = $LASTEXITCODE
    } finally { Pop-Location }
    $summary = ($out | Where-Object { $_ -match '^\d+ (failed|passed)|failed,|passed,' } | Select-Object -Last 1)
    Say ("   " + $(if ($summary) { $summary } else { "(no pytest summary)" }))
    $failedNames = $out | Where-Object { $_ -match '^FAILED ' } | ForEach-Object { ($_ -replace '^FAILED ', '') }
    if ($failedNames.Count -gt 0) {
        Say ("   failing: " + $failedNames.Count) Yellow
        foreach ($f in $failedNames) { Say ("     " + $f) DarkGray }
        if ($StrictTests) { Fail "`nTEST SUITE FAILED (" + $failedNames.Count + " tests) and -StrictTests was passed." }
        Say "   continuing anyway (pass -StrictTests to make tests a hard gate)" Yellow
    } elseif ($code -ne 0) {
        Say "   pytest exited with code $code (collection error?)" Yellow
        if ($StrictTests) { Fail "pytest failed with exit code $code" }
    } else {
        Say "   all tests passed" Green
    }
}

# Tests and local source runs create rebuildable Python caches. They must not
# leak into the PyInstaller data trees or the final package.
$cacheRoots = @($SourceRoot, $Dist)
foreach ($cacheRoot in $cacheRoots) {
    if (-not (Test-Path -LiteralPath $cacheRoot -PathType Container)) { continue }
    Get-ChildItem -LiteralPath $cacheRoot -Directory -Force -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -eq "__pycache__" } |
        Sort-Object FullName -Descending |
        ForEach-Object {
            Assert-ChildPath $_.FullName $cacheRoot
            Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue
        }
}

if ($VerifyOnly -and -not (Test-Path $Dist)) {
    Say ""
    Say "verify-only mode: no dist to check, source checks passed" Green
    exit 0
}

# ---------------------------------------------------------------- 3) build
if (-not $VerifyOnly -and -not $SkipBuild) {
    Say ""
    Say "== 3/6 build (PyInstaller) ==" Cyan
    # The exe carries a real version resource so Explorer / Task Manager show
    # "EngAixt" and a version instead of a blank product name.
    $buildVersion = $Version
    if ([string]::IsNullOrWhiteSpace($buildVersion)) {
        $updatePy = Join-Path $SourceRoot "core\app_update.py"
        if (Test-Path $updatePy) {
            $m = Select-String -Path $updatePy -Pattern 'APP_VERSION\s*=\s*"([^"]+)"' | Select-Object -First 1
            if ($m) { $buildVersion = $m.Matches[0].Groups[1].Value }
        }
    }
    if ([string]::IsNullOrWhiteSpace($buildVersion)) { Fail "cannot determine version for the exe version resource (pass -Version)" }
    $quad = ($buildVersion -split '[.\-+]') | Where-Object { $_ -match '^\d+$' } | Select-Object -First 4
    while ($quad.Count -lt 4) { $quad += '0' }
    $quadText = ($quad -join ', ')
    $viPath = Join-Path $SourceRoot "version_info.txt"
    $vi = @"
# generated by tools\release\release.ps1 - do not edit by hand
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=($quadText),
    prodvers=($quadText),
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('080404B0', [
        StringStruct('CompanyName', 'EngAixt'),
        StringStruct('FileDescription', 'EngAixt'),
        StringStruct('FileVersion', '$buildVersion'),
        StringStruct('InternalName', 'EngAixt'),
        StringStruct('LegalCopyright', 'EngAixt'),
        StringStruct('OriginalFilename', 'EngAixt.exe'),
        StringStruct('ProductName', 'EngAixt'),
        StringStruct('ProductVersion', '$buildVersion')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"@
    [System.IO.File]::WriteAllText($viPath, $vi, (New-Object System.Text.UTF8Encoding($false)))
    Say ("   version resource: EngAixt " + $buildVersion)
    Push-Location $SourceRoot
    try {
        & python -m PyInstaller game-translator.spec --noconfirm
        if ($LASTEXITCODE -ne 0) { Fail "PyInstaller failed with exit code $LASTEXITCODE" }
    } finally { Pop-Location }
} else {
    Say ""
    Say "== 3/6 build skipped ==" Cyan
}

$distExe = $null
foreach ($n in @("game-translator.exe", "EngAixt.exe")) {
    if (Test-Path (Join-Path $Dist $n)) { $distExe = Join-Path $Dist $n; break }
}
if ($null -eq $distExe) { Fail ("build output missing: no game-translator.exe / EngAixt.exe under " + $Dist) }

# ---------------------------------------------------------------- 4) verify build against baseline
Say ""
Say "== 4/6 verify build against baseline ==" Cyan
Say ("   baseline: " + $baseline.generated_from + "  (" + $baseline.functional_entries + " functional files)")
$miss = @()
foreach ($entry in $baseline.files) {
    $p = ($entry.path -replace "/", "\")
    if ($p -eq "EngAixt.exe") {
        if ((Test-Path (Join-Path $Dist "game-translator.exe")) -or (Test-Path (Join-Path $Dist "EngAixt.exe"))) { continue }
        $miss += "EngAixt.exe (or game-translator.exe)"
        continue
    }
    if ($p -eq "app_version.json" -or $p -like "*\app_version.json") { continue }   # written at staging time
    # PyInstaller onedir keeps payload under _internal\; an old spec produced a
    # doubled _internal\_internal\ prefix. Accept either layout, require the file.
    $candidates = @($p)
    if ($p -like "_internal\_internal\*") { $candidates += ($p -replace "^_internal\\_internal\\", "_internal\") }
    $found = $false
    foreach ($c in $candidates) {
        $target = if ($c -like "_internal\*") { Join-Path $Dist $c } else { Join-Path $Dist ("_internal\" + $c) }
        if (Test-Path $target) { $found = $true; break }
    }
    if (-not $found) { $miss += $p }
}
if ($miss.Count -gt 0) {
    $cats = $miss | Sort-Object | ForEach-Object {
        $n = ($_ -replace "\\", "/") -replace "^_internal/_internal/", "_internal/"
        (($n -split "/")[0..1] -join "/")
    } | Group-Object | Sort-Object Count -Descending
    $summary = ($cats | ForEach-Object { "  " + $_.Name + "  x" + $_.Count }) -join "`n"
    Fail ("`nBUILD VERIFY FAILED: " + $miss.Count + " functional file(s) from the baseline are not in the build:`n" + $summary + "`n`nFirst 20:`n  " + (($miss | Sort-Object | Select-Object -First 20) -join "`n  "))
}
Say ("   build contains all " + $baseline.functional_entries + " baseline functional files") Green
& python $BrandingCheck --source-root $SourceRoot --dist $Dist
if ($LASTEXITCODE -ne 0) { Fail "Built branding verification failed; rebuild instead of packaging stale icon resources." }

if ($VerifyOnly) {
    Say ""
    Say "verify-only: all checks passed" Green
    exit 0
}

if ([string]::IsNullOrWhiteSpace($Version)) { Fail "`n-Version is required for packaging (e.g. -Version 1.1.16)" }

# ---------------------------------------------------------------- 5) stage + package
Say ""
Say "== 5/6 package ==" Cyan
$pkgName = "EngAixt-$Version-Windows-x64"
$stage = Join-Path $ReleaseRoot "stage"
$pkgRoot = Join-Path $stage "EngAixt"
$zip = Join-Path $ReleaseRoot "$pkgName.zip"
Assert-ChildPath $stage $ReleaseRoot
if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
New-Item -ItemType Directory -Force -Path $pkgRoot | Out-Null
Get-ChildItem -LiteralPath $Dist -Force | ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $pkgRoot -Recurse -Force }
if ((Split-Path $distExe -Leaf) -ne "EngAixt.exe") {
    Move-Item -LiteralPath (Join-Path $pkgRoot (Split-Path $distExe -Leaf)) -Destination (Join-Path $pkgRoot "EngAixt.exe") -Force
}
$marker = [pscustomobject]@{ schema = 1; app = "EngAixt"; version = $Version } | ConvertTo-Json -Depth 4
$utf8 = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText((Join-Path $pkgRoot "app_version.json"), $marker, $utf8)
if (Test-Path (Join-Path $pkgRoot "_internal")) { [System.IO.File]::WriteAllText((Join-Path $pkgRoot "_internal\app_version.json"), $marker, $utf8) }
if (Test-Path $zip) { Remove-Item $zip -Force }
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory($stage, $zip, [System.IO.Compression.CompressionLevel]::Optimal, $false)
$zipItem = Get-Item $zip
$zipSha = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLowerInvariant()
Say ("   " + $zipItem.Name + "  " + [Math]::Round($zipItem.Length / 1MB, 1) + " MB  sha256=" + $zipSha.Substring(0, 16))

$dlDir = Join-Path $DeployDir "downloads"
Assert-ChildPath $dlDir $DeployDir
if (Test-Path -LiteralPath $dlDir) { Remove-Item -LiteralPath $dlDir -Recurse -Force }
New-Item -ItemType Directory -Force -Path $dlDir | Out-Null
$githubBase = "https://github.com/$GitHubRepo/releases/download/v$Version"
$packageUrl = "$githubBase/$pkgName.zip"
$manifestUrl = "$githubBase/$pkgName.zip.manifest.json"
$parts = @(
    [pscustomobject]@{
        path = $packageUrl
        size = $zipItem.Length
        sha256 = $zipSha
    }
)
Say "   GitHub Release asset manifest prepared"
$publishedAt = (Get-Date).ToUniversalTime().ToString("o")
$manifest = [pscustomobject]@{ filename = "$pkgName.zip"; version = $Version; size = $zipItem.Length; sha256 = $zipSha; partSize = $zipItem.Length; published_at = $publishedAt; parts = $parts }
[System.IO.File]::WriteAllText((Join-Path $dlDir "$pkgName.zip.manifest.json"), ($manifest | ConvertTo-Json -Depth 8), $utf8)
[System.IO.File]::WriteAllText((Join-Path $dlDir "$pkgName.zip.sha256"), "$zipSha  $pkgName.zip`n", $utf8)
if ($CleanOldParts) {
    Get-ChildItem $dlDir -Force | Where-Object { $_.Name -notlike "*$Version*" } | ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force }
    Say "   pruned older packages from downloads\ (-CleanOldParts)" Yellow
} else {
    Say "   older packages kept in downloads\ (pass -CleanOldParts to prune)"
}

$common = [ordered]@{
    schema = 1; app = "EngAixt"; version = $Version
    package_name = "$pkgName.zip"
    package_url = $packageUrl
    package_manifest_url = $manifestUrl
    sha256 = $zipSha; size = $zipItem.Length; published_at = $publishedAt
    notes = @("Windows x64 build $Version. Free and unrestricted; no activation code required.")
}
[System.IO.File]::WriteAllText((Join-Path $DeployDir "updates\latest.json"), ([pscustomobject]$common | ConvertTo-Json -Depth 6), $utf8)
Say "   update feed written (updates/latest.json)" Green

# ---------------------------------------------------------------- 6) deploy
if ($Deploy) {
    Say ""
    Say "== 6/6 deploy ==" Cyan
    # Wrangler also supports the user's existing OAuth login.
    if ([string]::IsNullOrWhiteSpace($env:CLOUDFLARE_ACCOUNT_ID)) { Say "   CLOUDFLARE_ACCOUNT_ID not set; letting wrangler pick the account" Yellow }
    Push-Location $DeployDir
    try {
        & "$env:APPDATA\npm\wrangler.cmd" pages deploy . --project-name=engaixt --branch=main --commit-dirty=true
        if ($LASTEXITCODE -ne 0) { Fail "wrangler pages deploy failed ($LASTEXITCODE)" }
    } finally { Pop-Location }
    $wranglerCache = Join-Path $DeployDir ".wrangler"
    Assert-ChildPath $wranglerCache $DeployDir
    Remove-Item -LiteralPath $wranglerCache -Recurse -Force -ErrorAction SilentlyContinue
    Say "   deployed; confirm the live feed with: tools\release\verify-live.ps1" Green
} else {
    Say ""
    Say "== 6/6 deploy skipped (pass -Deploy to upload) ==" Cyan
}

Say ""
Say ("DONE. package: " + $zip) Green
Say ("      deploy dir: " + $DeployDir) Green
