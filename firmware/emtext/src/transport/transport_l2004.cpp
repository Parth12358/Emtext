#include "transport.h"
#if TRANSPORT_BACKEND_L2004

#include "../logx.h"
#include "certs.h"                // Google Trust Services roots (R1-R4)
#include <WebSocketsClient.h>     // Links2004 arduinoWebSockets
#include <string.h>

// DIAGNOSTIC: 1 = beginSSL insecure (skip cert validation) to isolate whether the
// connect failure is TLS-cert vs the WS upgrade. Set back to 0 once diagnosed.
#define L2004_INSECURE 1

// Links2004 backend: event-callback + loop() model, adapted to the poll() interface.
// sendBIN() sends a full chunk as ONE frame (no sub-framing), which is what makes
// real-time streaming feasible. Used only from net's core-0 task (single-threaded).

namespace {
  WebSocketsClient* ws = nullptr;
  bool connected_ = false;

  // Small ring of inbound TEXT frames. onEvent (producer) runs synchronously inside
  // ws->loop(), same thread as poll() (consumer) -- no cross-core, no locking needed.
  const int    RXN   = 8;
  const size_t RXCAP = 512;
  char  rxbuf[RXN][RXCAP];
  size_t rxlen[RXN] = {0};
  int   rxHead = 0, rxTail = 0;

  void onEvent(WStype_t type, uint8_t* payload, size_t length) {
    switch (type) {
      case WStype_CONNECTED:    connected_ = true;  LOG_INFO("transport: ws connected"); break;
      case WStype_DISCONNECTED: connected_ = false; LOG_WARN("transport: ws disconnected"); break;
      case WStype_TEXT: {
        int next = (rxHead + 1) % RXN;
        if (next != rxTail) {                 // drop if the ring is full
          size_t n = (length < RXCAP - 1) ? length : RXCAP - 1;
          memcpy(rxbuf[rxHead], payload, n);
          rxbuf[rxHead][n] = 0;
          rxlen[rxHead] = n;
          rxHead = next;
        }
        break;
      }
      default: break;                          // BIN/PING/PONG/fragment ignored
    }
  }
}

bool transport::connect(const char* host, uint16_t port, const char* path) {
  close();
  ws = new WebSocketsClient();
  ws->onEvent(onEvent);
  // Leave _reconnectInterval at its 500 ms default: a huge value makes loop()'s
  // "(millis - lastFail) < interval" gate always true, so it never attempts the
  // connect. net owns the OUTER reconnect; on DISCONNECTED it close()s us at once,
  // so the library's internal retry never competes with net's state machine.
  connected_ = false;
  rxHead = rxTail = 0;
#if L2004_INSECURE
  ws->beginSSL(host, port, path);                        // DIAGNOSTIC: skip cert validation
#else
  ws->beginSslWithCA(host, port, path, GTS_ROOTS_PEM);   // TLS handshake + WS upgrade
#endif

  uint32_t start = millis();                   // pump until connected (event) or timeout
  while (!connected_ && millis() - start < 15000) {
    ws->loop();
    delay(5);
  }
  if (!connected_) { LOG_WARN("transport: ws connect timeout"); close(); return false; }
  return true;
}

bool transport::sendText(const char* s) { return ws && ws->sendTXT(s); }

bool transport::sendBin(const uint8_t* data, size_t n) {
  return ws && ws->sendBIN(data, n);           // whole chunk, one frame
}

size_t transport::poll(char* buf, size_t cap) {
  if (!ws) return 0;
  ws->loop();                                  // pump; onEvent fills the ring
  if (rxTail == rxHead) return 0;
  size_t n = rxlen[rxTail];
  if (n >= cap) n = cap - 1;
  memcpy(buf, rxbuf[rxTail], n);
  buf[n] = 0;
  rxTail = (rxTail + 1) % RXN;
  return n;
}

bool transport::connected() { return ws && connected_; }

void transport::close() {
  if (ws) { ws->disconnect(); delete ws; ws = nullptr; }
  connected_ = false;
}

#endif  // TRANSPORT_BACKEND_L2004
