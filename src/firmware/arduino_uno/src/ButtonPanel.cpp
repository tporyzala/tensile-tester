#include "ButtonPanel.h"

void ButtonPanel::begin(uint8_t button1Pin, uint8_t button2Pin, uint8_t button3Pin, uint8_t estopPin) {
  pins_[0] = button1Pin;
  pins_[1] = button2Pin;
  pins_[2] = button3Pin;
  pins_[3] = estopPin;
  for (uint8_t index = 0; index < kButtonCount; ++index) {
    pinMode(pins_[index], INPUT_PULLUP);
    const bool down = digitalRead(pins_[index]) == LOW;
    stableDown_[index] = down;
    rawDown_[index] = down;
  }
}

void ButtonPanel::update(uint32_t nowMs) {
  for (uint8_t index = 0; index < kButtonCount; ++index) {
    const bool down = digitalRead(pins_[index]) == LOW;
    if (down != rawDown_[index]) {
      rawDown_[index] = down;
      changedAtMs_[index] = nowMs;
      continue;
    }
    if (down != stableDown_[index] && (nowMs - changedAtMs_[index]) >= kDebounceMs) {
      stableDown_[index] = down;
      if (down) {
        pressLatched_[index] = true;
      }
    }
  }
}

bool ButtonPanel::isDown(ButtonId id) const {
  return stableDown_[static_cast<uint8_t>(id)];
}

bool ButtonPanel::pressed(ButtonId id) {
  const uint8_t index = static_cast<uint8_t>(id);
  const bool latched = pressLatched_[index];
  pressLatched_[index] = false;
  return latched;
}

bool ButtonPanel::estopActive() const {
  return isDown(ButtonId::Estop);
}

