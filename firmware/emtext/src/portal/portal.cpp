#include "portal.h"
#include "../logx.h"
#include "../config/config.h"
#include "../net/net.h"
#include <WiFi.h>
#include <WebServer.h>
#include <DNSServer.h>

// Self-contained config page (no external assets -- the phone joins the AP with no
// internet). %SSID%/%HOST% are filled with the current config before serving. Keep this
// in sync with src/portal/portal_page.html (the design source).

namespace {
  WebServer server(80);
  DNSServer dns;
  bool wantActive = false;   // requested serving state (from setActive)
  bool dnsUp = false;        // captive DNS currently running

  const char PAGE[] = R"HTML(<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>emtext setup</title>
<style>
  :root{--bg:#0e1116;--card:#161b22;--fg:#e8ebf0;--muted:#8b93a3;--accent:#4a9eff;--line:#242b36;--field:#0f141b}
  *{box-sizing:border-box}html,body{margin:0}
  body{background:var(--bg);color:var(--fg);font:16px/1.45 -apple-system,system-ui,Segoe UI,Roboto,sans-serif;-webkit-font-smoothing:antialiased;padding:env(safe-area-inset-top) 0 env(safe-area-inset-bottom)}
  .wrap{max-width:440px;margin:0 auto;padding:26px 18px 48px}
  header{text-align:center;padding:22px 0 6px}
  .dot{width:9px;height:9px;border-radius:50%;background:var(--accent);display:inline-block;margin-right:7px;vertical-align:middle}
  .logo{font-weight:700;font-size:22px;letter-spacing:.3px;vertical-align:middle}
  .sub{color:var(--muted);font-size:13px;margin-top:5px}
  form{margin-top:16px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px 16px}
  label{display:block;font-size:12px;font-weight:600;letter-spacing:.3px;text-transform:uppercase;color:var(--muted);margin:16px 0 7px}
  label:first-of-type{margin-top:2px}
  input{width:100%;padding:13px 12px;font-size:16px;color:var(--fg);background:var(--field);border:1px solid var(--line);border-radius:10px;outline:none}
  input:focus{border-color:var(--accent)}input::placeholder{color:#59606e}
  .hint{font-size:12px;color:var(--muted);margin-top:6px}
  button{width:100%;margin-top:22px;padding:14px;font-size:16px;font-weight:600;color:#fff;background:var(--accent);border:0;border-radius:11px}
  button:active{filter:brightness(.9)}
  .foot{text-align:center;color:var(--muted);font-size:12px;margin-top:18px}
</style></head><body>
<div class="wrap">
  <header><span class="dot"></span><span class="logo">emtext</span><div class="sub">pendant Wi-Fi setup</div></header>
  <form method="POST" action="/save">
    <div class="card">
      <label>Wi-Fi network</label>
      <input name="ssid" value="%SSID%" placeholder="network name" autocapitalize="off" autocorrect="off" spellcheck="false">
      <label>Wi-Fi password</label>
      <input name="pass" type="password" placeholder="leave blank to keep current">
      <label>Server host</label>
      <input name="host" value="%HOST%" placeholder="emtext.example.com" autocapitalize="off" autocorrect="off" spellcheck="false">
      <div class="hint">hostname only — no https:// and no /stream</div>
      <label>Access token</label>
      <input name="token" type="password" placeholder="leave blank to keep current">
      <button type="submit">Save &amp; reconnect</button>
    </div>
  </form>
  <div class="foot">The pendant saves this and reconnects. You can then rejoin your own Wi-Fi.</div>
</div></body></html>)HTML";

  String render() {
    String p = PAGE;
    p.replace("%SSID%", config::get().nets[0].ssid);
    p.replace("%HOST%", config::get().serverHost);
    return p;
  }

  void handleRoot() { server.send(200, "text/html", render()); }

  void handleSave() {
    const config::Config& c = config::get();
    String ssid  = server.arg("ssid");
    String pass  = server.arg("pass");
    String host  = server.arg("host");
    String token = server.arg("token");
    if (ssid.length())  config::setWifi(ssid, pass.length() ? pass : c.nets[0].pass);
    if (host.length())  config::setHost(host);
    if (token.length()) config::setToken(token);
    config::save();
    LOG_INFO("portal: config saved via AP -> reconnecting");

    String shown = ssid.length() ? ssid : c.nets[0].ssid;
    String body =
      "<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
      "<body style='font-family:-apple-system,system-ui,sans-serif;background:#0e1116;"
      "color:#e8ebf0;text-align:center;padding:48px 20px'>"
      "<h2 style='color:#4a9eff'>Saved</h2>"
      "<p>Reconnecting to <b>" + shown + "</b>&hellip;</p>"
      "<p style='color:#8b93a3;font-size:14px'>You can rejoin your own Wi-Fi now.</p></body>";
    server.send(200, "text/html", body);
    net::reconnect();
  }

  // Captive portal: return the config page for ANY unmatched URL -- including the OS
  // reachability probes (captive.apple.com, connectivitycheck.gstatic.com/generate_204,
  // msftconnecttest.com, ...). Getting the form instead of the expected "success"/204
  // makes iOS/Android/Windows pop their "sign in to network" sheet automatically.
  void handleNotFound() {
    server.send(200, "text/html", render());
  }
}

void portal::begin() {
  // Only register handlers here -- do NOT touch the network stack at boot (starting
  // the WebServer before WiFi is initialized raced net's WiFi bring-up and asserted).
  server.on("/", handleRoot);
  server.on("/save", HTTP_POST, handleSave);
  server.onNotFound(handleNotFound);
  LOG_INFO("portal: registered (enable the setup AP from the Status screen)");
}

void portal::setActive(bool on) { wantActive = on; }

void portal::loop() {
  // Start the WebServer + captive DNS only once the setup AP is actually up (net owns
  // it, so ask net rather than poking WiFi here). Stop them when it goes down.
  bool serve = wantActive && net::portalOn();
  if (serve && !dnsUp) {
    server.begin();
    dns.start(53, "*", WiFi.softAPIP());
    dnsUp = true;
    LOG_INFO("portal: serving at http://%s/", WiFi.softAPIP().toString().c_str());
  } else if (!serve && dnsUp) {
    dns.stop();
    server.stop();
    dnsUp = false;
  }
  if (dnsUp) { server.handleClient(); dns.processNextRequest(); }
}
