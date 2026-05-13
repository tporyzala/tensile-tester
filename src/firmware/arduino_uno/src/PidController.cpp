#include "PidController.h"

#include <math.h>

void PidController::configure(float pGain, float iGain, float dGain, float deadbandN) {
  pGain_ = pGain;
  iGain_ = iGain;
  dGain_ = dGain;
  deadbandN_ = fabs(deadbandN);
  reset();
}

void PidController::reset() {
  integral_ = 0.0f;
  previousError_ = 0.0f;
  hasPreviousError_ = false;
}

float PidController::update(float errorN, float dtS, float maxOutputAbs) {
  if (dtS <= 0.0f) {
    return 0.0f;
  }

  if (fabs(errorN) <= deadbandN_) {
    errorN = 0.0f;
  }

  const float derivative = hasPreviousError_ ? (errorN - previousError_) / dtS : 0.0f;
  const float candidateIntegral = integral_ + errorN * dtS;
  const float unclamped =
      (pGain_ * errorN) +
      (iGain_ * candidateIntegral) +
      (dGain_ * derivative);
  const float clamped = constrain(unclamped, -maxOutputAbs, maxOutputAbs);

  const bool outputNotSaturated = fabs(unclamped - clamped) < 0.001f;
  const bool unwindingSaturation =
      (unclamped > clamped && errorN < 0.0f) ||
      (unclamped < clamped && errorN > 0.0f);
  if (outputNotSaturated || unwindingSaturation) {
    integral_ = candidateIntegral;
  }

  previousError_ = errorN;
  hasPreviousError_ = true;
  return clamped;
}

