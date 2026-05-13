#pragma once

#include <Arduino.h>

#include "ButtonPanel.h"
#include "LoadCellReader.h"
#include "MachineTypes.h"
#include "PidController.h"
#include "StepperMotion.h"

class MachineController {
 public:
  void begin();
  void update();

 private:
  static constexpr uint8_t kPulsePin = 9;
  static constexpr uint8_t kDirectionPin = 8;
  static constexpr uint8_t kEnablePin = 7;
  static constexpr uint8_t kHx711DataPin = 4;
  static constexpr uint8_t kHx711ClockPin = 5;
  static constexpr uint8_t kButton1Pin = 10;
  static constexpr uint8_t kButton2Pin = 11;
  static constexpr uint8_t kButton3Pin = 12;
  static constexpr uint8_t kEstopPin = 6;
  static constexpr uint8_t kMaxSteps = 12;
  static constexpr uint16_t kControlPeriodMs = 10;
  static constexpr uint16_t kTelemetryPeriodMs = 50;
  static constexpr uint8_t kSerialBufferSize = 180;

  void processSerial();
  void handleCommand(char* line);
  void handleButtons();
  void runControlLoop(uint32_t nowMs);
  void updateRunningTarget(float dtS, uint32_t nowMs);
  void updateReturnToZeroTarget(float dtS);
  void completeCurrentStep(uint32_t nowMs);
  void enterReturningToZero();
  void enterAbort();
  void cancelArmedRun();
  void enterFault(const __FlashStringHelper* code);
  void enterEstop();
  void resetTerminalState();
  void transitionToIdleAfterCompletion();
  void applyZeroLoad();
  void updateMeasuredForce();
  float untaredForceN() const;
  void emitTelemetry(uint32_t nowMs);
  void emitStatus();
  void ack(const __FlashStringHelper* command);
  void error(const __FlashStringHelper* code);
  void event(const __FlashStringHelper* name);
  void eventWithIndex(const __FlashStringHelper* name, uint8_t index);
  bool configuredCommandAllowed(const char* command) const;
  bool parseLoadConfig(char* savePtr);
  bool parseLoadMethod(char* savePtr);
  bool parseMethodStep(char* savePtr);
  float parseFloatToken(char*& savePtr, bool& ok);
  long parseLongToken(char*& savePtr, bool& ok);
  void clearMethod();

  MachineConfig config_;
  TestStep steps_[kMaxSteps];
  MachineState state_ = MachineState::BOOT;
  MachineState pausedReturnState_ = MachineState::RUNNING;
  PidController pid_;
  StepperMotion motion_;
  ButtonPanel buttons_;
  LoadCellReader loadCell_;
  bool configured_ = false;
  bool methodLoading_ = false;
  uint8_t expectedStepCount_ = 0;
  uint8_t loadedStepCount_ = 0;
  uint8_t activeStepIndex_ = 0;
  uint32_t activeStepStartedMs_ = 0;
  float activeTargetForceN_ = 0.0f;
  float measuredForceN_ = 0.0f;
  float tareOffsetN_ = 0.0f;
  uint32_t lastControlMs_ = 0;
  uint32_t lastTelemetryMs_ = 0;
  uint32_t telemetrySeq_ = 0;
  char serialBuffer_[kSerialBufferSize] = {0};
  uint8_t serialBufferIndex_ = 0;
};
