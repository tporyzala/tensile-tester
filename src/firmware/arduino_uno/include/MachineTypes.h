#pragma once

#include <Arduino.h>

enum class MachineState : uint8_t {
  BOOT,
  WAITING_FOR_PI_CONFIG,
  IDLE,
  SETUP,
  ARMED,
  RUNNING,
  RETURNING_TO_ZERO,
  PAUSED,
  ABORTED,
  ESTOPPED,
  FAULT,
};

enum class TestStepType : uint8_t {
  RAMP_TO_LOAD,
  HOLD_LOAD,
};

struct MachineConfig {
  float pGain = 0.8f;
  float iGain = 0.04f;
  float dGain = 0.01f;
  float deadbandN = 1.0f;
  float maxStepRateStepsS = 2200.0f;
  float maxAccelerationStepsS2 = 4000.0f;
  float jogSpeedStepsS = 500.0f;
  float returnToZeroRateNS = 50.0f;
  float overloadThresholdN = 1000.0f;
  uint16_t microstepping = 4;
  bool invertMotorDirection = false;
  bool invertLoadCellSign = false;
  float calibrationSlope = 0.001f;
  float calibrationIntercept = 0.0f;
};

struct TestStep {
  TestStepType type = TestStepType::RAMP_TO_LOAD;
  float targetForceN = 0.0f;
  float rateNPerS = 0.0f;
  float timeoutS = 0.0f;
  float durationS = 0.0f;
};

inline const __FlashStringHelper* machineStateName(MachineState state) {
  switch (state) {
    case MachineState::BOOT: return F("BOOT");
    case MachineState::WAITING_FOR_PI_CONFIG: return F("WAITING_FOR_PI_CONFIG");
    case MachineState::IDLE: return F("IDLE");
    case MachineState::SETUP: return F("SETUP");
    case MachineState::ARMED: return F("ARMED");
    case MachineState::RUNNING: return F("RUNNING");
    case MachineState::RETURNING_TO_ZERO: return F("RETURNING_TO_ZERO");
    case MachineState::PAUSED: return F("PAUSED");
    case MachineState::ABORTED: return F("ABORTED");
    case MachineState::ESTOPPED: return F("ESTOPPED");
    case MachineState::FAULT: return F("FAULT");
  }
  return F("FAULT");
}

