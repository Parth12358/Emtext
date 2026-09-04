<#
.SYNOPSIS
  Start the emtext server and a Cloudflare tunnel together, with a persisted
  AUTH_TOKEN, and print the URLs.

.DESCRIPTION
  Does the four things that otherwise have to be remembered in order:

    1. Loads AUTH_TOKEN from ~/.emtext/auth_token, creating one if absent. The
       server accepts ANY client when this is unset, so a public tunnel over an
       unauthenticated server is the failure mode worth engineering away.
    2. Starts the server and WAITS for /health. Opening the tunnel first makes
       the first requests 502, which looks like a tunnel fault rather than a race.
    3. Starts the tunnel -- named if ~/.cloudflared/config.yml exists, otherwise
       a quick tunnel -- and extracts the hostname.
    4. Prints ready-to-open URLs with the token already appended.

  Ctrl+C stops both.

.PARAMETER RotateToken
  Replace the AUTH_TOKEN with a fresh one, invalidating every existing URL.

  The token is STABLE across restarts by default so bookmarks and phone tabs
  keep working. Rotate when a token has actually been exposed. Rotation is not a
  defence against guessing -- 32 random bytes is 256 bits, which is not
  searchable -- it only bounds the damage from a leak.

.PARAMETER Quick
  Force a quick tunnel (random trycloudflare.com hostname) even when a named
  tunnel is configured.

.PARAMETER NoTunnel
  Server only, no tunnel. For local work.

.EXAMPLE
  .\tunnel\start.ps1
.EXAMPLE
  .\tunnel\start.ps1 -RotateToken
.EXAMPLE
  .\tunnel\start.ps1 -NoTunnel
#>
[CmdletBinding()]
param(
  [switch]$RotateToken,
  [switch]$Quick,
  [switch]$NoTunnel,
  [int]$Port = 8000
)

$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')

# Prefer the venv interpreter; the system python usually lacks the deps and
# fails much later with a confusing ModuleNotFoundError.
$py = if (Test-Path '.venv\Scripts\python.exe') { '.venv\Scripts\python.exe' } else { 'python' }

$script:ServerProc = $null
$script:TunnelProc = $null

function Stop-All {
  Write-Host ''
  Write-Host 'shutting down...' -ForegroundColor DarkGray
  foreach ($p in @($script:TunnelProc, $script:ServerProc)) {
    if ($p -and -not $p.HasExited) { try { $p.Kill() } catch {} }
  }
}

try {
  # --- 1. token ------------------------------------------------------------
  $tokenArgs = @('tunnel/token.py')
  if ($RotateToken) { $tokenArgs += '--rotate' }
  $token = (& $py @tokenArgs).Trim()
  if (-not $token) { throw 'could not obtain an AUTH_TOKEN' }
  $env:AUTH_TOKEN = $token
  # Windows HF cache needs symlink privileges without this; first-time model
  # downloads fail with WinError 1314.
  $env:HF_HUB_DISABLE_SYMLINKS = '1'
  $env:HF_HUB_DISABLE_SYMLINKS_WARNING = '1'
  if ($RotateToken) {
    Write-Host "auth token ROTATED ($($token.Length) chars) -- previously-opened URLs are now dead" -ForegroundColor DarkYellow
  } else {
    Write-Host "auth token ($($token.Length) chars) from $(& $py tunnel/token.py --path)" -ForegroundColor DarkGray
  }

  # --- 2. server -----------------------------------------------------------
  # Redirect the server's streams to a file rather than sharing this console.
  # Two reasons, one fatal:
  #   * Python logs to STDERR, and with $ErrorActionPreference='Stop' a native
  #     command writing to stderr becomes a TERMINATING error (NativeCommandError).
  #     Sharing the console killed this script immediately after the server
  #     started -- before the health check, so the tunnel never launched.
  #   * funasr narrates every weight tensor it loads (~200 lines), which buried
  #     the handful of messages that actually matter.
  # The log is tailed on failure and its path is always printed.
  $serverLog = Join-Path $env:TEMP 'emtext-server.log'
  $serverErr = Join-Path $env:TEMP 'emtext-server.err.log'
  foreach ($f in @($serverLog, $serverErr)) { if (Test-Path $f) { Remove-Item $f -Force } }

  Write-Host "starting server on :$Port ..." -ForegroundColor Cyan
  Write-Host "  (server log: $serverErr)" -ForegroundColor DarkGray
  $script:ServerProc = Start-Process -FilePath $py -ArgumentList '-m','server.main' `
    -NoNewWindow -PassThru -RedirectStandardOutput $serverLog -RedirectStandardError $serverErr

  $healthy = $false
  foreach ($i in 1..90) {
    if ($script:ServerProc.HasExited) { throw "server exited during startup (code $($script:ServerProc.ExitCode))" }
    try {
      # 127.0.0.1, NOT localhost. uvicorn binds 0.0.0.0, which is IPv4 only,
      # but on Windows "localhost" resolves to ::1 first -- so Invoke-WebRequest
      # tries IPv6, times out after the full -TimeoutSec, and never falls back.
      # That made a perfectly healthy server look dead for the entire 90s wait.
      $r = Invoke-WebRequest "http://127.0.0.1:$Port/health" -TimeoutSec 2 -UseBasicParsing
      if ($r.StatusCode -eq 200) { $healthy = $true; break }
    } catch {
      if ($i % 10 -eq 0) { Write-Host "  still loading models... ${i}s" -ForegroundColor DarkGray }
      Start-Sleep -Seconds 1
    }
  }
  if (-not $healthy) {
    Write-Host '--- last 30 lines of the server log ---' -ForegroundColor DarkGray
    if (Test-Path $serverErr) { Get-Content $serverErr -Tail 30 }
    throw "server did not become healthy in 90s"
  }
  Write-Host 'server healthy.' -ForegroundColor Green

  if ($NoTunnel) {
    Write-Host ''
    Write-Host "  local only:  http://127.0.0.1:$Port/?token=$token" -ForegroundColor Yellow
    Write-Host ''
    $script:ServerProc.WaitForExit()
    return
  }

  # --- 3. tunnel -----------------------------------------------------------
  if (-not (Get-Command cloudflared -ErrorAction SilentlyContinue)) {
    throw 'cloudflared not on PATH -- see tunnel/README.md'
  }

  $cfConfig = Join-Path $env:USERPROFILE '.cloudflared\config.yml'
  $named = (Test-Path $cfConfig) -and (-not $Quick)
  $log = Join-Path $env:TEMP 'cloudflared-emtext.log'
  if (Test-Path $log) { Remove-Item $log -Force }

  if ($named) {
    # Named tunnel: hostname comes from config.yml, so read it rather than
    # scraping the log (a named tunnel never prints a URL).
    $hostname = (Select-String -Path $cfConfig -Pattern '^\s*-?\s*hostname:\s*(\S+)' |
                 Select-Object -First 1).Matches.Groups[1].Value
    Write-Host "starting named tunnel -> $hostname ..." -ForegroundColor Cyan
    $script:TunnelProc = Start-Process -FilePath 'cloudflared' `
      -ArgumentList 'tunnel','run','emtext' `
      -NoNewWindow -PassThru -RedirectStandardOutput $log -RedirectStandardError "$log.err"
    $base = "https://$hostname"

    $ready = $false
    foreach ($i in 1..40) {
      Start-Sleep -Seconds 1
      if (Test-Path $log) {
        if (Select-String -Path $log -Pattern 'Registered tunnel connection' -Quiet) { $ready = $true; break }
      }
      if (Test-Path "$log.err") {
        if (Select-String -Path "$log.err" -Pattern 'Registered tunnel connection' -Quiet) { $ready = $true; break }
      }
    }
    if (-not $ready) { Write-Host 'warning: no connection registered yet -- check the log below' -ForegroundColor Yellow }
  }
  else {
    Write-Host 'starting quick tunnel (random hostname) ...' -ForegroundColor Cyan
    $script:TunnelProc = Start-Process -FilePath 'cloudflared' `
      -ArgumentList 'tunnel','--url',"http://localhost:$Port" `
      -NoNewWindow -PassThru -RedirectStandardOutput $log -RedirectStandardError "$log.err"

    $base = $null
    foreach ($i in 1..60) {
      Start-Sleep -Seconds 1
      foreach ($f in @($log, "$log.err")) {
        if (Test-Path $f) {
          $m = Select-String -Path $f -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' |
               Select-Object -First 1
          if ($m) { $base = $m.Matches[0].Value; break }
        }
      }
      if ($base) { break }
    }
    if (-not $base) { throw "no tunnel URL found in cloudflared output ($log)" }
  }

  # --- 4. report -----------------------------------------------------------
  $q = "?token=$token"
  Write-Host ''
  Write-Host ('=' * 74) -ForegroundColor DarkGray
  Write-Host '  emtext is up' -ForegroundColor Green
  if (-not $named) { Write-Host '  (quick tunnel -- this hostname changes every restart)' -ForegroundColor DarkYellow }
  Write-Host ''
  Write-Host "  app          $base/$q"
  Write-Host "  dashboard    $base/dashboard.html$q"
  Write-Host "  diagnostics  $base/remote.html$q"
  Write-Host "  health       $base/health"
  Write-Host ''
  Write-Host '  Test from a phone on MOBILE DATA, not WiFi -- on WiFi it may be' -ForegroundColor DarkGray
  Write-Host '  reaching this machine over the LAN and proving nothing.' -ForegroundColor DarkGray
  Write-Host ('=' * 74) -ForegroundColor DarkGray
  Write-Host ''
  Write-Host 'Ctrl+C to stop both.' -ForegroundColor DarkGray

  $script:ServerProc.WaitForExit()
}
finally {
  Stop-All
}
