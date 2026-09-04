# Reaching emtext from outside the LAN — Cloudflare Quick Tunnel

A quick tunnel gives the local server a public **HTTPS** address in one command:
no Cloudflare account, no domain, no DNS records, no port forwarding, and it
works behind NAT and CGNAT.

That HTTPS matters more than it looks. Browsers only allow microphone access on a
**secure origin**, and `localhost` is the only insecure origin they treat as
secure. Without a tunnel you cannot test the mic from a phone at all — not on the
same WiFi, not anywhere. The tunnel is what makes real-device testing possible.

**This is the intended setup for now.** A named tunnel on your own domain gives a
stable hostname and can sit behind a login (Cloudflare Access), and is the right
answer once the ESP32 pendant is real. It needs an account and a domain, so it
can wait.

---

## 1. Install cloudflared

```powershell
# Windows
winget install --id Cloudflare.cloudflared
```

```bash
# Debian / Ubuntu
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
  | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt update && sudo apt install cloudflared

# macOS
brew install cloudflared
```

Or grab a binary straight from
[github.com/cloudflare/cloudflared/releases](https://github.com/cloudflare/cloudflared/releases)
and put it on your PATH. Check it works:

```
cloudflared --version
```

## 2. Set AUTH_TOKEN first

**Do this before the tunnel, not after.** With `AUTH_TOKEN` unset the server
accepts *any* client — there is no rate limit, no connection cap, and no origin
check. A quick-tunnel hostname is random, but random is not secret: it travels
through Cloudflare, lands in your shell history, and anyone who obtains it gets
unmetered use of your CPU and GPU.

```powershell
# Windows
$env:AUTH_TOKEN = (python -c "import secrets; print(secrets.token_urlsafe(32))")
echo $env:AUTH_TOKEN
```

```bash
# Linux / macOS
export AUTH_TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
echo "$AUTH_TOKEN"
```

The server prints a loud warning at startup if this is unset.

## 3. Start the server

```
python -m server.main
```

## 4. Start the tunnel

In a second terminal:

```
cloudflared tunnel --url http://localhost:8000
```

It prints a banner with a random hostname:

```
+--------------------------------------------------------------------------------------------+
|  Your quick Tunnel has been created! Visit it at:                                           |
|  https://sudden-marble-cats-refer.trycloudflare.com                                         |
+--------------------------------------------------------------------------------------------+
```

**That URL changes every single time you restart the tunnel.** There is no way to
keep it; that is the trade for needing no account. Copy it fresh each run.

Or use the helper, which starts both and prints the URL prominently:

```powershell
tunnel\start.bat          # Windows
```
```bash
./tunnel/start.sh         # Linux / macOS
```

## 5. Test it

Open the diagnostic page on the phone you want to test from — **on mobile data,
not WiFi**, since WiFi could be reaching the machine over the LAN and proving
nothing:

```
https://<random>.trycloudflare.com/remote.html?token=<AUTH_TOKEN>
```

It checks, in order: secure origin, `/health` with round-trip time, websocket
scheme, connect, auth, keepalive latency, and whether the `voice` field is
arriving. Then **Stream mic** runs the whole path — mic → tunnel → Whisper →
Ollama → read — and shows the end-to-end latency.

The normal app is at `https://<random>.trycloudflare.com/?token=<AUTH_TOKEN>`.

---

## Troubleshooting

**502 Bad Gateway**
Cloudflare reached the tunnel but the tunnel could not reach the server. The
local server is not running, or is not on port 8000. Confirm with
`curl http://localhost:8000/health` — it should return `{"status":"ok"}`.

**The page loads but the websocket never connects**
Almost always a `ws://` vs `wss://` problem. Everything through a tunnel is
HTTPS, and a browser silently blocks a `ws://` connection from an `https://` page
as mixed content. Both `index.html` and `remote.html` derive the scheme from
`location.protocol`, so this should not happen — but any new client, and the
ESP32 firmware, must do the same. Check the browser console for a mixed-content
error.

**Connected fine, then drops after ~100 seconds of silence**
Cloudflare closes a proxied websocket after ~100 s with no traffic in either
direction on Free and Pro plans. Three things already prevent this: uvicorn sends
protocol-level pings every 20 s, the server sends its own `{"type":"ping"}` after
`WS_IDLE_PING_S` (30 s) without audio, and `remote.html` pings every 20 s. If you
are still seeing it, the keepalives are not firing — watch the message log in
`remote.html`, which timestamps every frame, and check `WS_IDLE_PING_S`.

**Closed immediately with code 1008**
The token was rejected. `1008` is what `server/main.py` sends when the first
frame does not match `AUTH_TOKEN`. Check for a stale token in the URL, or a shell
where `AUTH_TOKEN` was never exported.

**"Permission denied" or no microphone prompt**
The page is not a secure context. `remote.html` shows this as the first check.
You are almost certainly on `http://` — use the `https://` tunnel URL.

**The tunnel URL 404s or is dead**
Quick tunnels are ephemeral. If cloudflared restarted, the old hostname is gone
and there is a new one in its output.

**It works on WiFi but not on mobile data**
Then it was never going through the tunnel — the phone was reaching the machine
directly over the LAN. Mobile data is the real test.

---

## Notes

- **Cost: nothing.** Cloudflare Tunnel is free, and a quick tunnel needs no
  account at all.
- **Bandwidth** is about 32 KB/s upstream per client (16 kHz mono int16). Note
  Cloudflare's terms restrict serving large media over its CDN on non-paid plans;
  at this volume it is not a practical concern, but it is worth knowing.
- **The server knows nothing about any of this.** No tunnel code, no special
  configuration — it serves HTTP on `0.0.0.0:8000` exactly as it does locally,
  and cloudflared connects to it over loopback.
- **For the ESP32 later:** it will need `wss://` and therefore TLS, which costs
  roughly 20–40 KB of RAM on top of the websocket. Comfortable on an ESP32-S3
  with PSRAM, tight on a plain ESP32 once audio buffers are added.
