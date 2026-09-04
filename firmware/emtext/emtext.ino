#include <Arduino.h>
#include <M5Unified.h>

//importing from src
#include "src/logx.h"
#include "src/config/config.h"

void setup() {
  // put your setup code here, to run once:
  // setting up M5
  auto cfg = M5.config();
  cfg.serial_baudrate = 115200;
  cfg.clear_display = true;
  M5.begin(cfg);
  
  config::load();
  auto board = M5.getBoard();
  bool isStickS3 = (board == m5::board_t::board_M5StickS3);

  logx::setLevel(logx::INFO);
  LOG_INFO("boot: board id=%d isStickS3=%d", (int)board, isStickS3);


  M5.Display.setRotation(1);                 // landscape; orientation comes from IMU later
  M5.Display.fillScreen(TFT_BLACK);
  M5.Display.setTextColor(isStickS3 ? TFT_GREEN : TFT_RED, TFT_BLACK);
  M5.Display.setTextDatum(middle_center);    // datum for drawString centering
  M5.Display.setTextSize(2);
  M5.Display.drawString("emtext",
                        M5.Display.width()  / 2,
                        M5.Display.height() / 2);
}

void loop() {
  // put your main code here, to run repeatedly:
  M5.update();
  config::handleSerial();

  static uint32_t last = 0;
  uint32_t now = millis();
  if (now - last >= 1000) {
    last = now;
    LOG_DEBUG("hb up=%lus batt=%d%%", now/1000, M5.Power.getBatteryLevel());
  }

  M5.delay(1);
}
