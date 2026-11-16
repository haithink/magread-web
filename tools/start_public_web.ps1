param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$runtime = Join-Path $env:LOCALAPPDATA 'KanduWebRuntime'
$tunnelDir = Join-Path $runtime 'cloudflared'
$tunnelExe = Join-Path $tunnelDir 'cloudflared.exe'
$tunnelConfig = Join-Path $tunnelDir 'config.yml'
$tunnelPidFile = Join-Path $tunnelDir 'tunnel-launch.pid'
try {
    if (-not (Test-Path -LiteralPath $tunnelExe) -or -not (Test-Path -LiteralPath $tunnelConfig)) {
        throw 'Install cloudflared and configure its tunnel in KanduWebRuntime\cloudflared first. See README.md.'
    }
    & (Join-Path $PSScriptRoot 'start_web.ps1') -NoBrowser
    $running = $false
    if (Test-Path -LiteralPath $tunnelPidFile) {
        $tunnelProcessId = 0
        if ([int]::TryParse((Get-Content -LiteralPath $tunnelPidFile -Raw).Trim(), [ref]$tunnelProcessId)) {
            $existing = Get-CimInstance Win32_Process -Filter "ProcessId = $tunnelProcessId" -ErrorAction SilentlyContinue
            $running = $existing -and $existing.ExecutablePath -eq $tunnelExe -and $existing.CommandLine.Contains($tunnelConfig)
        }
    }
    if (-not $running) {
        $process = Start-Process -FilePath $tunnelExe -ArgumentList @('tunnel', '--config', ('"' + $tunnelConfig + '"'), 'run') `
            -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $tunnelDir 'tunnel-output.log') `
            -RedirectStandardError (Join-Path $tunnelDir 'tunnel-error.log')
        $process.Id | Set-Content -LiteralPath $tunnelPidFile
    }
    Write-Host 'Cloudflare Tunnel is running in the background.'
    if (-not $NoBrowser) {
        $launch = Get-Content -LiteralPath (Join-Path $runtime 'web-launch.json') -Raw -Encoding UTF8 | ConvertFrom-Json
        if (([uri]$launch.publicUrl).Scheme -ne 'https') { throw 'Set an HTTPS publicUrl in web-launch.json.' }
        Start-Process ([string]$launch.publicUrl)
    }
} catch {
    New-Item -ItemType Directory -Path $runtime -Force | Out-Null
    $_.Exception.Message | Set-Content -LiteralPath (Join-Path $runtime 'public-start-error.log') -Encoding UTF8
    Write-Error $_.Exception.Message
    exit 1
}
