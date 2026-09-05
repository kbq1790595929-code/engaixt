param(
    [string]$Period = (Get-Date -Format "yyyy-MM")
)

$ErrorActionPreference = "Stop"

function New-TextFromCodepoints([int[]]$Codepoints) {
    return -join ($Codepoints | ForEach-Object { [string][char]$_ })
}

$repo = "C:\Users\31714\game-translator"
$generator = Join-Path $repo "scripts\generate_monthly_member_code.py"
$keyDirName = "EngAixt" + (New-TextFromCodepoints @(0x53D1, 0x5E03, 0x5BC6, 0x94A5))
$codeDirName = New-TextFromCodepoints @(0x4F1A, 0x5458, 0x7801)
$fileSuffix = New-TextFromCodepoints @(0x6708, 0x5EA6, 0x4F1A, 0x5458, 0x7801, 0x4E0E, 0x81EA, 0x52A8, 0x56DE, 0x590D)
$outputDir = Join-Path (Join-Path "C:\games" $keyDirName) $codeDirName
$output = Join-Path $outputDir ($Period + $fileSuffix + ".txt")

if (-not (Test-Path -LiteralPath $generator)) {
    throw "Monthly membership code generator not found: $generator"
}

New-Item -ItemType Directory -Force -Path $outputDir | Out-Null
Push-Location $repo
try {
    & python $generator --period $Period --output $output
    if ($LASTEXITCODE -ne 0) {
        throw "Monthly membership code generation failed with exit code $LASTEXITCODE"
    }
} finally {
    Pop-Location
}

$code = Get-Content -LiteralPath $output -Encoding UTF8 |
    Where-Object { $_ -match '^EAI[12]\.' } |
    Select-Object -First 1
if (-not $code) {
    throw "Generated output does not contain an EngAixt membership code: $output"
}

Set-Clipboard -Value $code
Write-Host "Membership code copied to the clipboard." -ForegroundColor Green
Write-Host "Afdian auto-reply file: $output" -ForegroundColor Green
