#!/usr/bin/env bash
# Start the emtext server and a Cloudflare quick tunnel together, then print the
# public URL prominently -- cloudflared buries it in a banner among startup logs,
# and it changes on every run, so it is the one thing you always need and always
# have to hunt for.
#
#   ./tunnel/start.sh
#
# Ctrl+C stops both.
set -uo pipefail

cd "$(dirname "$0")/.."

PORT="${PORT:-8000}"
PY="python"
[ -x ".venv/bin/python" ] && PY=".venv/bin/python"
[ -x ".venv/Scripts/python.exe" ] && PY=".venv/Scripts/python.exe"

command -v cloudflared >/dev/null 2>&1 || {
  echo "cloudflared not found on PATH -- see tunnel/README.md for install steps." >&2
  exit 1
}

# Refuse to expose an unauthenticated server. The hostname is random, but random
# is not secret, and there is no rate limit behind it.
if [ -z "${AUTH_TOKEN:-}" ]; then
  echo
  echo "AUTH_TOKEN is not set. Refusing to open a public tunnel to an open server."
  echo
  echo "  export AUTH_TOKEN=\$($PY -c 'import secrets; print(secrets.token_urlsafe(32))')"
  echo
  echo "Set it and run again. (To run locally without a tunnel, just start the"
  echo "server directly: $PY -m server.main)"
  exit 1
fi

LOG="$(mktemp -t cloudflared.XXXXXX)"
SERVER_PID=""
TUNNEL_PID=""

cleanup() {
  echo
  echo "shutting down..."
  [ -n "$TUNNEL_PID" ] && kill "$TUNNEL_PID" 2>/dev/null
  [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null
  wait 2>/dev/null
  rm -f "$LOG"
}
trap cleanup EXIT INT TERM

echo "starting server on :$PORT ..."
"$PY" -m server.main &
SERVER_PID=$!

# Wait for the server before opening the tunnel, otherwise the first requests
# through it return 502 and look like a tunnel fault rather than a race.
for _ in $(seq 1 60); do
  if curl -fsS "http://localhost:$PORT/health" >/dev/null 2>&1; then break; fi
  kill -0 "$SERVER_PID" 2>/dev/null || { echo "server exited during startup" >&2; exit 1; }
  sleep 1
done
curl -fsS "http://localhost:$PORT/health" >/dev/null 2>&1 \
  || { echo "server did not become healthy in 60s" >&2; exit 1; }
echo "server healthy."

echo "starting cloudflared quick tunnel ..."
cloudflared tunnel --url "http://localhost:$PORT" >"$LOG" 2>&1 &
TUNNEL_PID=$!

# cloudflared prints the hostname inside an ASCII banner; grep it out.
URL=""
for _ in $(seq 1 60); do
  URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" | head -1 || true)"
  [ -n "$URL" ] && break
  kill -0 "$TUNNEL_PID" 2>/dev/null || { echo "cloudflared exited:"; cat "$LOG"; exit 1; }
  sleep 1
done

if [ -z "$URL" ]; then
  echo "could not find a tunnel URL in cloudflared output:" >&2
  cat "$LOG" >&2
  exit 1
fi

cat <<BANNER

========================================================================
  TUNNEL UP  (this URL changes every restart)

  app          $URL/?token=$AUTH_TOKEN
  diagnostics  $URL/remote.html?token=$AUTH_TOKEN
  health       $URL/health

  Test from a phone on MOBILE DATA, not WiFi -- on WiFi it may be
  reaching this machine over the LAN and proving nothing.
========================================================================

BANNER

# Stream cloudflared's ongoing output so connection errors stay visible.
tail -f "$LOG" &
wait "$SERVER_PID"
