#include "net.h"
#include "../logx.h"
#include "../config/config.h"
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

  bool connectWiFi() {
    const config::Config& c = config::get();
    if (c.netCount == 0) { LOG_ERR("net: no networks configured (set ssid/pass)"); return false; }
    WiFi.mode(WIFI_STA);
    for (uint8_t i = 0; i < c.netCount; i++) {
      if (connectWiFiOnce(c.nets[i], 10000)) {
        LOG_INFO("net: wifi up ip=%s rssi=%d",
                 WiFi.localIP().toString().c_str(), WiFi.RSSI());
        return true;
      }
    }
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

  void task(void*) {
    for (;;) {
      setState(net::State::Searching);
      if (!connectWiFi()) { backoff(); continue; }
      setState(net::State::Connected);
      resetBackoff();

      syncTime();

      // --- 4a stub: 4b adds tls.setInsecure()+connect(host,443); 4c ws.begin()+auth ---
      setState(net::State::SocketConnecting);
      LOG_INFO("net: [stub] TLS+WebSocket connect goes here (Stage 4b/4c)");

      // Hold until WiFi drops, then rebuild the connection.
      while (WiFi.status() == WL_CONNECTED) {
        vTaskDelay(pdMS_TO_TICKS(500));
      }
      LOG_WARN("net: wifi lost");
      backoff();
    }
  }
}

void net::begin() {
  rxq = xQueueCreate(8, sizeof(proto::Frame));
  // 16 KB stack: the TLS handshake (Stage 4c+) is stack-hungry.
  xTaskCreatePinnedToCore(task, "net", 16384, nullptr, 1, &taskh, 0);   // core 0
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
