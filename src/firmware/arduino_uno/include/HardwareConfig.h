#pragma once

#include <Arduino.h>

namespace HardwareConfig {
namespace Pins {
constexpr uint8_t StepPulse = 12;
constexpr uint8_t StepDirection = 11;
constexpr uint8_t StepEnable = 10;
constexpr uint8_t Hx711Data = 4;
constexpr uint8_t Hx711Clock = 5;
constexpr uint8_t ButtonUp = 2;
constexpr uint8_t ButtonDown = 3;
}

namespace Motion {
constexpr float MotorStepsPerRev = 200.0f;
constexpr float GearboxRatio = 19.203f;
constexpr float ScrewPitchMmPerRev = 4.0f;
constexpr uint16_t Microstepping = 4;
constexpr float JogStepRateStepsS = 500.0f;
constexpr float MaxAccelerationStepsS2 = 4000.0f;
constexpr uint32_t StepPulseHighMicros = 20;
constexpr bool InvertDirection = false;
constexpr bool InvertStepPulse = true;
constexpr bool UseEnablePin = false;
constexpr bool DisableMotorWhenIdle = false;
}

namespace LoadCell {
constexpr float CalibrationSlopeNPerCount = 0.001f;
constexpr float CalibrationInterceptN = 0.0f;
constexpr bool InvertSign = false;
}

namespace Timing {
constexpr uint16_t TelemetryPeriodMs = 100;
constexpr uint16_t ButtonDebounceMs = 25;
}
}
