#include "net.h"
#include "../logx.h"
#include "../config/config.h"
#include "../transport/transport.h"
#include <WiFi.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <freertos/queue.h>
#include <freertos/semphr.h>
#include <esp_heap_caps.h>
#include <string.h>
#include <stdio.h>

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
  const uint32_t PING_MS     = 15000;   // client keepalive interval
  const uint32_t DEGRADED_MS = 45000;   // no server frame this long (socket up) -> degraded
  volatile bool  forceReconnect = false; // set by reconnect() after a config change
  volatile bool  wantPortal = false;     // desired setup-AP state (set from core 1)
  bool           apOn = false;           // actual AP state (task-owned, core 0)

  // Cross-core audio handoff (5.3): a drop-oldest byte ring in PSRAM. core 1 (sendAudio)
  // writes, core 0 (task) drains -> sendBIN. Sized for a ~3 s outage so a WiFi blip loses
  // no audio (requirement Section 2). Mutex-guarded (both cores touch head/tail).
  const size_t      RING_WANT = 3000 * 16 * 2;   // 3 s * 16 samples/ms * 2 bytes = 96000
  uint8_t*          ring = nullptr;
  size_t            ringCap = 0;                 // actual capacity allocated
  size_t            ringHead = 0, ringTail = 0, ringCount = 0;
  SemaphoreHandle_t ringMx = nullptr;
  uint32_t          droppedBytes = 0;

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
    WiFi.disconnect(false);           // drop STA only; keep the setup AP radio up
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
    WiFi.mode(apOn ? WIFI_AP_STA : WIFI_STA);   // keep the setup AP up only if enabled
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
    if (getLocalTime(&tm, 4000)) {              // short wait; NTP keeps syncing in background
      LOG_INFO("net: time synced %04d-%02d-%02d %02d:%02d UTC",
               tm.tm_year + 1900, tm.tm_mon + 1, tm.tm_mday, tm.tm_hour, tm.tm_min);
    } else {
      LOG_WARN("net: NTP not synced yet (ok while TLS is insecure)");
    }
  }

  void backoff() {
    LOG_INFO("net: backoff %lums", (unsigned long)backoffMs);
    vTaskDelay(pdMS_TO_TICKS(backoffMs));
    backoffMs *= 2;
    if (backoffMs > 5000) backoffMs = 5000;   // 1s -> 5s cap
  }
  void resetBackoff() { backoffMs = 1000; }

  // Apply a pending setup-AP toggle. Runs only on the net task (core 0) so all radio
  // ops stay single-threaded; setPortal() from core 1 just flips `wantPortal`.
  void applyPortal() {
    if (wantPortal == apOn) return;
    const config::Config& c = config::get();
    if (wantPortal) {
      WiFi.mode(WIFI_AP_STA);
      WiFi.softAP(c.apSsid.c_str(), c.apPass.c_str());
      LOG_INFO("net: setup AP ON '%s' at %s", c.apSsid.c_str(),
               WiFi.softAPIP().toString().c_str());
    } else {
      WiFi.softAPdisconnect(false);   // stop the AP, keep STA
      WiFi.mode(WIFI_STA);
      LOG_INFO("net: setup AP OFF");
    }
    apOn = wantPortal;
  }

  void task(void*) {
    for (;;) {
      applyPortal();
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
      char     buf[512];
      uint32_t lastRx     = millis();
      uint32_t lastPing   = millis();

      while (transport::connected() && WiFi.status() == WL_CONNECTED && !forceReconnect) {
        applyPortal();
        size_t n = transport::poll(buf, sizeof(buf));
        if (n > 0) {
          lastRx = millis();
          if (gotReady && st == net::State::Degraded) setState(net::State::Ready);   // recovered
          proto::Frame f;
          proto::parse(buf, f);
          if (f.type == proto::Type::Ready) {
            gotReady = true;
            setState(net::State::Ready);
            resetBackoff();
          }
          xQueueSend(rxq, &f, 0);
        }

        // 5.4: client keepalive ping (server echoes pong -> keeps a quiet link fresh).
        if (gotReady && millis() - lastPing >= PING_MS) {
          lastPing = millis();
          char pb[48];
          snprintf(pb, sizeof(pb), "{\"type\":\"ping\",\"t\":%lu}", (unsigned long)millis());
          transport::sendText(pb);
        }
        // 5.4: no server frame for DEGRADED_MS while the socket is still up = degraded.
        // Keep the socket and keep buffering into the ring -- do NOT tear down.
        if (gotReady && st == net::State::Ready && (millis() - lastRx) > DEGRADED_MS) {
          setState(net::State::Degraded);
        }
        if (gotReady) {                          // 5.3: drain the PSRAM ring -> server
          static uint8_t txbuf[2048];            // ~64 ms per frame
          for (;;) {
            xSemaphoreTake(ringMx, portMAX_DELAY);
            size_t take = (ringCount < sizeof(txbuf)) ? ringCount : sizeof(txbuf);
            take &= ~((size_t)1);                // keep int16-aligned
            if (take) {
              size_t first = ringCap - ringTail;
              if (first > take) first = take;
              memcpy(txbuf, ring + ringTail, first);
              if (take > first) memcpy(txbuf + first, ring, take - first);
              ringTail = (ringTail + take) % ringCap;
              ringCount -= take;
            }
            xSemaphoreGive(ringMx);
            if (!take) break;
            transport::sendBin(txbuf, take);
          }
        }
        vTaskDelay(pdMS_TO_TICKS(2));
      }
      transport::close();
      forceReconnect = false;   // consumed

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
  ringMx = xSemaphoreCreateMutex();
  ring = (uint8_t*)heap_caps_malloc(RING_WANT, MALLOC_CAP_SPIRAM);
  ringCap = RING_WANT;
  if (!ring) {                                 // no PSRAM -> smaller internal fallback
    ringCap = 16 * 1024;
    ring = (uint8_t*)heap_caps_malloc(ringCap, MALLOC_CAP_8BIT);
    LOG_WARN("net: no PSRAM; TX ring = %u B internal", (unsigned)ringCap);
  } else {
    LOG_INFO("net: TX ring %u B in PSRAM (~%us)", (unsigned)ringCap,
             (unsigned)(ringCap / (16 * 2) / 1000));
  }
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

// Called on core 1 (from audio::onChunk). Copies the chunk into the TX queue,
// dropping the oldest if it's full, so a network stall never blocks capture.
void net::sendAudio(const int16_t* pcm, size_t n) {
  if (!ring || !ringMx) return;
  size_t bytes = n * sizeof(int16_t);
  const uint8_t* src = (const uint8_t*)pcm;
  if (bytes > ringCap) { src += (bytes - ringCap); bytes = ringCap; }   // clamp (never hit)

  xSemaphoreTake(ringMx, portMAX_DELAY);
  if (ringCount + bytes > ringCap) {           // drop oldest to make room
    size_t drop = ringCount + bytes - ringCap;
    ringTail = (ringTail + drop) % ringCap;
    ringCount -= drop;
    droppedBytes += drop;
  }
  size_t first = ringCap - ringHead;
  if (first > bytes) first = bytes;
  memcpy(ring + ringHead, src, first);
  if (bytes > first) memcpy(ring, src + first, bytes - first);
  ringHead = (ringHead + bytes) % ringCap;
  ringCount += bytes;
  xSemaphoreGive(ringMx);
}

void net::reconnect() {
  forceReconnect = true;
  backoffMs = 1000;   // reconnect promptly after a config change
}

void net::setPortal(bool on) { wantPortal = on; }
bool net::portalOn()         { return apOn; }
