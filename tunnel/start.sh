#!/usr/bin/env bash
# Start the emtext server and a Cloudflare tunnel together, with a persisted
# AUTH_TOKEN, and print the URLs.
#
#   ./tunnel/start.sh                 # named tunnel if configured, else quick
#   ./tunnel/start.sh --rotate-token  # replace the token first
#   ./tunnel/start.sh --quick         # force a quick tunnel
#   ./tunnel/start.sh --no-tunnel     # server only
#
# The AUTH_TOKEN is STABLE across restarts, so bookmarks and phone tabs keep
# working. Rotate explicitly with --rotate-token when a token has been exposed
# (a screenshot, a shared terminal, a chat log). Rotation is not a defence
# against guessing -- 32 random bytes is 256 bits, which is not searchable --
# it only bounds the damage from a token that has actually leaked.
#
# Ctrl+C stops both. The PowerShell twin is tunnel/start.ps1; both call
# tunnel/token.py so the token logic exists once, not twice.
set -uo pipefail
cd "$(dirname "$0")/.."

PORT="${PORT:-8000}"
# Stable by default; --rotate-token opts in.
ROTATE=""; FORCE_QUICK=""; NO_TUNNEL=""
for arg in "$@"; do
  case "$arg" in
    --rotate-token) ROTATE="--rotate" ;;
    --keep-token)   ROTATE="" ;;           # accepted, though it is the default
    --quick)        FORCE_QUICK=1 ;;
    --no-tunnel)    NO_TUNNEL=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

PY="python"
[ -x ".venv/bin/python" ] && PY=".venv/bin/python"
[ -x ".venv/Scripts/python.exe" ] && PY=".venv/Scripts/python.exe"

# --- 1. token ---------------------------------------------------------------
# The server accepts ANY client when AUTH_TOKEN is unset, so a public tunnel
# over an unauthenticated server is the failure worth engineering away.
TOKEN="$("$PY" tunnel/token.py $ROTATE)" || { echo "could not obtain AUTH_TOKEN" >&2; exit 1; }
[ -n "$TOKEN" ] || { echo "empty AUTH_TOKEN" >&2; exit 1; }
export AUTH_TOKEN="$TOKEN"
# Without this the Windows HF cache needs symlink privileges and first-time
# model downloads fail with WinError 1314.
export HF_HUB_DISABLE_SYMLINKS=1
export HF_HUB_DISABLE_SYMLINKS_WARNING=1
if [ -n "$ROTATE" ]; then
  echo "auth token ROTATED (${#TOKEN} chars) -- previously-opened URLs are now dead"
else
  echo "auth token (${#TOKEN} chars) from $("$PY" tunnel/token.py --path)"
fi

SERVER_PID=""; TUNNEL_PID=""
LOG="$(mktemp -t cloudflared.XXXXXX)"
cleanup() {
  echo; echo "shutting down..."
  [ -n "$TUNNEL_PID" ] && kill "$TUNNEL_PID" 2>/dev/null
  [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null
  wait 2>/dev/null
  rm -f "$LOG"
}
trap cleanup EXIT INT TERM

# --- 2. server --------------------------------------------------------------
echo "starting server on :$PORT ..."
"$PY" -m server.main &
SERVER_PID=$!

# Wait for health BEFORE opening the tunnel: otherwise the first requests
# through it return 502, which reads as a tunnel fault rather than a race.
# 127.0.0.1 rather than localhost: uvicorn binds 0.0.0.0 (IPv4 only) and on
# Windows localhost resolves to ::1 first, which simply refuses.
for _ in $(seq 1 90); do
  curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
  kill -0 "$SERVER_PID" 2>/dev/null || { echo "server exited during startup" >&2; exit 1; }
  sleep 1
done
curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 \
  || { echo "server did not become healthy in 90s" >&2; exit 1; }
echo "server healthy."

if [ -n "$NO_TUNNEL" ]; then
  echo
  echo "  local only:  http://127.0.0.1:$PORT/?token=$TOKEN"
  echo
  wait "$SERVER_PID"
  exit 0
fi

# --- 3. tunnel --------------------------------------------------------------
command -v cloudflared >/dev/null 2>&1 \
  || { echo "cloudflared not on PATH -- see tunnel/README.md" >&2; exit 1; }

# Find config.yml without doing path translation by hand. On Git Bash for
# Windows $USERPROFILE is backslashed and $HOME is not, and they can point at
# different places -- so just try both rather than rewriting separators.
CF_CONFIG=""
for candidate in "$HOME/.cloudflared/config.yml" "${USERPROFILE:-}/.cloudflared/config.yml"; do
  [ -f "$candidate" ] && { CF_CONFIG="$candidate"; break; }
done

if [ -n "$CF_CONFIG" ] && [ -z "$FORCE_QUICK" ]; then
  # Named tunnel: the hostname is in config.yml. A named tunnel never prints a
  # URL, so read it rather than scraping the log.
  HOSTNAME="$(grep -oE '^\s*-?\s*hostname:\s*\S+' "$CF_CONFIG" | head -1 | awk '{print $NF}')"
  echo "starting named tunnel -> $HOSTNAME ..."
  cloudflared tunnel run emtext >"$LOG" 2>&1 &
  TUNNEL_PID=$!
  BASE="https://$HOSTNAME"
  for _ in $(seq 1 40); do
    grep -q "Registered tunnel connection" "$LOG" 2>/dev/null && break
    kill -0 "$TUNNEL_PID" 2>/dev/null || { echo "cloudflared exited:"; cat "$LOG"; exit 1; }
    sleep 1
  done
  grep -q "Registered tunnel connection" "$LOG" 2>/dev/null \
    || echo "warning: no connection registered yet -- check $LOG"
  NAMED=1
else
  echo "starting quick tunnel (random hostname) ..."
  cloudflared tunnel --url "http://localhost:$PORT" >"$LOG" 2>&1 &
  TUNNEL_PID=$!
  BASE=""
  for _ in $(seq 1 60); do
    BASE="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" | head -1 || true)"
    [ -n "$BASE" ] && break
    kill -0 "$TUNNEL_PID" 2>/dev/null || { echo "cloudflared exited:"; cat "$LOG"; exit 1; }
    sleep 1
  done
  [ -n "$BASE" ] || { echo "no tunnel URL found in cloudflared output:" >&2; cat "$LOG" >&2; exit 1; }
  NAMED=""
fi

# --- 4. report --------------------------------------------------------------
Q="?token=$TOKEN"
cat <<BANNER

==========================================================================
  emtext is up
BANNER
[ -z "$NAMED" ] && echo "  (quick tunnel -- this hostname changes every restart)"
cat <<BANNER

  app          $BASE/$Q
  dashboard    $BASE/dashboard.html$Q
  diagnostics  $BASE/remote.html$Q
  health       $BASE/health

  Test from a phone on MOBILE DATA, not WiFi -- on WiFi it may be
  reaching this machine over the LAN and proving nothing.
==========================================================================

Ctrl+C to stop both.

BANNER

tail -f "$LOG" &
wait "$SERVER_PID"
