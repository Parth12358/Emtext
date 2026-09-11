#include "net.h"
#include "../logx.h"
#include "../config/config.h"
#include "../transport/transport.h"
#include <WiFi.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <freertos/queue.h>

// Stage 4a: WiFi (fallback list) + NTP + the logged state machine, running on a
// dedicated core-0 task. The socket step (TLS + WebSocket + auth) is stubbed here
// and filled in by 4b/4c/4d.

namespace {
  volatile net::State st = net::State::Off;
  QueueHandle_t rxq   = nullptr;
  TaskHandle_t  taskh = nullptr;
  void (*cbFrame)(const proto::Frame&) = nullptr;
  uint32_t backoffMs = 1000;
  int      authFails = 0;   // consecutive close-before-ready cycles (bad token)

  const char* NAMES[] = {
    "off", "searching", "connected", "socket-connecting", "ready", "degraded", "halted"
  };

  void setState(net::State s) {
    if (s == st) return;
    st = s;
    LOG_INFO("net -> %s (t=%lu)", NAMES[(int)s], (unsigned long)millis());
  }

  bool connectWiFiOnce(const config::Net& n, uint32_t deadlineMs) {
    if (n.ssid.length() == 0) return false;
    WiFi.disconnect(true);            // stop any in-flight attempt (fixes "cannot set config")
    vTaskDelay(pdMS_TO_TICKS(150));
    LOG_INFO("net: trying ssid '%s'", n.ssid.c_str());
    WiFi.begin(n.ssid.c_str(), n.pass.length() ? n.pass.c_str() : nullptr);
    uint32_t start = millis();
    while (millis() - start < deadlineMs) {
      wl_status_t s = WiFi.status();
      if (s == WL_CONNECTED) return true;
      if (s == WL_NO_SSID_AVAIL || s == WL_CONNECT_FAILED) {
        LOG_WARN("net: ssid '%s' rejected (status=%d)", n.ssid.c_str(), (int)s);
        return false;   // terminal for this AP -> move on
      }
      vTaskDelay(pdMS_TO_TICKS(100));
    }
    LOG_WARN("net: ssid '%s' timed out", n.ssid.c_str());
    return false;
  }

  void scanAndLog() {
    LOG_INFO("net: scanning for visible 2.4GHz APs (S3 is 2.4GHz-only)...");
    int n = WiFi.scanNetworks();
    if (n <= 0) { LOG_WARN("net: scan found no APs"); return; }
    for (int i = 0; i < n && i < 15; i++) {
      LOG_INFO("net:   '%s' rssi=%d ch=%d %s",
               WiFi.SSID(i).c_str(), WiFi.RSSI(i), WiFi.channel(i),
               WiFi.encryptionType(i) == WIFI_AUTH_OPEN ? "open" : "enc");
    }
    WiFi.scanDelete();
  }

  bool connectWiFi() {
    const config::Config& c = config::get();
    if (c.netCount == 0) { LOG_ERR("net: no networks configured (set ssid/pass)"); return false; }
    WiFi.mode(WIFI_STA);
    WiFi.setAutoReconnect(false);    // our state machine owns retries, not the driver
    for (uint8_t i = 0; i < c.netCount; i++) {
      if (connectWiFiOnce(c.nets[i], 10000)) {
        LOG_INFO("net: wifi up ip=%s rssi=%d",
                 WiFi.localIP().toString().c_str(), WiFi.RSSI());
        return true;
      }
    }
    static uint8_t fails = 0;
    if (++fails % 3 == 1) scanAndLog();   // list visible APs so the exact SSID can be matched
    return false;
  }

  void syncTime() {
    // POSIX TZ "UTC0" -- we only need a correct absolute clock for TLS cert dates.
    configTzTime("UTC0", "pool.ntp.org", "time.google.com");
    struct tm tm;
    if (getLocalTime(&tm, 10000)) {
      LOG_INFO("net: time synced %04d-%02d-%02d %02d:%02d UTC",
               tm.tm_year + 1900, tm.tm_mon + 1, tm.tm_mday, tm.tm_hour, tm.tm_min);
    } else {
      LOG_WARN("net: NTP sync timed out (ok while TLS is insecure)");
    }
  }

  void backoff() {
    LOG_INFO("net: backoff %lums", (unsigned long)backoffMs);
    vTaskDelay(pdMS_TO_TICKS(backoffMs));
    backoffMs *= 2;
    if (backoffMs > 5000) backoffMs = 5000;   // 1s -> 5s cap
  }
  void resetBackoff() { backoffMs = 1000; }

  // 5.1: one-shot synthetic burst to prove the TX path + server ingest end-to-end,
  // before any mic/cross-core wiring. Removed in 5.2. Loud noise trips the server VAD;
  // trailing silence lets END_SILENCE_MS close the utterance -> `status: heard` returns.
  void sendTestBurst() {
    LOG_INFO("net: [5.1] sending synthetic audio burst (watch for status:heard)");
    const int CH = 1024;                 // 64 ms at 16 kHz -> 2048 bytes/frame
    static int16_t tb[CH];
    for (int fr = 0; fr < 16; fr++) {    // ~1 s loud noise (>> server SPEECH_RMS=500)
      for (int i = 0; i < CH; i++) tb[i] = (int16_t)random(-6000, 6000);
      transport::sendBin((const uint8_t*)tb, CH * sizeof(int16_t));
      vTaskDelay(pdMS_TO_TICKS(64));
    }
    for (int i = 0; i < CH; i++) tb[i] = 0;
    for (int fr = 0; fr < 13; fr++) {    // ~0.8 s silence -> end-of-utterance
      transport::sendBin((const uint8_t*)tb, CH * sizeof(int16_t));
      vTaskDelay(pdMS_TO_TICKS(64));
    }
    LOG_INFO("net: [5.1] burst done");
  }

  void task(void*) {
    for (;;) {
      setState(net::State::Searching);
      if (!connectWiFi()) { backoff(); continue; }
      setState(net::State::Connected);
      resetBackoff();

      syncTime();

      // 4c: open the WebSocket (TLS handshake happens inside connect), send the
      // token as the first TEXT frame, then pump inbound frames to core 1.
      setState(net::State::SocketConnecting);
      const config::Config& cc = config::get();
      LOG_INFO("net: ws connect wss://%s%s", cc.serverHost.c_str(), cc.serverPath.c_str());
      if (!transport::connect(cc.serverHost.c_str(), 443, cc.serverPath.c_str())) {
        LOG_WARN("net: ws connect failed");
        backoff();
        continue;
      }
      transport::sendText(cc.token.c_str());      // first frame MUST be the auth token
      LOG_INFO("net: ws up, sent token (%d chars)", (int)cc.token.length());

      String   triedToken = cc.token;
      uint32_t openedAt   = millis();
      bool     gotReady   = false;
      bool     sentTest   = false;
      char     buf[512];

      while (transport::connected() && WiFi.status() == WL_CONNECTED) {
        size_t n = transport::poll(buf, sizeof(buf));
        if (n > 0) {
          proto::Frame f;
          proto::parse(buf, f);
          if (f.type == proto::Type::Ready) {
            gotReady = true;
            setState(net::State::Ready);
            resetBackoff();
          }
          xQueueSend(rxq, &f, 0);
        }
        if (gotReady && !sentTest) { sentTest = true; sendTestBurst(); }  // 5.1 one-shot
        vTaskDelay(pdMS_TO_TICKS(2));
      }
      transport::close();

      // Auth-reject inference: the AHC lib can't read the 1008 close code, so a
      // socket that closes quickly BEFORE `ready` is treated as a rejected token.
      // Two such cycles -> halt until the token changes (serial `set token`) or reboot.
      if (!gotReady && (millis() - openedAt) < 5000 && ++authFails >= 2) {
        setState(net::State::Halted);
        LOG_ERR("net: auth rejected (closed before ready) -- halting; fix token then reboot");
        while (config::get().token == triedToken) vTaskDelay(pdMS_TO_TICKS(500));
        LOG_INFO("net: token changed -- resuming");
        authFails = 0;
      } else if (gotReady) {
        authFails = 0;
      }

      LOG_WARN("net: connection closed");
      setState(net::State::Searching);
      backoff();
    }
  }
}

void net::begin() {
  rxq = xQueueCreate(8, sizeof(proto::Frame));
  // 32 KB stack: the TLS handshake is stack-hungry, and the Links2004 backend
  // reaches mbedtls several frames deeper than AHC did -- 16 KB overflowed.
  xTaskCreatePinnedToCore(task, "net", 32768, nullptr, 1, &taskh, 0);   // core 0
  LOG_INFO("net: task started on core 0");
}

void net::loop() {
  if (!rxq) return;
  proto::Frame f;
  while (xQueueReceive(rxq, &f, 0) == pdTRUE) {
    if (cbFrame) cbFrame(f);
  }
}

net::State net::state() { return st; }
const char* net::stateName() { return NAMES[(int)st]; }
void net::onFrame(void (*cb)(const proto::Frame&)) { cbFrame = cb; }
