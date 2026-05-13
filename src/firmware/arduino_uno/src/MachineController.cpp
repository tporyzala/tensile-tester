#include "MachineController.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>

void MachineController::begin() {
  Serial.begin(115200);
  motion_.begin(kPulsePin, kDirectionPin, kEnablePin);
  buttons_.begin(kButton1Pin, kButton2Pin, kButton3Pin, kEstopPin);
  loadCell_.begin(kHx711DataPin, kHx711ClockPin);
  pid_.configure(config_.pGain, config_.iGain, config_.dGain, config_.deadbandN);
  clearMethod();
  state_ = MachineState::WAITING_FOR_PI_CONFIG;
  emitStatus();
}

void MachineController::update() {
  const uint32_t nowMs = millis();
  const uint32_t nowMicros = micros();
  processSerial();
  loadCell_.update();
  buttons_.update(nowMs);
  updateMeasuredForce();
  handleButtons();

  if (buttons_.estopActive() && state_ != MachineState::ESTOPPED) {
    enterEstop();
  }

  if ((nowMs - lastControlMs_) >= kControlPeriodMs) {
    runControlLoop(nowMs);
    lastControlMs_ = nowMs;
  }

  motion_.update(nowMicros);

  if ((nowMs - lastTelemetryMs_) >= kTelemetryPeriodMs) {
    emitTelemetry(nowMs);
    lastTelemetryMs_ = nowMs;
  }
}

void MachineController::processSerial() {
  while (Serial.available() > 0) {
    const char next = static_cast<char>(Serial.read());
    if (next == '\r') {
      continue;
    }
    if (next == '\n') {
      serialBuffer_[serialBufferIndex_] = '\0';
      if (serialBufferIndex_ > 0) {
        handleCommand(serialBuffer_);
      }
      serialBufferIndex_ = 0;
      continue;
    }
    if (serialBufferIndex_ < (kSerialBufferSize - 1)) {
      serialBuffer_[serialBufferIndex_++] = next;
    } else {
      serialBufferIndex_ = 0;
      error(F("SERIAL_LINE_TOO_LONG"));
    }
  }
}

void MachineController::handleCommand(char* line) {
  char* savePtr = nullptr;
  char* command = strtok_r(line, ",", &savePtr);
  if (command == nullptr) {
    error(F("INVALID_COMMAND"));
    return;
  }

  if (!configuredCommandAllowed(command)) {
    error(F("CONFIG_REQUIRED"));
    return;
  }

  if (strcmp(command, "PING") == 0) {
    ack(F("PING"));
  } else if (strcmp(command, "GET_STATUS") == 0) {
    emitStatus();
  } else if (strcmp(command, "LOAD_CONFIG") == 0) {
    if (!parseLoadConfig(savePtr)) {
      error(F("INVALID_CONFIG"));
      return;
    }
    configured_ = true;
    if (state_ == MachineState::WAITING_FOR_PI_CONFIG) {
      state_ = MachineState::IDLE;
      motion_.setEnabled(true);
    }
    ack(F("LOAD_CONFIG"));
    emitStatus();
  } else if (strcmp(command, "LOAD_METHOD") == 0) {
    if (!parseLoadMethod(savePtr)) {
      error(F("INVALID_METHOD"));
    } else {
      ack(F("LOAD_METHOD"));
    }
  } else if (strcmp(command, "METHOD_STEP") == 0) {
    if (!parseMethodStep(savePtr)) {
      error(F("INVALID_METHOD_STEP"));
    } else {
      ack(F("METHOD_STEP"));
    }
  } else if (strcmp(command, "START") == 0) {
    if (state_ != MachineState::ARMED || loadedStepCount_ == 0) {
      error(F("INVALID_STATE"));
      return;
    }
    state_ = MachineState::RUNNING;
    activeStepIndex_ = 0;
    activeStepStartedMs_ = millis();
    activeTargetForceN_ = measuredForceN_;
    pid_.reset();
    ack(F("START"));
    event(F("TEST_STARTED"));
    eventWithIndex(F("STEP_STARTED"), 1);
  } else if (strcmp(command, "CANCEL_ARM") == 0) {
    if (state_ != MachineState::ARMED) {
      error(F("INVALID_STATE"));
      return;
    }
    cancelArmedRun();
    ack(F("CANCEL_ARM"));
  } else if (strcmp(command, "PAUSE") == 0) {
    if (state_ != MachineState::RUNNING && state_ != MachineState::RETURNING_TO_ZERO) {
      error(F("INVALID_STATE"));
      return;
    }
    pausedReturnState_ = state_;
    state_ = MachineState::PAUSED;
    motion_.setTargetStepRate(0.0f);
    ack(F("PAUSE"));
  } else if (strcmp(command, "RESUME") == 0) {
    if (state_ != MachineState::PAUSED) {
      error(F("INVALID_STATE"));
      return;
    }
    state_ = pausedReturnState_;
    ack(F("RESUME"));
  } else if (strcmp(command, "RETURN_ZERO") == 0) {
    if (state_ != MachineState::PAUSED) {
      error(F("INVALID_STATE"));
      return;
    }
    enterReturningToZero();
    ack(F("RETURN_ZERO"));
  } else if (strcmp(command, "ABORT") == 0) {
    if (
        state_ != MachineState::ARMED &&
        state_ != MachineState::RUNNING &&
        state_ != MachineState::RETURNING_TO_ZERO &&
        state_ != MachineState::PAUSED) {
      error(F("INVALID_STATE"));
      return;
    }
    enterAbort();
    ack(F("ABORT"));
  } else if (strcmp(command, "ENTER_SETUP") == 0) {
    if (state_ != MachineState::IDLE) {
      error(F("INVALID_STATE"));
      return;
    }
    state_ = MachineState::SETUP;
    motion_.setEnabled(false);
    ack(F("ENTER_SETUP"));
    emitStatus();
  } else if (strcmp(command, "EXIT_SETUP") == 0) {
    if (state_ != MachineState::SETUP) {
      error(F("INVALID_STATE"));
      return;
    }
    state_ = MachineState::IDLE;
    motion_.setEnabled(true);
    ack(F("EXIT_SETUP"));
    emitStatus();
  } else if (strcmp(command, "ZERO_LOAD") == 0) {
    if (state_ != MachineState::IDLE) {
      error(F("INVALID_STATE"));
      return;
    }
    applyZeroLoad();
    ack(F("ZERO_LOAD"));
  } else if (strcmp(command, "RESET_FAULT") == 0) {
    if (
        state_ != MachineState::ABORTED &&
        state_ != MachineState::ESTOPPED &&
        state_ != MachineState::FAULT) {
      error(F("INVALID_STATE"));
      return;
    }
    if (state_ == MachineState::ESTOPPED && buttons_.estopActive()) {
      error(F("ESTOP_STILL_ACTIVE"));
      return;
    }
    resetTerminalState();
    ack(F("RESET_FAULT"));
  } else {
    error(F("INVALID_COMMAND"));
  }
}

void MachineController::handleButtons() {
  if (state_ == MachineState::ESTOPPED) {
    if (buttons_.pressed(ButtonId::Button3) && !buttons_.estopActive()) {
      resetTerminalState();
    }
    return;
  }

  switch (state_) {
    case MachineState::IDLE:
      if (buttons_.pressed(ButtonId::Button3)) {
        applyZeroLoad();
      }
      break;
    case MachineState::SETUP:
      if (buttons_.pressed(ButtonId::Button3)) {
        state_ = MachineState::IDLE;
        motion_.setEnabled(true);
      }
      break;
    case MachineState::ARMED:
      if (buttons_.pressed(ButtonId::Button1)) {
        state_ = MachineState::RUNNING;
        activeStepIndex_ = 0;
        activeStepStartedMs_ = millis();
        activeTargetForceN_ = measuredForceN_;
        pid_.reset();
        event(F("TEST_STARTED"));
        eventWithIndex(F("STEP_STARTED"), 1);
      } else if (buttons_.pressed(ButtonId::Button2)) {
        cancelArmedRun();
      } else if (buttons_.pressed(ButtonId::Button3)) {
        enterAbort();
      }
      break;
    case MachineState::RUNNING:
    case MachineState::RETURNING_TO_ZERO:
      if (buttons_.pressed(ButtonId::Button2)) {
        pausedReturnState_ = state_;
        state_ = MachineState::PAUSED;
        motion_.setTargetStepRate(0.0f);
      } else if (buttons_.pressed(ButtonId::Button3)) {
        enterAbort();
      }
      break;
    case MachineState::PAUSED:
      if (buttons_.pressed(ButtonId::Button1)) {
        state_ = pausedReturnState_;
      } else if (buttons_.pressed(ButtonId::Button2)) {
        enterReturningToZero();
      } else if (buttons_.pressed(ButtonId::Button3)) {
        enterAbort();
      }
      break;
    case MachineState::ABORTED:
    case MachineState::FAULT:
      if (buttons_.pressed(ButtonId::Button3)) {
        resetTerminalState();
      }
      break;
    default:
      break;
  }
}

void MachineController::runControlLoop(uint32_t nowMs) {
  const float dtS = static_cast<float>(kControlPeriodMs) / 1000.0f;

  if (!configured_) {
    motion_.setEnabled(false);
    motion_.setTargetStepRate(0.0f);
    return;
  }

  if (fabs(measuredForceN_) > config_.overloadThresholdN &&
      state_ != MachineState::FAULT &&
      state_ != MachineState::ESTOPPED) {
    enterFault(F("OVERLOAD"));
  }

  if (state_ == MachineState::SETUP || state_ == MachineState::ESTOPPED) {
    motion_.setEnabled(false);
    motion_.setTargetStepRate(0.0f);
    return;
  }

  motion_.setEnabled(true);

  if (state_ == MachineState::IDLE) {
    int8_t jogDirection = 0;
    if (buttons_.isDown(ButtonId::Button1)) {
      jogDirection = 1;
    } else if (buttons_.isDown(ButtonId::Button2)) {
      jogDirection = -1;
    }
    motion_.setTargetStepRate(static_cast<float>(jogDirection) * config_.jogSpeedStepsS);
    return;
  }

  if (state_ == MachineState::PAUSED ||
      state_ == MachineState::ABORTED ||
      state_ == MachineState::FAULT ||
      state_ == MachineState::ARMED ||
      state_ == MachineState::WAITING_FOR_PI_CONFIG) {
    motion_.setTargetStepRate(0.0f);
    return;
  }

  if (state_ == MachineState::RUNNING) {
    updateRunningTarget(dtS, nowMs);
  } else if (state_ == MachineState::RETURNING_TO_ZERO) {
    updateReturnToZeroTarget(dtS);
  }

  const float errorN = activeTargetForceN_ - measuredForceN_;
  const float pidStepRate = pid_.update(errorN, dtS, config_.maxStepRateStepsS);
  motion_.setTargetStepRate(pidStepRate);
}

void MachineController::updateRunningTarget(float dtS, uint32_t nowMs) {
  if (activeStepIndex_ >= loadedStepCount_) {
    enterReturningToZero();
    return;
  }

  const TestStep& step = steps_[activeStepIndex_];
  const float elapsedS = static_cast<float>(nowMs - activeStepStartedMs_) / 1000.0f;
  if (step.type == TestStepType::RAMP_TO_LOAD) {
    const float difference = step.targetForceN - activeTargetForceN_;
    const float allowedChange = max(step.rateNPerS, 0.01f) * dtS;
    if (fabs(difference) <= allowedChange) {
      activeTargetForceN_ = step.targetForceN;
    } else {
      activeTargetForceN_ += copysign(allowedChange, difference);
    }
    const bool targetReached = fabs(activeTargetForceN_ - step.targetForceN) <= 0.05f;
    const bool measuredReached = fabs(measuredForceN_ - step.targetForceN) <= max(config_.deadbandN, 1.0f);
    const bool timedOut = step.timeoutS > 0.0f && elapsedS >= step.timeoutS;
    if ((targetReached && measuredReached) || timedOut) {
      completeCurrentStep(nowMs);
    }
  } else {
    activeTargetForceN_ = step.targetForceN;
    if (elapsedS >= step.durationS) {
      completeCurrentStep(nowMs);
    }
  }
}

void MachineController::updateReturnToZeroTarget(float dtS) {
  const float allowedChange = max(config_.returnToZeroRateNS, 0.01f) * dtS;
  if (fabs(activeTargetForceN_) <= allowedChange) {
    activeTargetForceN_ = 0.0f;
  } else {
    activeTargetForceN_ -= copysign(allowedChange, activeTargetForceN_);
  }

  const bool targetNearZero = fabs(activeTargetForceN_) <= 0.05f;
  const bool measuredNearZero = fabs(measuredForceN_) <= max(config_.deadbandN, 1.5f);
  if (targetNearZero && measuredNearZero) {
    transitionToIdleAfterCompletion();
  }
}

void MachineController::completeCurrentStep(uint32_t nowMs) {
  eventWithIndex(F("STEP_COMPLETED"), activeStepIndex_ + 1);
  ++activeStepIndex_;
  activeStepStartedMs_ = nowMs;
  if (activeStepIndex_ >= loadedStepCount_) {
    enterReturningToZero();
  } else {
    eventWithIndex(F("STEP_STARTED"), activeStepIndex_ + 1);
  }
}

void MachineController::enterReturningToZero() {
  state_ = MachineState::RETURNING_TO_ZERO;
  pid_.reset();
  event(F("RETURN_ZERO_STARTED"));
}

void MachineController::enterAbort() {
  state_ = MachineState::ABORTED;
  motion_.setTargetStepRate(0.0f);
  event(F("ABORTED"));
}

void MachineController::cancelArmedRun() {
  clearMethod();
  activeTargetForceN_ = 0.0f;
  pid_.reset();
  state_ = MachineState::IDLE;
  motion_.setTargetStepRate(0.0f);
  event(F("ARM_CANCELLED"));
  emitStatus();
}

void MachineController::enterFault(const __FlashStringHelper* code) {
  state_ = MachineState::FAULT;
  motion_.setTargetStepRate(0.0f);
  Serial.print(F("EVENT,FAULT,"));
  Serial.println(code);
}

void MachineController::enterEstop() {
  state_ = MachineState::ESTOPPED;
  motion_.setEnabled(false);
  motion_.setTargetStepRate(0.0f);
  event(F("ESTOPPED"));
}

void MachineController::resetTerminalState() {
  clearMethod();
  tareOffsetN_ = 0.0f;
  activeTargetForceN_ = 0.0f;
  pid_.reset();
  state_ = MachineState::IDLE;
  motion_.setEnabled(true);
  motion_.setTargetStepRate(0.0f);
  emitStatus();
}

void MachineController::transitionToIdleAfterCompletion() {
  event(F("TEST_COMPLETE"));
  clearMethod();
  tareOffsetN_ = 0.0f;
  activeTargetForceN_ = 0.0f;
  pid_.reset();
  state_ = MachineState::IDLE;
  motion_.setTargetStepRate(0.0f);
}

void MachineController::applyZeroLoad() {
  tareOffsetN_ = untaredForceN();
  measuredForceN_ = 0.0f;
  event(F("TARE_APPLIED"));
}

void MachineController::updateMeasuredForce() {
  measuredForceN_ = untaredForceN() - tareOffsetN_;
}

float MachineController::untaredForceN() const {
  const float rawForce = (config_.calibrationSlope * static_cast<float>(loadCell_.rawAdc())) + config_.calibrationIntercept;
  return config_.invertLoadCellSign ? -rawForce : rawForce;
}

void MachineController::emitTelemetry(uint32_t nowMs) {
  ++telemetrySeq_;
  Serial.print(F("TEL,"));
  Serial.print(telemetrySeq_);
  Serial.print(',');
  Serial.print(nowMs);
  Serial.print(',');
  Serial.print(machineStateName(state_));
  Serial.print(',');
  Serial.print(loadCell_.rawAdc());
  Serial.print(',');
  Serial.print(measuredForceN_, 4);
  Serial.print(',');
  Serial.print(activeTargetForceN_, 4);
  Serial.print(',');
  Serial.print(motion_.currentStepRate(), 4);
  Serial.print(',');
  Serial.println(motion_.estimatedCrossheadMm(), 5);
}

void MachineController::emitStatus() {
  Serial.print(F("STATUS,"));
  Serial.print(machineStateName(state_));
  Serial.print(',');
  Serial.print(configured_ ? 1 : 0);
  Serial.print(',');
  Serial.print(measuredForceN_, 4);
  Serial.print(',');
  Serial.print(activeTargetForceN_, 4);
  Serial.print(',');
  Serial.print(motion_.currentStepRate(), 4);
  Serial.print(',');
  Serial.println(motion_.estimatedCrossheadMm(), 5);
}

void MachineController::ack(const __FlashStringHelper* command) {
  Serial.print(F("ACK,"));
  Serial.println(command);
}

void MachineController::error(const __FlashStringHelper* code) {
  Serial.print(F("ERR,"));
  Serial.println(code);
}

void MachineController::event(const __FlashStringHelper* name) {
  Serial.print(F("EVENT,"));
  Serial.println(name);
}

void MachineController::eventWithIndex(const __FlashStringHelper* name, uint8_t index) {
  Serial.print(F("EVENT,"));
  Serial.print(name);
  Serial.print(',');
  Serial.println(index);
}

bool MachineController::configuredCommandAllowed(const char* command) const {
  if (configured_) {
    return true;
  }
  return strcmp(command, "PING") == 0 ||
         strcmp(command, "GET_STATUS") == 0 ||
         strcmp(command, "LOAD_CONFIG") == 0;
}

bool MachineController::parseLoadConfig(char* savePtr) {
  bool ok = true;
  config_.pGain = parseFloatToken(savePtr, ok);
  config_.iGain = parseFloatToken(savePtr, ok);
  config_.dGain = parseFloatToken(savePtr, ok);
  config_.deadbandN = parseFloatToken(savePtr, ok);
  config_.maxStepRateStepsS = parseFloatToken(savePtr, ok);
  config_.maxAccelerationStepsS2 = parseFloatToken(savePtr, ok);
  config_.jogSpeedStepsS = parseFloatToken(savePtr, ok);
  config_.returnToZeroRateNS = parseFloatToken(savePtr, ok);
  config_.overloadThresholdN = parseFloatToken(savePtr, ok);
  config_.microstepping = static_cast<uint16_t>(parseLongToken(savePtr, ok));
  config_.invertMotorDirection = parseLongToken(savePtr, ok) != 0;
  config_.invertLoadCellSign = parseLongToken(savePtr, ok) != 0;
  config_.calibrationSlope = parseFloatToken(savePtr, ok);
  config_.calibrationIntercept = parseFloatToken(savePtr, ok);
  if (!ok) {
    return false;
  }
  pid_.configure(config_.pGain, config_.iGain, config_.dGain, config_.deadbandN);
  motion_.configure(config_.maxAccelerationStepsS2, config_.microstepping, config_.invertMotorDirection);
  return true;
}

bool MachineController::parseLoadMethod(char* savePtr) {
  if (state_ != MachineState::IDLE) {
    return false;
  }
  bool ok = true;
  parseLongToken(savePtr, ok);
  const long expected = parseLongToken(savePtr, ok);
  if (!ok || expected <= 0 || expected > kMaxSteps) {
    return false;
  }
  clearMethod();
  expectedStepCount_ = static_cast<uint8_t>(expected);
  methodLoading_ = true;
  return true;
}

bool MachineController::parseMethodStep(char* savePtr) {
  if (state_ != MachineState::IDLE || !methodLoading_) {
    return false;
  }

  bool ok = true;
  const long position = parseLongToken(savePtr, ok);
  char* typeToken = strtok_r(nullptr, ",", &savePtr);
  const float targetForceN = parseFloatToken(savePtr, ok);
  const float scalarA = parseFloatToken(savePtr, ok);
  const float scalarB = parseFloatToken(savePtr, ok);
  const long totalCount = parseLongToken(savePtr, ok);
  if (!ok || typeToken == nullptr || position <= 0 || position > kMaxSteps || totalCount != expectedStepCount_) {
    return false;
  }

  TestStep& step = steps_[position - 1];
  if (strcmp(typeToken, "RAMP_TO_LOAD") == 0) {
    step.type = TestStepType::RAMP_TO_LOAD;
    step.targetForceN = targetForceN;
    step.rateNPerS = scalarA;
    step.timeoutS = scalarB;
    step.durationS = 0.0f;
  } else if (strcmp(typeToken, "HOLD_LOAD") == 0) {
    step.type = TestStepType::HOLD_LOAD;
    step.targetForceN = targetForceN;
    step.rateNPerS = 0.0f;
    step.timeoutS = 0.0f;
    step.durationS = scalarA;
  } else {
    return false;
  }

  ++loadedStepCount_;
  if (loadedStepCount_ >= expectedStepCount_) {
    methodLoading_ = false;
    state_ = MachineState::ARMED;
    event(F("METHOD_ARMED"));
    emitStatus();
  }
  return true;
}

float MachineController::parseFloatToken(char*& savePtr, bool& ok) {
  char* token = strtok_r(nullptr, ",", &savePtr);
  if (token == nullptr) {
    ok = false;
    return 0.0f;
  }
  return static_cast<float>(atof(token));
}

long MachineController::parseLongToken(char*& savePtr, bool& ok) {
  char* token = strtok_r(nullptr, ",", &savePtr);
  if (token == nullptr) {
    ok = false;
    return 0;
  }
  char* endPtr = nullptr;
  const long value = strtol(token, &endPtr, 10);
  if (endPtr == token) {
    ok = false;
    return 0;
  }
  return value;
}

void MachineController::clearMethod() {
  expectedStepCount_ = 0;
  loadedStepCount_ = 0;
  activeStepIndex_ = 0;
  activeStepStartedMs_ = 0;
  methodLoading_ = false;
}
