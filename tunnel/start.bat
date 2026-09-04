@echo off
REM Start the emtext server and a Cloudflare quick tunnel together, then print
REM the public URL prominently -- cloudflared buries it in a banner among its
REM startup logs, and it changes on every run, so it is the one thing you always
REM need and always have to hunt for.
REM
REM   tunnel\start.bat
REM
REM Closes both windows when you are done.
setlocal EnableDelayedExpansion
cd /d "%~dp0.."

if "%PORT%"=="" set PORT=8000

set PY=python
if exist ".venv\Scripts\python.exe" set PY=.venv\Scripts\python.exe

where cloudflared >nul 2>&1
if errorlevel 1 (
  echo cloudflared not found on PATH -- see tunnel\README.md for install steps.
  exit /b 1
)

REM Refuse to expose an unauthenticated server. The hostname is random, but
REM random is not secret, and there is no rate limit behind it.
if "%AUTH_TOKEN%"=="" (
  echo.
  echo AUTH_TOKEN is not set. Refusing to open a public tunnel to an open server.
  echo.
  echo   for /f %%%%i in ^('%PY% -c "import secrets; print(secrets.token_urlsafe(32))"'^) do set AUTH_TOKEN=%%%%i
  echo.
  echo Set it and run again. To run locally without a tunnel, just start the
  echo server directly:  %PY% -m server.main
  exit /b 1
)

set LOG=%TEMP%\cloudflared-emtext.log
if exist "%LOG%" del "%LOG%"

echo starting server on :%PORT% ...
start "emtext server" %PY% -m server.main

REM Wait for the server before opening the tunnel, otherwise the first requests
REM through it return 502 and look like a tunnel fault rather than a race.
set READY=0
for /l %%i in (1,1,60) do (
  if !READY!==0 (
    curl -fsS "http://localhost:%PORT%/health" >nul 2>&1
    if not errorlevel 1 (
      set READY=1
    ) else (
      timeout /t 1 /nobreak >nul
    )
  )
)
if !READY!==0 (
  echo server did not become healthy in 60s
  exit /b 1
)
echo server healthy.

echo starting cloudflared quick tunnel ...
start "cloudflared" cmd /c "cloudflared tunnel --url http://localhost:%PORT% > "%LOG%" 2>&1"

REM cloudflared prints the hostname inside an ASCII banner; pull it back out.
set URL=
for /l %%i in (1,1,60) do (
  if "!URL!"=="" (
    timeout /t 1 /nobreak >nul
    for /f "tokens=*" %%u in ('findstr /r /c:"https://[a-z0-9-]*\.trycloudflare\.com" "%LOG%" 2^>nul') do (
      for %%t in (%%u) do (
        echo %%t | findstr /r /c:"https://.*\.trycloudflare\.com" >nul && if "!URL!"=="" set URL=%%t
      )
    )
  )
)

if "!URL!"=="" (
  echo could not find a tunnel URL in cloudflared output:
  type "%LOG%"
  exit /b 1
)

echo.
echo ========================================================================
echo   TUNNEL UP  ^(this URL changes every restart^)
echo.
echo   app          !URL!/?token=%AUTH_TOKEN%
echo   diagnostics  !URL!/remote.html?token=%AUTH_TOKEN%
echo   health       !URL!/health
echo.
echo   Test from a phone on MOBILE DATA, not WiFi -- on WiFi it may be
echo   reaching this machine over the LAN and proving nothing.
echo ========================================================================
echo.
echo Server and tunnel are running in their own windows. Close them to stop.
pause
