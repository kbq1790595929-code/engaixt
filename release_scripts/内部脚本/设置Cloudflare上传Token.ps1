param(
    [switch]$UserScope
)

$ErrorActionPreference = "Stop"

Write-Host "Paste your Cloudflare API token below. The input is hidden."
$secure = Read-Host "CLOUDFLARE_API_TOKEN" -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
} finally {
    if ($bstr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

if ([string]::IsNullOrWhiteSpace($plain)) {
    throw "Empty token."
}

if ($UserScope) {
    [Environment]::SetEnvironmentVariable("CLOUDFLARE_API_TOKEN", $plain, "User")
    Write-Host "Saved CLOUDFLARE_API_TOKEN to the current Windows user."
    Write-Host "Open a new terminal before running upload scripts."
} else {
    $env:CLOUDFLARE_API_TOKEN = $plain
    Write-Host "Token is available in this PowerShell session only."
    Write-Host "Uploading website now..."
    & powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "engaixt_upload_website.ps1")
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
