param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$data = if ($env:KANDU_WEB_DATA_DIR) { $env:KANDU_WEB_DATA_DIR } else { Join-Path $env:LOCALAPPDATA 'KanduWeb' }
$runtime = Join-Path $env:LOCALAPPDATA 'KanduWebRuntime'
$python = Join-Path $runtime '.venv\Scripts\python.exe'
$url = 'http://127.0.0.1:8766'
try {
    $launchFile = Join-Path $runtime 'web-launch.json'
    $launch = if (Test-Path -LiteralPath $launchFile) { Get-Content -LiteralPath $launchFile -Raw -Encoding UTF8 | ConvertFrom-Json } else { $null }
    $openUrl = if ($launch.publicUrl) { [string]$launch.publicUrl } else { $url }
    if (([uri]$openUrl).Scheme -notin @('http', 'https')) { throw 'publicUrl must be an HTTP or HTTPS URL.' }
    $health = $null
    try { $health = Invoke-RestMethod -Uri ($url + '/api/health') -TimeoutSec 2 } catch {}
    if (-not $health -or $health.status -ne 'ok' -or -not $health.version) {
        if (-not (Test-Path -LiteralPath $python)) { throw 'Run setup_web.bat first. See README.md.' }
        if (Get-NetTCPConnection -LocalPort 8766 -State Listen -ErrorAction SilentlyContinue) { throw 'Port 8766 is occupied by another application.' }
        New-Item -ItemType Directory -Path $runtime -Force | Out-Null
        $arguments = @('-B', '-u', ('"' + (Join-Path $repo 'run_web.py') + '"'), '--data-dir', ('"' + $data + '"'))
        foreach ($hostname in $launch.allowedHosts) { $arguments += @('--allowed-host', [string]$hostname) }
        foreach ($hostname in $launch.httpsHosts) { $arguments += @('--https-host', [string]$hostname) }
        foreach ($hostname in $launch.cloudflareHosts) { $arguments += @('--cloudflare-host', [string]$hostname) }
        if ($launch.secureCookie -eq $true) { $arguments += '--secure-cookie' }
        $process = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $repo -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $runtime 'web-server.log') -RedirectStandardError (Join-Path $runtime 'web-server-error.log')
        $process.Id | Set-Content -LiteralPath (Join-Path $runtime 'web-server.pid')
        for ($attempt = 0; $attempt -lt 30; $attempt++) {
            Start-Sleep -Milliseconds 300
            try { $health = Invoke-RestMethod -Uri ($url + '/api/health') -TimeoutSec 1; break } catch {}
            if ($process.HasExited) { throw ('Server exited. See ' + (Join-Path $runtime 'web-server-error.log')) }
        }
        if (-not $health) { throw ('Server is still starting. See ' + (Join-Path $runtime 'web-server.log')) }
    }
    Write-Host ('Kandu Web: ' + $openUrl)
    if (-not $NoBrowser) { Write-Host ('Access password: ' + (Get-Content -LiteralPath (Join-Path $data 'access-code.txt') -Raw).Trim()) }
    Write-Host ('Server logs: ' + $runtime)
    Write-Host 'The server runs in the background; closing this launcher does not stop it.'
    if (-not $NoBrowser) { Start-Process $openUrl }
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
