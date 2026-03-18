$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$engineRoot = Join-Path $root 'engine'
$distRoot = Join-Path $root 'dist'
$binPath = Join-Path $distRoot 'memact-engine.exe'

New-Item -ItemType Directory -Force $distRoot | Out-Null

Push-Location $engineRoot
try {
  & go build -o $binPath .\cmd\memact-engine
} finally {
  Pop-Location
}

Write-Host "Built $binPath"
