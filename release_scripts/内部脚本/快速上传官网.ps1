param(
    [string]$DeployDir = "",
    [string]$ProjectName = "engaixt",
    [string]$Branch = "main",
    [string]$SiteUrl = "https://engaixt.com",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$helper = Join-Path $PSScriptRoot "engaixt_upload_website.ps1"
$params = @{
    DeployDir = $DeployDir
    ProjectName = $ProjectName
    Branch = $Branch
    SiteUrl = $SiteUrl
}
if ($DryRun) { $params.DryRun = $true }
& $helper @params
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
