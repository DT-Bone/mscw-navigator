# First install of MSCW Navigator, run by "MSCW Navigator.bat": downloads the navigator and its own copy
# of Python into the install folder. Nothing is installed on Windows itself. After this, launcher.py keeps
# both up to date.
param(
    [Parameter(Mandatory = $true)] [string]$App,
    [Parameter(Mandatory = $true)] [string]$Repo,
    [string]$Branch = 'main'
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'  # Windows PowerShell's progress bar makes downloads crawl
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Add-Type -AssemblyName System.IO.Compression.FileSystem
$headers = @{ 'User-Agent' = 'mscw-navigator' }

function Get-Zip($url, $dest) {
    $zip = Join-Path $env:TEMP ('mscw-' + [guid]::NewGuid() + '.zip')
    Invoke-WebRequest $url -OutFile $zip -UseBasicParsing -Headers $headers
    if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
    [IO.Compression.ZipFile]::ExtractToDirectory($zip, $dest)
    Remove-Item $zip
}

New-Item -ItemType Directory -Force $App | Out-Null
Write-Host 'Downloading the navigator...'
$sha = (Invoke-RestMethod "https://api.github.com/repos/$Repo/commits/$Branch" -Headers $headers).sha
$code = Join-Path $env:TEMP ('mscw-code-' + [guid]::NewGuid())
Get-Zip "https://codeload.github.com/$Repo/zip/$sha" $code
$top = Get-ChildItem $code -Directory | Select-Object -First 1  # GitHub's zips hold one top folder
Copy-Item (Join-Path $top.FullName '*') $App -Recurse -Force
Remove-Item $code -Recurse -Force
[IO.File]::WriteAllText((Join-Path $App 'version.txt'), $sha)

$runtime = (Get-Content (Join-Path $App 'runtime.txt') -Raw).Trim()
Write-Host 'Downloading its own copy of Python (about 35 MB)...'
Get-Zip "https://github.com/$Repo/releases/download/$runtime/runtime.zip" (Join-Path $App 'runtime')
Write-Host 'Installed.'
