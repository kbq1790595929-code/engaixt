[CmdletBinding()]
param(
    [string]$CacheDirectory = (Join-Path $env:USERPROFILE '.cache\engaixt-analysis\ruby'),
    [string]$SevenZip = 'C:\Program Files\7-Zip\7z.exe'
)
$ErrorActionPreference = 'Stop'
$version = '4.0.7-1'
$asset = "rubyinstaller-$version-x64.7z"
$expected = '7a9be96d3448ecbd9a81eccae1f619131dfcc761f2c98dded03ece6c05d0474f'
$url = "https://github.com/oneclick/rubyinstaller2/releases/download/RubyInstaller-$version/$asset"
$cache = [System.IO.Path]::GetFullPath($CacheDirectory)
$ruby = Join-Path $cache "rubyinstaller-$version-x64\bin\ruby.exe"
if (-not (Test-Path -LiteralPath $ruby -PathType Leaf)) {
    if (-not (Test-Path -LiteralPath $SevenZip -PathType Leaf)) {
        throw "7-Zip is required to unpack the official portable Ruby archive: $SevenZip"
    }
    New-Item -ItemType Directory -Path $cache -Force | Out-Null
    $download = Join-Path $cache $asset
    Invoke-WebRequest -Uri $url -OutFile $download
    if ((Get-FileHash -LiteralPath $download -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expected) {
        throw 'RubyInstaller archive SHA-256 does not match the pinned official release'
    }
    & $SevenZip x $download "-o$cache" -y -bso0
    if ($LASTEXITCODE -ne 0) { throw "7-Zip failed: $LASTEXITCODE" }
}
& python -m pip install -r (Join-Path $PSScriptRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw "Python dependency installation failed: $LASTEXITCODE" }
& $ruby --version
if ($LASTEXITCODE -ne 0) { throw 'Portable Ruby verification failed' }
Write-Output "Ruby executable: $ruby"
Write-Output 'Ready: python -m tools.rgss_compare --mtool <MTool/Tool> --ruby <Ruby executable> --archive <Game.rgss2a>'
