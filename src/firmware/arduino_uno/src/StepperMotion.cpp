#include "StepperMotion.h"

#include <math.h>

namespace {
constexpr float kMotorStepsPerRev = 200.0f;
constexpr float kGearboxRatio = 19.203f;
constexpr float kScrewPitchMmPerRev = 4.0f;
constexpr uint32_t kPulseHighMicros = 4;
}

void StepperMotion::begin(uint8_t pulsePin, uint8_t directionPin, uint8_t enablePin) {
  pulsePin_ = pulsePin;
  directionPin_ = directionPin;
  enablePin_ = enablePin;
  pinMode(pulsePin_, OUTPUT);
  pinMode(directionPin_, OUTPUT);
  pinMode(enablePin_, OUTPUT);
  digitalWrite(pulsePin_, LOW);
  digitalWrite(directionPin_, LOW);
  setEnabled(false);
}

void StepperMotion::configure(float maxAccelerationStepsS2, uint16_t microstepping, bool invertDirection) {
  maxAccelerationStepsS2_ = max(maxAccelerationStepsS2, 1.0f);
  microstepping_ = microstepping < 1 ? 1 : microstepping;
  invertDirection_ = invertDirection;
}

void StepperMotion::setEnabled(bool enabled) {
  enabled_ = enabled;
  digitalWrite(enablePin_, enabled ? LOW : HIGH);
  if (!enabled) {
    stop();
  }
}

void StepperMotion::setTargetStepRate(float rateStepsS) {
  targetRateStepsS_ = rateStepsS;
}

void StepperMotion::stop() {
  targetRateStepsS_ = 0.0f;
  currentRateStepsS_ = 0.0f;
  pulseHigh_ = false;
  digitalWrite(pulsePin_, LOW);
}

void StepperMotion::update(uint32_t nowMicros) {
  updateVelocity(nowMicros);
  updatePulse(nowMicros);
}

float StepperMotion::currentStepRate() const {
  return currentRateStepsS_;
}

float StepperMotion::targetStepRate() const {
  return targetRateStepsS_;
}

float StepperMotion::estimatedCrossheadMm() const {
  return static_cast<float>(signedStepCount_) / stepsPerMm();
}

void StepperMotion::updateVelocity(uint32_t nowMicros) {
  if (lastVelocityUpdateMicros_ == 0) {
    lastVelocityUpdateMicros_ = nowMicros;
    return;
  }

  const uint32_t elapsedMicros = nowMicros - lastVelocityUpdateMicros_;
  if (elapsedMicros < 1000) {
    return;
  }
  lastVelocityUpdateMicros_ = nowMicros;

  const float dtS = static_cast<float>(elapsedMicros) / 1000000.0f;
  const float maxDelta = maxAccelerationStepsS2_ * dtS;
  const float rateError = targetRateStepsS_ - currentRateStepsS_;
  if (fabs(rateError) <= maxDelta) {
    currentRateStepsS_ = targetRateStepsS_;
  } else {
    currentRateStepsS_ += copysign(maxDelta, rateError);
  }
}

void StepperMotion::updatePulse(uint32_t nowMicros) {
  if (!enabled_ || fabs(currentRateStepsS_) < 0.5f) {
    digitalWrite(pulsePin_, LOW);
    pulseHigh_ = false;
    return;
  }

  if (pulseHigh_) {
    if (static_cast<int32_t>(nowMicros - pulseReleaseMicros_) >= 0) {
      endPulse(nowMicros);
    }
    return;
  }

  if (nextPulseMicros_ == 0 || static_cast<int32_t>(nowMicros - nextPulseMicros_) >= 0) {
    beginPulse(nowMicros);
  }
}

void StepperMotion::beginPulse(uint32_t nowMicros) {
  directionPositive_ = currentRateStepsS_ >= 0.0f;
  const bool electricalDirection = invertDirection_ ? !directionPositive_ : directionPositive_;
  digitalWrite(directionPin_, electricalDirection ? HIGH : LOW);
  digitalWrite(pulsePin_, HIGH);
  pulseHigh_ = true;
  pulseReleaseMicros_ = nowMicros + kPulseHighMicros;
}

void StepperMotion::endPulse(uint32_t nowMicros) {
  digitalWrite(pulsePin_, LOW);
  pulseHigh_ = false;
  signedStepCount_ += directionPositive_ ? 1 : -1;
  const float stepRateAbs = max(fabs(currentRateStepsS_), 0.5f);
  const uint32_t periodMicros = static_cast<uint32_t>(1000000.0f / stepRateAbs);
  nextPulseMicros_ = nowMicros + periodMicros;
}

float StepperMotion::stepsPerMm() const {
  return (kMotorStepsPerRev * kGearboxRatio * static_cast<float>(microstepping_)) / kScrewPitchMmPerRev;
}
