$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$distRoot = Join-Path $root 'dist'
$releaseRoot = Join-Path $distRoot 'memact-release'
$pyiKey = $env:MEMACT_PYI_KEY
if (-not $pyiKey) {
  $pyiKey = [System.Guid]::NewGuid().ToString('N')
  Write-Host "Generated MEMACT_PYI_KEY=$pyiKey"
}

if (Test-Path $releaseRoot) { Remove-Item -Recurse -Force $releaseRoot }
New-Item -ItemType Directory -Force $releaseRoot | Out-Null

Write-Host 'Packaging app (PyInstaller, encrypted bytecode)...'
& python -m PyInstaller --noconfirm --clean --onefile --name memact --key $pyiKey --add-data "assets;assets" --add-data "extension;extension" main.py

Write-Host 'Assembling release bundle (no .py sources)...'
Copy-Item (Join-Path $distRoot 'memact.exe') $releaseRoot
Copy-Item (Join-Path $root 'LICENSE') $releaseRoot
Copy-Item (Join-Path $root 'README.md') $releaseRoot

Get-ChildItem -Path $releaseRoot
Write-Host 'Done.'
