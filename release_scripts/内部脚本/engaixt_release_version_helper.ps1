param(
    [ValidateSet("patch", "minor", "major")]
    [string]$Bump = "patch",
    [ValidateSet("small", "bugfix", "feature", "major")]
    [string]$Kind = "small",
    [switch]$DryRun,
    [switch]$SkipTests,
    [switch]$SkipBuild,
    [switch]$TrialOnly,
    [switch]$ProOnly
)

$ErrorActionPreference = "Stop"

function New-TextFromCodepoints([int[]]$Codepoints) {
    return -join ($Codepoints | ForEach-Object { [string][char]$_ })
}

function Read-Utf8([string]$Path) {
    return [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
}

function Write-Utf8NoBom([string]$Path, [string]$Text) {
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Text, $encoding)
}

function Parse-Version([string]$Version) {
    if ($Version -notmatch '^(\d+)\.(\d+)\.(\d+)$') {
        throw "Invalid version: $Version"
    }
    return [pscustomobject]@{
        Major = [int]$Matches[1]
        Minor = [int]$Matches[2]
        Patch = [int]$Matches[3]
    }
}

function Next-Version([string]$Current, [string]$BumpKind) {
    $v = Parse-Version $Current
    if ($BumpKind -eq "major") {
        return "{0}.0.0" -f ($v.Major + 1)
    }
    if ($BumpKind -eq "minor") {
        return "{0}.{1}.0" -f $v.Major, ($v.Minor + 1)
    }
    return "{0}.{1}.{2}" -f $v.Major, $v.Minor, ($v.Patch + 1)
}

function Replace-Required([string]$Text, [string]$Pattern, [string]$Replacement, [string]$Label) {
    $newText = [regex]::Replace($Text, $Pattern, $Replacement, 1)
    if ($newText -eq $Text) {
        throw "Version replacement failed: $Label"
    }
    return $newText
}

function Invoke-Checked([string]$Title, [scriptblock]$Body) {
    Write-Host ""
    Write-Host "==> $Title" -ForegroundColor Cyan
    & $Body
    if ($LASTEXITCODE -ne 0) {
        throw "$Title failed with exit code $LASTEXITCODE"
    }
}

$appRepo = "C:\Users\31714\game-translator"
$websiteRoot = "C:\Users\31714\engaixt-website"
$appUpdatePath = Join-Path $appRepo "core\app_update.py"
$indexPath = Join-Path $appRepo "web\index.html"
$releaseWebsiteScript = Join-Path $websiteRoot "scripts\release_website.ps1"

foreach ($path in @($appUpdatePath, $indexPath, $releaseWebsiteScript)) {
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Required file not found: $path"
    }
}

$appUpdateText = Read-Utf8 $appUpdatePath
if ($appUpdateText -notmatch 'APP_VERSION\s*=\s*"(\d+\.\d+\.\d+)"') {
    throw "Could not read APP_VERSION from $appUpdatePath"
}

$currentVersion = $Matches[1]
$nextVersion = Next-Version $currentVersion $Bump

Write-Host "EngAixt release kind : $Kind"
Write-Host "Version bump         : $Bump"
Write-Host "Current version      : $currentVersion"
Write-Host "Next version         : $nextVersion"

if ($DryRun) {
    Write-Host ""
    Write-Host "Dry run only. No files changed and no package was built." -ForegroundColor Yellow
    exit 0
}

$indexText = Read-Utf8 $indexPath
$releaseWebsiteText = Read-Utf8 $releaseWebsiteScript

$appUpdateText = Replace-Required `
    $appUpdateText `
    'APP_VERSION\s*=\s*"\d+\.\d+\.\d+"' `
    ('APP_VERSION = "' + $nextVersion + '"') `
    $appUpdatePath

$indexText = Replace-Required `
    $indexText `
    '(<span\b[^>]*\bclass="ver"[^>]*>)v\d+\.\d+\.\d+(</span>)' `
    ('$1v' + $nextVersion + '$2') `
    $indexPath

$releaseWebsiteText = Replace-Required `
    $releaseWebsiteText `
    '\[string\]\$AppVersion\s*=\s*"\d+\.\d+\.\d+"' `
    ('[string]$AppVersion = "' + $nextVersion + '"') `
    $releaseWebsiteScript

Write-Utf8NoBom $appUpdatePath $appUpdateText
Write-Utf8NoBom $indexPath $indexText
Write-Utf8NoBom $releaseWebsiteScript $releaseWebsiteText

Write-Host ""
Write-Host "Updated source version to $nextVersion" -ForegroundColor Green

if (-not $SkipTests) {
    Invoke-Checked "Run release tests" {
        Push-Location $appRepo
        try {
            & python -m py_compile app.py core\app_update.py
            if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
            & python -m pytest tests\test_app_update.py tests\test_release_packaging.py -q
        } finally {
            Pop-Location
        }
    }
}

if ($TrialOnly -or $ProOnly) {
    Write-Warning "TrialOnly/ProOnly are legacy switches. EngAixt now publishes one unified package."
}

Invoke-Checked "Build unified website package" {
    Push-Location $websiteRoot
    try {
        $args = @("-ExecutionPolicy", "Bypass", "-File", ".\scripts\release_website.ps1", "-AppVersion", $nextVersion)
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

Write-Host ""
Write-Host "Release finished." -ForegroundColor Green
Write-Host "Version: $nextVersion"
$releaseFolderName = "EngAixt" + (New-TextFromCodepoints @(0x53D1, 0x5E03, 0x5305))
Write-Host ("Output : " + (Join-Path "C:\games" $releaseFolderName))
