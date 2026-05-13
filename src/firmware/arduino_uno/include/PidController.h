#pragma once

#include <Arduino.h>

class PidController {
 public:
  void configure(float pGain, float iGain, float dGain, float deadbandN);
  void reset();
  float update(float errorN, float dtS, float maxOutputAbs);

 private:
  float pGain_ = 0.8f;
  float iGain_ = 0.04f;
  float dGain_ = 0.01f;
  float deadbandN_ = 1.0f;
  float integral_ = 0.0f;
  float previousError_ = 0.0f;
  bool hasPreviousError_ = false;
};

