$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$runtime = Join-Path $env:LOCALAPPDATA 'KanduWebRuntime'
$venv = Join-Path $runtime '.venv'
$python = Join-Path $venv 'Scripts\python.exe'
try {
    if (-not (Test-Path -LiteralPath $python)) {
        New-Item -ItemType Directory -Path $runtime -Force | Out-Null
        if (Get-Command py -ErrorAction SilentlyContinue) {
            & py -3 -m venv $venv
        } elseif (Get-Command python -ErrorAction SilentlyContinue) {
            & python -m venv $venv
        } else {
            throw 'Install Python 3.10 or newer first, then run setup_web.bat again.'
        }
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python virtual environment.' }
    }
    & $python -m pip install -r (Join-Path $repo 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. See output above.' }
    Write-Host 'Setup complete. Run start_web.bat.'
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
