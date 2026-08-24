# Build cesium-terrain-server locally (Docker Hub image uses obsolete manifest v1).
# Requires sibling repo: ../cesium-terrain-server

$ErrorActionPreference = "Stop"

$handlerRoot = Split-Path $PSScriptRoot -Parent
$ctsDockerDir = Join-Path (Split-Path $handlerRoot -Parent) "cesium-terrain-server\docker"

if (-not (Test-Path $ctsDockerDir)) {
    Write-Error @"
cesium-terrain-server not found at: $ctsDockerDir

Clone it next to this project:
  git clone https://github.com/geo-data/cesium-terrain-server D:\workspace\cesium-terrain-server
"@
}

$checkout = (Get-Content (Join-Path $ctsDockerDir "cts-checkout.txt") -Raw).Trim()
& (Join-Path $ctsDockerDir "pack-src.ps1") -Checkout $checkout

docker build -t geodata/cesium-terrain-server:local $ctsDockerDir
Write-Host "Built geodata/cesium-terrain-server:local"
