#pragma once

#include <Arduino.h>

class StepperMotion {
 public:
  void begin(uint8_t pulsePin, uint8_t directionPin, uint8_t enablePin);
  void configure(float maxAccelerationStepsS2, uint16_t microstepping, bool invertDirection);
  void setEnabled(bool enabled);
  void setTargetStepRate(float rateStepsS);
  void stop();
  void update(uint32_t nowMicros);

  float currentStepRate() const;
  float targetStepRate() const;
  float estimatedCrossheadMm() const;

 private:
  void updateVelocity(uint32_t nowMicros);
  void updatePulse(uint32_t nowMicros);
  void beginPulse(uint32_t nowMicros);
  void endPulse(uint32_t nowMicros);
  float stepsPerMm() const;

  uint8_t pulsePin_ = 9;
  uint8_t directionPin_ = 8;
  uint8_t enablePin_ = 7;
  bool enabled_ = false;
  bool invertDirection_ = false;
  bool pulseHigh_ = false;
  bool directionPositive_ = true;
  uint16_t microstepping_ = 4;
  float maxAccelerationStepsS2_ = 4000.0f;
  float targetRateStepsS_ = 0.0f;
  float currentRateStepsS_ = 0.0f;
  int32_t signedStepCount_ = 0;
  uint32_t lastVelocityUpdateMicros_ = 0;
  uint32_t nextPulseMicros_ = 0;
  uint32_t pulseReleaseMicros_ = 0;
};

