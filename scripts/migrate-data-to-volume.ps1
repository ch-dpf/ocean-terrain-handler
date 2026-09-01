# Migrate existing ./data/{source,jobs,tilesets,uploads} into the Docker named volume
# used by the default docker-compose.yml (fast Linux volume on Docker Desktop).
#
# Usage (from repo root):
#   powershell -ExecutionPolicy Bypass -File .\scripts\migrate-data-to-volume.ps1

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$volumeName = if ($env:WORKSPACE_DOCKER_VOLUME) {
    $env:WORKSPACE_DOCKER_VOLUME
} else {
    "ocean-terrain-handler_workspace_data"
}

$hostData = Join-Path $repoRoot "data"
if (-not (Test-Path $hostData)) {
    throw "Host data directory not found: $hostData"
}

$helper = Join-Path $PSScriptRoot "_migrate-copy.sh"
$scriptBody = @'
#!/bin/sh
set -e
mkdir -p /to
for d in source jobs tilesets uploads; do
  if [ -d "/from/$d" ]; then
    echo "-> $d"
    rm -rf "/to/$d"
    cp -a "/from/$d" /to/
  fi
done
mkdir -p /to/source /to/jobs /to/uploads /to/tilesets/terrain
echo "Volume contents:"
ls -la /to
echo "source entries:" 
ls /to/source | wc -l
'@
[System.IO.File]::WriteAllText($helper, $scriptBody.Replace("`r`n", "`n"))

Write-Host "Ensuring Compose volume '$volumeName' exists..."
docker compose create api | Out-Null

$fromMount = ($hostData -replace "\\", "/")
$helperMount = ($helper -replace "\\", "/")
Write-Host "Copying source/jobs/tilesets/uploads from $hostData into volume $volumeName ..."

docker run --rm `
  -v "${volumeName}:/to" `
  -v "${fromMount}:/from:ro" `
  -v "${helperMount}:/migrate.sh:ro" `
  alpine:3.20 `
  sh /migrate.sh

Remove-Item -Force $helper -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "Done. Next:"
Write-Host "  1. Ensure .env has WORKSPACE_DOCKER_VOLUME=$volumeName"
Write-Host "  2. docker compose up -d --build"
Write-Host "Workspace (including DEM under source/) now lives in the named volume."
Write-Host "To add more DEM later, use: .\scripts\copy-to-workspace-volume.ps1"
