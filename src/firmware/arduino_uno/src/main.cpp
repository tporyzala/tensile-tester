#include <Arduino.h>
#include <AccelStepper.h>
#include <HX711.h>

#include "HardwareConfig.h"

#include <string.h>

namespace {

struct DebouncedButton {
  uint8_t pin = 0;
  bool rawDown = false;
  bool stableDown = false;
  uint32_t changedAtMs = 0;

  void begin(uint8_t assignedPin) {
    pin = assignedPin;
    pinMode(pin, INPUT_PULLUP);
    rawDown = digitalRead(pin) == LOW;
    stableDown = rawDown;
    changedAtMs = millis();
  }

  void update(uint32_t nowMs) {
    const bool down = digitalRead(pin) == LOW;
    if (down != rawDown) {
      rawDown = down;
      changedAtMs = nowMs;
      return;
    }
    if (down != stableDown && (nowMs - changedAtMs) >= HardwareConfig::Timing::ButtonDebounceMs) {
      stableDown = down;
    }
  }
};

HX711 loadCell;
DebouncedButton upButton;
DebouncedButton downButton;
AccelStepper stepper(
    AccelStepper::DRIVER,
    HardwareConfig::Pins::StepPulse,
    HardwareConfig::Pins::StepDirection);

long rawAdc = 0;
bool tareSet = false;
float tareForceN = 0.0f;

bool motorEnabled = false;
int8_t jogDirection = 0;

uint32_t telemetrySeq = 0;
uint32_t lastTelemetryMs = 0;
char serialBuffer[48] = {0};
uint8_t serialBufferIndex = 0;

float absoluteFloat(float value) {
  return value >= 0.0f ? value : -value;
}

float stepsPerMm() {
  return (
      HardwareConfig::Motion::MotorStepsPerRev *
      HardwareConfig::Motion::GearboxRatio *
      static_cast<float>(HardwareConfig::Motion::Microstepping)) /
      HardwareConfig::Motion::ScrewPitchMmPerRev;
}

float positionMm() {
  return static_cast<float>(stepper.currentPosition()) / stepsPerMm();
}

float rawForceN() {
  float force = (
      HardwareConfig::LoadCell::CalibrationSlopeNPerCount * static_cast<float>(rawAdc)) +
      HardwareConfig::LoadCell::CalibrationInterceptN;
  if (HardwareConfig::LoadCell::InvertSign) {
    force = -force;
  }
  return force;
}

float measuredForceN() {
  const float force = rawForceN();
  return tareSet ? (force - tareForceN) : force;
}

const char* motionStateName() {
  const float speed = stepper.speed();
  if (speed > 0.5f || jogDirection > 0) {
    return "UP";
  }
  if (speed < -0.5f || jogDirection < 0) {
    return "DOWN";
  }
  return "IDLE";
}

void setMotorEnabled(bool enabled) {
  if (motorEnabled == enabled) {
    return;
  }
  motorEnabled = enabled;
  if (!HardwareConfig::Motion::UseEnablePin) {
    return;
  }
  if (enabled) {
    stepper.enableOutputs();
  } else {
    stepper.disableOutputs();
  }
}

void updateLoadCell() {
  if (!loadCell.is_ready()) {
    return;
  }

  rawAdc = loadCell.read();
  if (!tareSet) {
    tareForceN = rawForceN();
    tareSet = true;
  }
}

void updateButtons(uint32_t nowMs) {
  upButton.update(nowMs);
  downButton.update(nowMs);

  const bool up = upButton.stableDown;
  const bool down = downButton.stableDown;

  int8_t nextJogDirection = 0;
  if (up && !down) {
    nextJogDirection = 1;
  } else if (down && !up) {
    nextJogDirection = -1;
  }

  if (nextJogDirection != jogDirection) {
    jogDirection = nextJogDirection;
    stepper.setSpeed(static_cast<float>(jogDirection) * HardwareConfig::Motion::JogStepRateStepsS);
  }

  const bool movingOrHolding =
      jogDirection != 0 ||
      absoluteFloat(stepper.speed()) > 0.5f ||
      !HardwareConfig::Motion::DisableMotorWhenIdle;
  setMotorEnabled(movingOrHolding);
}

void updateStepper() {
  if (motorEnabled && absoluteFloat(stepper.speed()) > 0.5f) {
    stepper.runSpeed();
  }
}

void tareNow() {
  tareForceN = rawForceN();
  tareSet = true;
}

void emitTelemetry(uint32_t nowMs) {
  ++telemetrySeq;
  Serial.print(F("TEL,"));
  Serial.print(telemetrySeq);
  Serial.print(',');
  Serial.print(nowMs);
  Serial.print(',');
  Serial.print(motionStateName());
  Serial.print(',');
  Serial.print(rawAdc);
  Serial.print(',');
  Serial.print(measuredForceN(), 4);
  Serial.print(',');
  Serial.print(stepper.speed(), 2);
  Serial.print(',');
  Serial.print(positionMm(), 5);
  Serial.print(',');
  Serial.print(upButton.stableDown ? 1 : 0);
  Serial.print(',');
  Serial.println(downButton.stableDown ? 1 : 0);
}

void emitStatus() {
  Serial.print(F("STATUS,"));
  Serial.print(motionStateName());
  Serial.print(',');
  Serial.print(rawAdc);
  Serial.print(',');
  Serial.print(measuredForceN(), 4);
  Serial.print(',');
  Serial.print(stepper.speed(), 2);
  Serial.print(',');
  Serial.print(positionMm(), 5);
  Serial.print(',');
  Serial.print(upButton.stableDown ? 1 : 0);
  Serial.print(',');
  Serial.println(downButton.stableDown ? 1 : 0);
}

void handleCommand(char* command) {
  if (strcmp(command, "PING") == 0) {
    Serial.println(F("ACK,PING"));
  } else if (strcmp(command, "GET_STATUS") == 0) {
    emitStatus();
  } else if (strcmp(command, "ZERO_LOAD") == 0) {
    tareNow();
    Serial.println(F("ACK,ZERO_LOAD"));
  } else {
    Serial.println(F("ERR,UNKNOWN_COMMAND"));
  }
}

void processSerial() {
  while (Serial.available() > 0) {
    const char next = static_cast<char>(Serial.read());
    if (next == '\r') {
      continue;
    }
    if (next == '\n') {
      serialBuffer[serialBufferIndex] = '\0';
      if (serialBufferIndex > 0) {
        handleCommand(serialBuffer);
      }
      serialBufferIndex = 0;
      continue;
    }
    if (serialBufferIndex < (sizeof(serialBuffer) - 1)) {
      serialBuffer[serialBufferIndex++] = next;
    } else {
      serialBufferIndex = 0;
      Serial.println(F("ERR,LINE_TOO_LONG"));
    }
  }
}

}  // namespace

void setup() {
  Serial.begin(115200);

  if (HardwareConfig::Motion::UseEnablePin) {
    stepper.setEnablePin(HardwareConfig::Pins::StepEnable);
  }
  stepper.setPinsInverted(
      HardwareConfig::Motion::InvertDirection,
      HardwareConfig::Motion::InvertStepPulse,
      false);
  stepper.setMinPulseWidth(HardwareConfig::Motion::StepPulseHighMicros);
  stepper.setMaxSpeed(HardwareConfig::Motion::JogStepRateStepsS);
  stepper.setAcceleration(HardwareConfig::Motion::MaxAccelerationStepsS2);
  if (HardwareConfig::Motion::UseEnablePin) {
    stepper.disableOutputs();
  } else {
    stepper.enableOutputs();
  }
  setMotorEnabled(!HardwareConfig::Motion::DisableMotorWhenIdle);

  upButton.begin(HardwareConfig::Pins::ButtonUp);
  downButton.begin(HardwareConfig::Pins::ButtonDown);
  loadCell.begin(HardwareConfig::Pins::Hx711Data, HardwareConfig::Pins::Hx711Clock);

  Serial.println(F("STATUS,BOOT,0,0.0000,0.00,0.00000,0,0"));
}

void loop() {
  const uint32_t nowMs = millis();

  processSerial();
  updateLoadCell();
  updateButtons(nowMs);
  updateStepper();

  if ((nowMs - lastTelemetryMs) >= HardwareConfig::Timing::TelemetryPeriodMs) {
    emitTelemetry(nowMs);
    lastTelemetryMs = nowMs;
  }
}
