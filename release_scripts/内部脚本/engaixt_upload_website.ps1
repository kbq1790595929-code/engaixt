param(
    [string]$DeployDir = "",
    [string]$ProjectName = "engaixt",
    [string]$Branch = "main",
    [string]$SiteUrl = "https://engaixt.com",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

function New-TextFromCodepoints([int[]]$Codepoints) {
    return -join ($Codepoints | ForEach-Object { [string][char]$_ })
}

function Resolve-FullPath([string]$Path) {
    return [System.IO.Path]::GetFullPath($Path)
}

function Read-JsonFile([string]$Path) {
    return Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
}

function Test-ProductionManifest([string]$SiteUrl, [object]$ExpectedManifest) {
    $baseUrl = $SiteUrl.TrimEnd("/")
    $lastError = $null

    for ($attempt = 1; $attempt -le 6; $attempt++) {
        $cacheBust = [DateTime]::UtcNow.Ticks
        $url = "$baseUrl/updates/trial/latest.json?cache=$cacheBust"
        try {
            try {
                $response = Invoke-WebRequest -UseBasicParsing -Uri $url -Headers @{"Cache-Control" = "no-cache"} -TimeoutSec 30
                $content = $response.Content
            } catch {
                $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
                if (-not $curl) {
                    throw
                }
                $content = & curl.exe -L --silent --show-error --retry 2 --connect-timeout 15 --max-time 60 $url
                if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($content)) {
                    throw
                }
            }
            $actual = $content | ConvertFrom-Json
            if ($actual.version -eq $ExpectedManifest.version -and $actual.sha256 -eq $ExpectedManifest.sha256) {
                Write-Host "Production check passed: $baseUrl updates/trial/latest.json => $($actual.version)" -ForegroundColor Green
                return
            }
            $lastError = "production manifest mismatch: version=$($actual.version), sha256=$($actual.sha256)"
        } catch {
            $lastError = $_.Exception.Message
        }

        Write-Warning "Production check attempt $attempt failed: $lastError"
        Start-Sleep -Seconds 5
    }

    throw "Production site did not expose the new manifest after deploy: $lastError"
}

function Test-ProductionManifestAtPath([string]$SiteUrl, [string]$ManifestPath, [object]$ExpectedManifest) {
    $baseUrl = $SiteUrl.TrimEnd("/")
    $relative = $ManifestPath.TrimStart("\", "/").Replace("\", "/")
    $lastError = $null

    for ($attempt = 1; $attempt -le 6; $attempt++) {
        $cacheBust = [DateTime]::UtcNow.Ticks
        $url = "$baseUrl/$relative?cache=$cacheBust"
        try {
            try {
                $response = Invoke-WebRequest -UseBasicParsing -Uri $url -Headers @{"Cache-Control" = "no-cache"} -TimeoutSec 30
                $content = $response.Content
            } catch {
                $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
                if (-not $curl) {
                    throw
                }
                $content = & curl.exe -L --silent --show-error --retry 2 --connect-timeout 15 --max-time 60 $url
                if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($content)) {
                    throw
                }
            }
            $actual = $content | ConvertFrom-Json
            if ($actual.version -eq $ExpectedManifest.version -and $actual.sha256 -eq $ExpectedManifest.sha256) {
                Write-Host "Production check passed: $baseUrl/$relative => $($actual.version)" -ForegroundColor Green
                return
            }
            $lastError = "production manifest mismatch: version=$($actual.version), sha256=$($actual.sha256)"
        } catch {
            $lastError = $_.Exception.Message
        }

        Write-Warning "Production check $relative attempt $attempt failed: $lastError"
        Start-Sleep -Seconds 5
    }

    throw "Production site did not expose $relative after deploy: $lastError"
}

if ([string]::IsNullOrWhiteSpace($DeployDir)) {
    $releaseFolderName = "EngAixt" + (New-TextFromCodepoints @(0x53D1, 0x5E03, 0x5305))
    $websiteFolderName = New-TextFromCodepoints @(0x5B98, 0x7F51, 0x4E0A, 0x4F20, 0x76EE, 0x5F55)
    $DeployDir = Join-Path (Join-Path "C:\games" $releaseFolderName) $websiteFolderName
}

$DeployDir = Resolve-FullPath $DeployDir

if (-not (Test-Path -LiteralPath $DeployDir)) {
    throw "Deploy directory not found: $DeployDir"
}

$required = @(
    "index.html",
    "downloads\EngAixt-Windows-Trial.zip.manifest.json",
    "updates\trial\latest.json"
)
foreach ($rel in $required) {
    $path = Join-Path $DeployDir $rel
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Required deploy file missing: $path"
    }
}

$expectedManifest = Read-JsonFile (Join-Path $DeployDir "updates\trial\latest.json")
$unlimitedManifestPath = Join-Path $DeployDir "updates\unlimited\latest.json"
$expectedUnlimitedManifest = $null
if (Test-Path -LiteralPath $unlimitedManifestPath) {
    $expectedUnlimitedManifest = Read-JsonFile $unlimitedManifestPath
}

$npm = Get-Command npm -ErrorAction SilentlyContinue
if (-not $npm) {
    throw "npm was not found. Install Node.js first, or run this on a machine with npm."
}

Write-Host "DeployDir  : $DeployDir"
Write-Host "Project    : $ProjectName"
Write-Host "Branch     : $Branch"
Write-Host "SiteUrl    : $SiteUrl"
Write-Host "Version    : $($expectedManifest.version)"
if ($expectedUnlimitedManifest) {
    Write-Host "Pro update : $($expectedUnlimitedManifest.version) hidden manifest present"
}
Write-Host "Uploader   : Wrangler CLI"

$npmArgs = @(
    "exec",
    "--yes",
    "wrangler@latest",
    "--",
    "pages",
    "deploy",
    $DeployDir,
    "--project-name=$ProjectName",
    "--branch=$Branch"
)

if ($DryRun) {
    Write-Host ""
    Write-Host "Dry run. Command to execute:"
    Write-Host ("npm " + ($npmArgs -join " "))
    exit 0
}

Write-Host ""
Write-Host "Starting Cloudflare Pages deploy..."
& npm @npmArgs
if ($LASTEXITCODE -ne 0) {
    throw "Wrangler deploy failed with exit code $LASTEXITCODE"
}

Write-Host ""
Write-Host "Cloudflare Pages deploy finished."

Write-Host ""
Write-Host "Checking production manifest..."
Test-ProductionManifest -SiteUrl $SiteUrl -ExpectedManifest $expectedManifest
if ($expectedUnlimitedManifest) {
    try {
        Test-ProductionManifestAtPath -SiteUrl $SiteUrl -ManifestPath "updates\unlimited\latest.json" -ExpectedManifest $expectedUnlimitedManifest
    } catch {
        Write-Warning "Optional Pro update manifest check failed: $($_.Exception.Message)"
    }
}
