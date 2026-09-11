#include "transport.h"
#include "../logx.h"
#include <WiFiClientSecure.h>
#include <ArduinoHttpClient.h>   // WebSocketClient + TYPE_TEXT/TYPE_BINARY
#include <string.h>

// ArduinoHttpClient backend. NOTE: its WebSocketClient has a 128-byte TX buffer and
// sends whole (masked, un-fragmented) messages -- fine for the token + JSON here;
// audio streaming (Stage 5) will need -DWS_TX_BUFFER_SIZE=... or the Links2004 backend.

namespace {
  WiFiClientSecure* tls = nullptr;
  WebSocketClient*  ws  = nullptr;
}

bool transport::connect(const char* host, uint16_t port, const char* path) {
  close();                          // tear down any previous session
  tls = new WiFiClientSecure();
  tls->setInsecure();               // 4d: swap to setCACert()/setCACertBundle()
  tls->setHandshakeTimeout(15);     // seconds
  ws  = new WebSocketClient(*tls, host, port);
  int rc = ws->begin(path);         // TLS handshake + HTTP upgrade; 0 == HTTP 101
  if (rc != 0) {
    LOG_WARN("transport: ws begin(%s) failed rc=%d", path, rc);
    close();
    return false;
  }
  return true;
}

bool transport::sendText(const char* s) {
  if (!ws) return false;
  ws->beginMessage(TYPE_TEXT);
  ws->print(s);
  return ws->endMessage() == 0;
}

bool transport::sendBin(const uint8_t* data, size_t n) {
  if (!ws) return false;
  ws->beginMessage(TYPE_BINARY);
  ws->write(data, n);
  return ws->endMessage() == 0;
}

size_t transport::poll(char* buf, size_t cap) {
  if (!ws) return 0;
  int n = ws->parseMessage();       // auto-PONGs pings, auto-stops on CLOSE
  if (n <= 0) return 0;
  if (ws->messageType() != TYPE_TEXT) return 0;   // ignore binary/ping/pong here
  String s = ws->readString();
  size_t len = s.length();
  if (len >= cap) len = cap - 1;
  memcpy(buf, s.c_str(), len);
  buf[len] = 0;
  return len;
}

bool transport::connected() { return ws && ws->connected(); }

void transport::close() {
  if (ws)  { delete ws;  ws  = nullptr; }
  if (tls) { tls->stop(); delete tls; tls = nullptr; }
}
