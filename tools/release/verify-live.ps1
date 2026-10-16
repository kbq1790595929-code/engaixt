<#
  Confirm what the public site is actually serving after a deploy.

  Usage:
    powershell -File tools\release\verify-live.ps1              # feed + first/last part
    powershell -File tools\release\verify-live.ps1 -Full        # download and hash every part
    powershell -File tools\release\verify-live.ps1 -Local       # also diff against the local deploy dir
#>
param(
    [string]$Site = "https://engaixt.com",
    [string]$DeployDir = "",
    [switch]$Full,
    [switch]$Local
)

$ErrorActionPreference = "Stop"
function Say([string]$T, [string]$C = "Gray") { Write-Host $T -ForegroundColor $C }
if ([string]::IsNullOrWhiteSpace($DeployDir)) {
    $DeployDir = (Resolve-Path (Join-Path $PSScriptRoot "..\..\site")).Path
}
$curl = Join-Path $env:SystemRoot "System32\curl.exe"
$tmp = Join-Path $env:TEMP ("verify-live-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null

try {
    Say "== live feed ==" Cyan
    $feedPath = Join-Path $tmp "latest.json"
    & $curl -fsSL --connect-timeout 20 --max-time 900 --noproxy "*" -o $feedPath "$Site/updates/latest.json"
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $feedPath) -or (Get-Item $feedPath).Length -eq 0) { Say "FAILED: cannot fetch $Site/updates/latest.json" Red; exit 1 }
    $feed = Get-Content $feedPath -Raw -Encoding UTF8 | ConvertFrom-Json
    Say ("   version : " + $feed.version)
    Say ("   size    : " + $feed.size)
    Say ("   sha256  : " + $feed.sha256)
    Say ("   package : " + $feed.package_name)
    Say ("   notes   : " + ($feed.notes -join "; "))

    $manifestPath = Join-Path $tmp "manifest.json"
    & $curl -fsSL --connect-timeout 20 --max-time 900 --noproxy "*" -o $manifestPath $feed.package_manifest_url
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $manifestPath) -or (Get-Item $manifestPath).Length -eq 0) {
        Say "FAILED: cannot fetch package manifest $($feed.package_manifest_url)" Red
        exit 1
    }
    try {
        $manifest = Get-Content $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        Say "FAILED: package manifest is invalid JSON" Red
        exit 1
    }
    if (-not $manifest.parts -or $manifest.size -le 0 -or $manifest.version -ne $feed.version -or
        $manifest.size -ne $feed.size -or $manifest.sha256 -ne $feed.sha256) {
        Say "FAILED: package manifest is incomplete or does not match feed version/size/hash" Red
        exit 1
    }
    Say ""
    Say ("== package assets (" + $manifest.parts.Count + ") ==") Cyan
    $total = 0
    foreach ($p in $manifest.parts) { $total += $p.size }
    if ($total -ne $manifest.size) { Say "FAILED: package asset sizes do not sum to manifest size" Red; exit 1 }

    $targets = if ($Full) { $manifest.parts } else { @($manifest.parts[0], $manifest.parts[-1]) | Sort-Object path -Unique }
    $bad = 0
    foreach ($p in $targets) {
        $name = Split-Path $p.path -Leaf
        $out = Join-Path $tmp $name
        $partUrl = [string]$p.path
        if (-not ([Uri]$partUrl).IsAbsoluteUri) {
            $partUrl = "$Site/$partUrl"
        }
        & $curl -fsSL --connect-timeout 20 --max-time 900 --noproxy "*" -o $out $partUrl
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path $out)) {
            Say ("   BAD   " + $name + "  download failed") Red
            $bad++
            continue
        }
        $len = (Get-Item $out).Length
        $sha = (Get-FileHash $out -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($len -ne $p.size -or $sha -ne $p.sha256) {
            Say ("   BAD   " + $name + "  got " + $len + " / " + $sha.Substring(0, 12)) Red
            $bad++
        } else {
            Say ("   ok    " + $name + "  " + $len) DarkGray
        }
        Remove-Item $out -Force -ErrorAction SilentlyContinue
    }
    if ($bad -gt 0) { Say ("FAILED: " + $bad + " package asset(s) failed verification") Red; exit 1 }
    if (-not $Full) { Say ("   checked first and last asset (use -Full for all " + $manifest.parts.Count + ")") }

    if ($Local) {
        Say ""
        Say "== local deploy dir ==" Cyan
        $localFeed = Join-Path $DeployDir "updates\latest.json"
        if (-not (Test-Path $localFeed)) { Say "FAILED: local feed does not exist" Red; exit 1 }
        else {
            $lf = Get-Content $localFeed -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($lf.version -eq $feed.version -and $lf.sha256 -eq $feed.sha256 -and $lf.size -eq $feed.size) { Say ("   local matches live (" + $lf.version + ")") Green }
            else { Say "FAILED: local feed differs from live version/size/hash" Red; exit 1 }
        }
    }

    Say ""
    Say ("LIVE OK: " + $feed.version + " (" + $feed.package_name + ")") Green
} finally {
    $resolvedTmp = [System.IO.Path]::GetFullPath($tmp)
    $tempRoot = [System.IO.Path]::GetFullPath($env:TEMP).TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    if (-not $resolvedTmp.StartsWith($tempRoot, [System.StringComparison]::OrdinalIgnoreCase) -or
        [System.IO.Path]::GetFileName($resolvedTmp) -notlike 'verify-live-*') {
        throw "Refusing cleanup outside verification temp directory: $resolvedTmp"
    }
    Remove-Item -LiteralPath $resolvedTmp -Recurse -Force -ErrorAction SilentlyContinue
}
