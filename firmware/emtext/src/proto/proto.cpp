#include "proto.h"
#include <ArduinoJson.h>   // v7
#include <string.h>

namespace {
  void copyStr(char* dst, size_t cap, const char* src) {
    if (!src) { dst[0] = 0; return; }
    strncpy(dst, src, cap - 1);
    dst[cap - 1] = 0;
  }
  proto::Tone toneFromStr(const char* s) {
    if (!s) return proto::Tone::Neutral;
    if (!strcmp(s, "positive"))  return proto::Tone::Positive;
    if (!strcmp(s, "negative"))  return proto::Tone::Negative;
    if (!strcmp(s, "sarcastic")) return proto::Tone::Sarcastic;
    if (!strcmp(s, "mixed"))     return proto::Tone::Mixed;
    return proto::Tone::Neutral;
  }
}

const char* proto::toneName(Tone t) {
  switch (t) {
    case Tone::Positive:  return "positive";
    case Tone::Negative:  return "negative";
    case Tone::Sarcastic: return "sarcastic";
    case Tone::Mixed:     return "mixed";
    default:              return "neutral";
  }
}

proto::Type proto::parse(const char* json, Frame& out) {
  out = Frame{};
  JsonDocument doc;
  if (deserializeJson(doc, json)) { out.type = Type::Unknown; return out.type; }

  const char* type = doc["type"] | "";
  if (!strcmp(type, "ready")) {
    out.type = Type::Ready;
  } else if (!strcmp(type, "status")) {
    out.type = Type::Status;
    copyStr(out.status, sizeof(out.status), doc["state"] | "");
  } else if (!strcmp(type, "utterance")) {
    out.type = Type::Utterance;
    out.id = doc["id"] | 0;
    copyStr(out.transcript, sizeof(out.transcript), doc["transcript"] | "");
  } else if (!strcmp(type, "read")) {
    out.type = Type::Read;
    out.id = doc["id"] | 0;
    out.tone = toneFromStr(doc["tone"] | "neutral");
    copyStr(out.read, sizeof(out.read), doc["read"] | "");
    if (doc["voice"].is<JsonObject>()) {              // optional, omitted when SER is off
      copyStr(out.emotion, sizeof(out.emotion), doc["voice"]["emotion"] | "");
      out.valence = doc["voice"]["valence"] | -1.0f;  // null -> -1 (absent)
      out.arousal = doc["voice"]["arousal"] | -1.0f;
    }
  } else if (!strcmp(type, "ping")) {
    out.type = Type::Ping;
    out.t = doc["t"] | 0.0;
  } else if (!strcmp(type, "pong")) {
    out.type = Type::Pong;
    out.t = doc["t"] | 0.0;
  } else if (!strcmp(type, "saved")) {              // clip-save reply
    out.type = Type::Saved;
    out.id = doc["id"] | 0;
    out.ok = doc["ok"] | false;
  } else {
    out.type = Type::Unknown;   // unknown types are ignored, never an error
  }
  return out.type;
}
