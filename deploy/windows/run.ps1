param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("api", "worker", "check")]
    [string]$Mode,
    [string]$Venv = ".venv",
    [string]$EnvFile = ".env"
)

$ErrorActionPreference = "Stop"

if (Test-Path $EnvFile) {
    Get-Content $EnvFile | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
            $name, $value = $line.Split("=", 2)
            [Environment]::SetEnvironmentVariable($name.Trim(), $value.Trim(), "Process")
        }
    }
}

$venvRoot = (Resolve-Path $Venv).Path
$python = Join-Path $venvRoot "Scripts\python.exe"
$workspace = [Environment]::GetEnvironmentVariable("WORKSPACE_DIR", "Process")
if ($workspace) {
    New-Item -ItemType Directory -Force -Path $workspace | Out-Null
    Set-Location $workspace
}

if (-not (Test-Path $python)) {
    throw "Virtual environment not found: $Venv"
}

& $python -m app.services.ctb.native_check
if ($LASTEXITCODE -ne 0 -or $Mode -eq "check") {
    exit $LASTEXITCODE
}

if ($Mode -eq "api") {
    & (Join-Path $venvRoot "Scripts\ocean-terrain-api.exe")
} else {
    # app.cli selects Celery's solo pool on native Windows; terrain work itself
    # remains parallel inside the C++/thread tile engine.
    & (Join-Path $venvRoot "Scripts\ocean-terrain-worker.exe")
}
