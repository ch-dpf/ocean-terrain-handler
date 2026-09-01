# Copy a host file or directory into the workspace Docker volume.
#
# Usage (from repo root):
#   .\scripts\copy-to-workspace-volume.ps1 D:\path\to\dem.tif
#   .\scripts\copy-to-workspace-volume.ps1 D:\path\to\dem-folder source\gDEM_N
#   .\scripts\copy-to-workspace-volume.ps1 D:\path\to\dem-folder   # -> source/<folder-name>

$ErrorActionPreference = "Stop"

if ($args.Count -lt 1) {
    Write-Host "Usage: .\scripts\copy-to-workspace-volume.ps1 <host-path> [dest-relative-under-workspace]"
    Write-Host "  dest defaults to source/<leaf-name>"
    exit 1
}

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$volumeName = if ($env:WORKSPACE_DOCKER_VOLUME) {
    $env:WORKSPACE_DOCKER_VOLUME
} else {
    "ocean-terrain-handler_workspace_data"
}

$hostPath = $args[0]
if (-not (Test-Path $hostPath)) {
    throw "Path not found: $hostPath"
}

$item = Get-Item $hostPath
$destRel = if ($args.Count -ge 2 -and $args[1]) {
    ($args[1] -replace "\\", "/").Trim("/")
} else {
    "source/$($item.Name)"
}

$fromMount = ((Resolve-Path $hostPath).Path -replace "\\", "/")
$isDir = $item.PSIsContainer

Write-Host "Copying '$hostPath' -> volume:$volumeName /$destRel"

if ($isDir) {
    docker run --rm `
      -v "${volumeName}:/data/workspace" `
      -v "${fromMount}:/from:ro" `
      alpine:3.20 `
      sh -c "mkdir -p '/data/workspace/$destRel' && cp -a /from/. '/data/workspace/$destRel/'"
} else {
    $destDir = Split-Path -Parent $destRel
    if (-not $destDir) { $destDir = "source" }
    $destDir = $destDir -replace "\\", "/"
    $leaf = Split-Path -Leaf $destRel
    docker run --rm `
      -v "${volumeName}:/data/workspace" `
      -v "${fromMount}:/from/file:ro" `
      alpine:3.20 `
      sh -c "mkdir -p '/data/workspace/$destDir' && cp /from/file '/data/workspace/$destDir/$leaf'"
}

Write-Host "Done."
