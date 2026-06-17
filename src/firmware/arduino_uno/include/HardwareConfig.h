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
constexpr uint8_t ButtonStop = 4;
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
constexpr float DefaultJogStepRateStepsS = 4000.0f;
constexpr float MinAccelerationStepsS2 = 100.0f;
constexpr float MaxAccelerationStepsS2 = 10000.0f;
constexpr float DefaultAccelerationStepsS2 = 10000.0f;
constexpr uint32_t StepPulseHighMicros = 20;

// Driver polarity and enable behavior depend on the exact stepper driver wiring.
constexpr bool InvertDirection = false;
constexpr bool InvertStepPulse = true;
constexpr bool InvertEnable = false;
constexpr bool DisableMotorWhenIdle = false; // keep false to hold position when not jogging
}

namespace Test {
// Positive force error means the controller needs to increase the measured load.
// Flip this to -1 if the test moves away from the requested load on hardware.
constexpr int8_t IncreaseLoadDirection = 1;
constexpr float MinStepRateStepsS = 50.0f;
constexpr float MaxStepRateStepsS = 4000.0f;
constexpr float DefaultMaxStepRateStepsS = 2000.0f;
constexpr float ForceKpStepsPerSecondPerNewton = 12.0f;
constexpr float ForceKiStepsPerSecondPerNewtonSecond = 1.0f;
constexpr float ForceKdStepsPerSecondPerNewtonPerSecond = 0.0f;
constexpr float ForceDeadbandN = 0.20f;
constexpr float ForceIntegralLimitNSeconds = 300.0f;
constexpr uint16_t HeartbeatTimeoutMs = 2000;
}

namespace LoadCell {
// HX711 channel A gain. The bogde/HX711 library defaults to 128, but keep it explicit.
constexpr uint8_t Hx711Gain = 128;
// Force calibration maps raw HX711 counts to newtons: force = slope * counts + intercept.
constexpr float CalibrationSlopeNPerCount = 0.002283289f;
constexpr float CalibrationInterceptN = 0.0f;
constexpr bool InvertSign = false;
// An operator tare averages every fresh HX711 reading collected in this time window.
constexpr uint32_t TareDurationMs = 5000;
}

namespace Timing {
// Telemetry is sent every 100 ms; button input must be stable for 25 ms.
constexpr uint16_t TelemetryPeriodMs = 100;
constexpr uint16_t ButtonDebounceMs = 25;
}
}
