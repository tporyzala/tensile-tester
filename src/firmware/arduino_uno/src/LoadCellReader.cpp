#include "LoadCellReader.h"

void LoadCellReader::begin(uint8_t dataPin, uint8_t clockPin) {
  hx711_.begin(dataPin, clockPin);
}

void LoadCellReader::update() {
  if (hx711_.is_ready()) {
    rawAdc_ = hx711_.read();
    hasSample_ = true;
  }
}

long LoadCellReader::rawAdc() const {
  return rawAdc_;
}

bool LoadCellReader::hasSample() const {
  return hasSample_;
}

