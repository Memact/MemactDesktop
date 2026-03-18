$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$binPath = Join-Path $root 'dist\memact-engine.exe'

if (-not (Test-Path $binPath)) {
  Write-Host 'Engine binary not found. Run scripts\build_engine.ps1 first.'
  exit 1
}

& $binPath
