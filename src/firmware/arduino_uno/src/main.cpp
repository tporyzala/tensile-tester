#include <Arduino.h>

#include "MachineController.h"

MachineController controller;

void setup() {
  controller.begin();
}

void loop() {
  controller.update();
}

