#pragma once

#include <Arduino.h>

enum class ButtonId : uint8_t {
  Button1 = 0,
  Button2 = 1,
  Button3 = 2,
  Estop = 3,
};

class ButtonPanel {
 public:
  void begin(uint8_t button1Pin, uint8_t button2Pin, uint8_t button3Pin, uint8_t estopPin);
  void update(uint32_t nowMs);
  bool isDown(ButtonId id) const;
  bool pressed(ButtonId id);
  bool estopActive() const;

 private:
  static constexpr uint8_t kButtonCount = 4;
  static constexpr uint16_t kDebounceMs = 25;

  uint8_t pins_[kButtonCount] = {10, 11, 12, 6};
  bool stableDown_[kButtonCount] = {false, false, false, false};
  bool rawDown_[kButtonCount] = {false, false, false, false};
  bool pressLatched_[kButtonCount] = {false, false, false, false};
  uint32_t changedAtMs_[kButtonCount] = {0, 0, 0, 0};
};

