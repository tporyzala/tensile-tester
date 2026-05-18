#pragma once

#include <Arduino.h>

namespace HardwareConfig {
namespace Pins {
// Arduino pins wired to the stepper driver, HX711 load cell board, and jog buttons.
constexpr uint8_t StepPulse = 12;
constexpr uint8_t StepDirection = 11;
constexpr uint8_t StepEnable = 10;
constexpr uint8_t Hx711Data = 5;
constexpr uint8_t Hx711Clock = 6;
constexpr uint8_t ButtonUp = 2;
constexpr uint8_t ButtonDown = 3;
}

namespace Motion {
// Mechanical conversion from motor rotation to crosshead travel.
constexpr float MotorStepsPerRev = 200.0f;
constexpr float GearboxRatio = 19.203f;
constexpr float ScrewPitchMmPerRev = 4.0f;
constexpr uint16_t Microstepping = 8;

// Jog limits are expressed in motor steps per second and steps per second squared.
constexpr float MinJogStepRateStepsS = 50.0f;
constexpr float MaxJogStepRateStepsS = 4000.0f;
constexpr float DefaultJogStepRateStepsS = 500.0f;
constexpr float MinAccelerationStepsS2 = 100.0f;
constexpr float MaxAccelerationStepsS2 = 10000.0f;
constexpr float DefaultAccelerationStepsS2 = 4000.0f;
constexpr uint32_t StepPulseHighMicros = 20;

// Driver polarity and enable behavior depend on the exact stepper driver wiring.
constexpr bool InvertDirection = false;
constexpr bool InvertStepPulse = true;
constexpr bool InvertEnable = false;
constexpr bool DisableMotorWhenIdle = false; // keep false to hold position when not jogging
}

namespace LoadCell {
// HX711 channel A gain. The bogde/HX711 library defaults to 128, but keep it explicit.
constexpr uint8_t Hx711Gain = 128;
// Force calibration maps raw HX711 counts to newtons: force = slope * counts + intercept.
constexpr float CalibrationSlopeNPerCount = 0.002283289f;
constexpr float CalibrationInterceptN = 0.0f;
constexpr bool InvertSign = false;
// A manual tare averages this many HX711-ready readings before applying the zero.
constexpr uint8_t TareSampleCount = 10;
constexpr uint16_t TareTimeoutMs = 3000;
}

namespace Timing {
// Telemetry is sent every 100 ms; button input must be stable for 25 ms.
constexpr uint16_t TelemetryPeriodMs = 100;
constexpr uint16_t ButtonDebounceMs = 25;
}
}
