param(
    [ValidateSet("none", "patch", "minor", "major")]
    [string]$Bump = "patch",

    [ValidateSet("small", "bugfix", "feature", "major")]
    [string]$Kind = "bugfix",

    [switch]$SkipTests,
    [switch]$SkipBuild,
    [switch]$NoUpload,
    [switch]$Upload,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

function New-TextFromCodepoints([int[]]$Codepoints) {
    return -join ($Codepoints | ForEach-Object { [string][char]$_ })
}

function Invoke-Checked([string]$Title, [scriptblock]$Body) {
    Write-Host ""
    Write-Host "==> $Title" -ForegroundColor Cyan
    & $Body
    if ($LASTEXITCODE -ne 0) {
        throw "$Title failed with exit code $LASTEXITCODE"
    }
}

function Read-AppVersion([string]$AppRepo) {
    $appUpdatePath = Join-Path $AppRepo "core\app_update.py"
    if (-not (Test-Path -LiteralPath $appUpdatePath)) {
        throw "Version source not found: $appUpdatePath"
    }
    $text = [System.IO.File]::ReadAllText($appUpdatePath, [System.Text.Encoding]::UTF8)
    if ($text -notmatch 'APP_VERSION\s*=\s*"(\d+\.\d+\.\d+)"') {
        throw "Could not read APP_VERSION from $appUpdatePath"
    }
    return $Matches[1]
}

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$appRepo = "C:\Users\31714\game-translator"
$websiteRoot = "C:\Users\31714\engaixt-website"
$internalScriptRoot = Join-Path $scriptRoot (New-TextFromCodepoints @(0x5185, 0x90E8, 0x811A, 0x672C))
$releaseHelper = Join-Path $internalScriptRoot "engaixt_release_version_helper.ps1"
$uploadScriptName = (New-TextFromCodepoints @(0x5FEB, 0x901F, 0x4E0A, 0x4F20, 0x5B98, 0x7F51)) + ".ps1"
$uploadScript = Join-Path $internalScriptRoot $uploadScriptName
$trialScript = Join-Path $websiteRoot "scripts\release_website.ps1"
$releaseFolderName = "EngAixt" + (New-TextFromCodepoints @(0x53D1, 0x5E03, 0x5305))
$websiteDeployFolderName = New-TextFromCodepoints @(0x5B98, 0x7F51, 0x4E0A, 0x4F20, 0x76EE, 0x5F55)
$trialFolderName = New-TextFromCodepoints @(0x8BD5, 0x7528, 0x7248)
$outputRoot = Join-Path "C:\games" $releaseFolderName
$deployDir = Join-Path $outputRoot $websiteDeployFolderName
$shouldUpload = -not [bool]$NoUpload
if ($Upload) {
    $shouldUpload = $true
}

foreach ($required in @($appRepo, $websiteRoot, $releaseHelper, $trialScript)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Required path not found: $required"
    }
}

$currentVersion = Read-AppVersion $appRepo

Write-Host "EngAixt one-command release"
Write-Host "Current version : $currentVersion"
Write-Host "Version bump    : $Bump"
Write-Host "Release kind    : $Kind"
Write-Host "Output root     : $outputRoot"
Write-Host "Pages deploy dir: $deployDir"
Write-Host "Upload website  : $shouldUpload"

if ($DryRun) {
    Write-Host ""
    Write-Host "Dry run only. No package will be built." -ForegroundColor Yellow
    if ($Bump -eq "none") {
        Write-Host "Would rebuild current version ${currentVersion}:"
        Write-Host "  1. unified package + website upload directory"
        Write-Host "  2. legacy Pro compatibility manifest pointing to the unified package"
    } else {
        Write-Host "Would bump version by $Bump and build the unified package + website upload directory."
    }
    if ($shouldUpload) {
        Write-Host "Would upload website after packaging."
    }
    exit 0
}

if ($Bump -eq "none") {
    Invoke-Checked "Build unified package and website upload directory for current version $currentVersion" {
        Push-Location $websiteRoot
        try {
            $args = @(
                "-ExecutionPolicy", "Bypass",
                "-File", ".\scripts\release_website.ps1",
                "-AppRepo", $appRepo,
                "-WebsiteRoot", $websiteRoot,
                "-ExportDir", $outputRoot,
                "-DeployDir", $deployDir,
                "-AppVersion", $currentVersion
            )
            if ($SkipBuild) {
                $args += "-SkipBuild"
            } else {
                $args += "-CleanBuild"
            }
            & powershell @args
        } finally {
            Pop-Location
        }
    }

} else {
    Invoke-Checked "Bump version and build unified package + website upload directory" {
        $params = @{
            Bump = $Bump
            Kind = $Kind
        }
        if ($SkipTests) { $params.SkipTests = $true }
        if ($SkipBuild) { $params.SkipBuild = $true }
        & $releaseHelper @params
    }
}

if ($shouldUpload) {
    if (-not (Test-Path -LiteralPath $uploadScript)) {
        throw "Upload script not found: $uploadScript"
    }
    Invoke-Checked "Upload website to Cloudflare Pages" {
        & powershell -ExecutionPolicy Bypass -File $uploadScript -DeployDir $deployDir
    }
}

$finalVersion = Read-AppVersion $appRepo
Write-Host ""
Write-Host "Release finished." -ForegroundColor Green
Write-Host "Version         : $finalVersion"
Write-Host "Unified package : $(Join-Path $outputRoot $trialFolderName)"
Write-Host "Pages deploy dir: $deployDir"
if ($shouldUpload) {
    Write-Host "Website upload  : done"
} else {
    Write-Host "Website upload  : skipped by -NoUpload"
}
