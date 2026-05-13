#pragma once

#include <Arduino.h>
#include <HX711.h>

class LoadCellReader {
 public:
  void begin(uint8_t dataPin, uint8_t clockPin);
  void update();
  long rawAdc() const;
  bool hasSample() const;

 private:
  HX711 hx711_;
  long rawAdc_ = 0;
  bool hasSample_ = false;
};

