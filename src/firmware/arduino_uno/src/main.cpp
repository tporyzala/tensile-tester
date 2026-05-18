#include <Arduino.h>
#include <AccelStepper.h>
#include <HX711.h>

#include "HardwareConfig.h"

#include <stdlib.h>
#include <string.h>

/*
  Plain-English firmware map:

  1. setup() runs once when the Arduino powers up or resets.
     It configures the stepper driver, buttons, load-cell board, and serial port.

  2. loop() repeats forever.
     Each pass checks for Pi commands, reads the load cell, reads the jog buttons,
     updates the motor speed, and occasionally sends telemetry back to the Pi.

  3. The Arduino owns the real-time hardware work.
     The Raspberry Pi tells it settings such as jog speed, test speed ceiling,
     and acceleration, but the Arduino decides every loop whether the motor
     should step right now.
*/

namespace {

// Keeps the rest of this file private to this firmware file.
// That matters less for Arduino sketches, but it makes the intent explicit:
// these helpers are internal implementation details, not a reusable library API.

struct DebouncedButton {
  /*
    Mechanical push buttons do not switch cleanly from OFF to ON.
    For a few milliseconds, the electrical signal can chatter between states.

    This small helper turns that noisy raw signal into a stable true/false value:
    - rawDown: what the pin reads right now.
    - stableDown: what we trust after the signal has stayed unchanged long enough.
    - changedAtMs: when the raw signal last changed.
  */
  uint8_t pin = 0;
  bool rawDown = false;
  bool stableDown = false;
  uint32_t changedAtMs = 0;

  void begin(uint8_t assignedPin) {
    // Button wiring uses INPUT_PULLUP:
    // - unpressed button reads HIGH
    // - pressed button connects the pin to ground and reads LOW
    pin = assignedPin;
    pinMode(pin, INPUT_PULLUP);
    rawDown = digitalRead(pin) == LOW;
    stableDown = rawDown;
    changedAtMs = millis();
  }

  void update(uint32_t nowMs) {
    /*
      Pseudo-code:
      - read the pin
      - if the raw value changed, restart the debounce timer
      - if the raw value stayed the same for ButtonDebounceMs, accept it
    */
    const bool down = digitalRead(pin) == LOW;
    if (down != rawDown) {
      rawDown = down;
      changedAtMs = nowMs;
      return;
    }
    if (down != stableDown && (nowMs - changedAtMs) >= HardwareConfig::Timing::ButtonDebounceMs) {
      stableDown = down;
    }
  }
};

// Hardware interfaces. These objects know how to talk to the external boards.
HX711 loadCell;
DebouncedButton upButton;
DebouncedButton downButton;
DebouncedButton stopButton;
AccelStepper stepper(
    AccelStepper::DRIVER,
    HardwareConfig::Pins::StepPulse,
    HardwareConfig::Pins::StepDirection);

/*
  Load-cell zeroing:
  - rawAdc is the raw HX711 number.
  - rawForceN() converts rawAdc into force using the calibration constants.
  - tareForceN stores the force reading that should be treated as zero.
  - operator tare commands average fresh HX711 readings for a timed window.
  - measuredForceN() reports rawForceN() minus tareForceN.
*/
long rawAdc = 0;
bool tareSet = false;
float tareForceN = 0.0f;
bool tareAveraging = false;
float tareForceSumN = 0.0f;
uint32_t tareSamplesCollected = 0;
uint32_t tareStartedAtMs = 0;

/*
  Jog motion state:
  - jogDirection is -1, 0, or +1 based on the two physical buttons.
  - targetStepRateStepsS is the speed we want.
  - commandedStepRateStepsS is the speed currently being sent to AccelStepper.
  - commandedStepRateStepsS ramps toward targetStepRateStepsS using accelerationStepsS2.

  That ramp is what prevents the motor command from jumping instantly from
  stopped to full speed.
*/
bool motorEnabled = false;
int8_t jogDirection = 0;
float jogSpeedStepsS = HardwareConfig::Motion::DefaultJogStepRateStepsS;
float testMaxStepRateStepsS = HardwareConfig::Test::DefaultMaxStepRateStepsS;
float accelerationStepsS2 = HardwareConfig::Motion::DefaultAccelerationStepsS2;
float targetStepRateStepsS = 0.0f;
float commandedStepRateStepsS = 0.0f;
uint32_t lastMotionUpdateMicros = 0;
long displacementZeroSteps = 0;

uint32_t telemetrySeq = 0;
uint32_t lastTelemetryMs = 0;

/*
  Lite operator-protection model:

  SETUP   -> setup jog, tare, and displacement zeroing are allowed.
  ARMED   -> a test run exists, but the Arduino is waiting for the Pi.
  TESTING -> the Arduino owns automated motion or pause.
  FAULT   -> motion is stopped; STOP_TEST returns to setup.

  Test phases are separate from frame modes. That keeps "what the machine may do"
  separate from "where the test program is".
*/
enum FrameMode : uint8_t {
  FRAME_SETUP,
  FRAME_ARMED,
  FRAME_TESTING,
  FRAME_FAULT
};

enum TestPhase : uint8_t {
  TEST_NONE,
  TEST_WAITING_STEP,
  TEST_RAMPING,
  TEST_HOLDING,
  TEST_PAUSED,
  TEST_FAULTED
};

enum TestValueType : uint8_t {
  TEST_VALUE_FORCE,
  TEST_VALUE_DISPLACEMENT
};

enum TestControlMode : uint8_t {
  TEST_CONTROL_NONE,
  TEST_CONTROL_FORCE,
  TEST_CONTROL_DISPLACEMENT
};

enum FaultReason : uint8_t {
  FAULT_NONE,
  FAULT_HEARTBEAT_TIMEOUT
};

// One record describes the active automated-test program and its current step.
struct ActiveTestStep {
  TestValueType targetType = TEST_VALUE_FORCE;
  float targetValue = 0.0f;
  TestValueType rateType = TEST_VALUE_FORCE;
  float rateValuePerS = 0.0f;
  uint32_t holdDurationMs = 0;
};

struct TestControllerState {
  FrameMode frameMode = FRAME_SETUP;
  TestPhase phase = TEST_NONE;
  TestPhase resumePhase = TEST_NONE;
  FaultReason faultReason = FAULT_NONE;
  uint16_t runId = 0;
  uint16_t stepIndex = 0;
  uint16_t stepCount = 0;
  ActiveTestStep step;
  TestControlMode controlMode = TEST_CONTROL_NONE;
  float stepStartForceN = 0.0f;
  float stepStartDisplacementMm = 0.0f;
  float setpointForceN = 0.0f;
  float setpointDisplacementMm = 0.0f;
  uint32_t stepStartedAtMs = 0;
  uint32_t holdStartedAtMs = 0;
  uint32_t pauseStartedAtMs = 0;
  uint32_t lastHeartbeatMs = 0;
};

/*
  Instron-lite state model:
  - frameMode says what the machine is allowed to do.
  - testPhase says where the current test program is.
  - faultReason says why the machine stopped abnormally.
  - The Pi stores the full step list.
  - The Arduino stores only the active step and keeps running it in real time.
  - One step has an end condition and an independent rate-control type.
  - A force endpoint is held with PID; a displacement endpoint is held in position.
*/
TestControllerState test;
bool lastPauseButtonDown = false;
bool lastStopButtonDown = false;
bool lastButton3StopDown = false;
float forcePidIntegralNSeconds = 0.0f;
float forcePidLastErrorN = 0.0f;
uint32_t forcePidLastUpdateMs = 0;
bool forcePidHasLastError = false;

// Serial commands arrive as bytes. This buffer collects them until a newline.
char serialBuffer[96] = {0};
uint8_t serialBufferIndex = 0;

float absoluteFloat(float value) {
  // Same idea as abs(), but explicit for float values.
  return value >= 0.0f ? value : -value;
}

float clampFloat(float value, float minValue, float maxValue) {
  // Keep a setting inside a safe range before applying it to hardware.
  if (value < minValue) {
    return minValue;
  }
  if (value > maxValue) {
    return maxValue;
  }
  return value;
}

float signFloat(float value) {
  if (value > 0.0f) {
    return 1.0f;
  }
  if (value < 0.0f) {
    return -1.0f;
  }
  return 0.0f;
}

float stepsPerMm() {
  /*
    Convert motor steps into crosshead travel.

    For one screw revolution:
    - the motor must turn through the gearbox
    - each motor revolution has MotorStepsPerRev full steps
    - microstepping multiplies the number of step pulses
    - the screw pitch converts screw rotation into millimeters
  */
  return (
      HardwareConfig::Motion::MotorStepsPerRev *
      HardwareConfig::Motion::GearboxRatio *
      static_cast<float>(HardwareConfig::Motion::Microstepping)) /
      HardwareConfig::Motion::ScrewPitchMmPerRev;
}

float positionMm() {
  // Keep motor tracking intact while reporting travel relative to the operator-selected zero.
  return (
      static_cast<float>(stepper.currentPosition()) -
      static_cast<float>(displacementZeroSteps)) /
      stepsPerMm();
}

float rawForceN() {
  /*
    Convert HX711 counts to force.

    This is a simple straight-line calibration:
      force = slope * rawAdc + intercept

    If the load cell sign is backwards for your wiring/mechanics, InvertSign
    flips tension/compression direction without changing the calibration numbers.
  */
  float force = (
      HardwareConfig::LoadCell::CalibrationSlopeNPerCount * static_cast<float>(rawAdc)) +
      HardwareConfig::LoadCell::CalibrationInterceptN;
  if (HardwareConfig::LoadCell::InvertSign) {
    force = -force;
  }
  return force;
}

float measuredForceN() {
  // Report force relative to the current tare/zero point.
  const float force = rawForceN();
  return tareSet ? (force - tareForceN) : force;
}

void beginTareAverage(uint32_t nowMs) {
  /*
    Start a non-blocking tare.

    The loop keeps servicing serial, buttons, and step pulses while the HX711
    produces fresh readings. Each ready reading contributes one sample for the
    full tare time window, and ACK is sent only after the averaged zero is applied.
  */
  tareAveraging = true;
  tareForceSumN = 0.0f;
  tareSamplesCollected = 0;
  tareStartedAtMs = nowMs;
}

void completeTareAverage() {
  if (tareSamplesCollected == 0) {
    tareAveraging = false;
    Serial.println(F("ERR,TARE_NO_SAMPLES"));
    return;
  }

  tareForceN = tareForceSumN / static_cast<float>(tareSamplesCollected);
  tareSet = true;
  tareAveraging = false;
  Serial.println(F("ACK,ZERO_LOAD"));
}

void addTareSample() {
  tareForceSumN += rawForceN();
  ++tareSamplesCollected;
}

const char* testPhaseName() {
  switch (test.phase) {
    case TEST_WAITING_STEP:
      return "WAITING_STEP";
    case TEST_RAMPING:
      return "RAMPING";
    case TEST_HOLDING:
      return "HOLDING";
    case TEST_PAUSED:
      return "PAUSED";
    case TEST_FAULTED:
      return "FAULTED";
    case TEST_NONE:
    default:
      return "NONE";
  }
}

const char* frameModeName() {
  switch (test.frameMode) {
    case FRAME_ARMED:
      return "ARMED";
    case FRAME_TESTING:
      return "TESTING";
    case FRAME_FAULT:
      return "FAULT";
    case FRAME_SETUP:
    default:
      return "SETUP";
  }
}

const char* faultReasonName() {
  switch (test.faultReason) {
    case FAULT_HEARTBEAT_TIMEOUT:
      return "HEARTBEAT_TIMEOUT";
    case FAULT_NONE:
    default:
      return "NONE";
  }
}

const char* testControlModeName() {
  switch (test.controlMode) {
    case TEST_CONTROL_FORCE:
      return "FORCE";
    case TEST_CONTROL_DISPLACEMENT:
      return "DISPLACEMENT";
    case TEST_CONTROL_NONE:
    default:
      return "NONE";
  }
}

bool parseTestValueType(const char* token, TestValueType& value) {
  if (strcmp(token, "FORCE") == 0) {
    value = TEST_VALUE_FORCE;
    return true;
  }
  if (strcmp(token, "DISPLACEMENT") == 0) {
    value = TEST_VALUE_DISPLACEMENT;
    return true;
  }
  return false;
}

void setMotorEnabled(bool enabled) {
  /*
    The enable pin is part of this machine's wiring.
    This helper keeps our software state and the driver output state together.
  */
  if (motorEnabled == enabled) {
    return;
  }
  motorEnabled = enabled;
  if (enabled) {
    stepper.enableOutputs();
  } else {
    stepper.disableOutputs();
  }
}

void resetForcePid() {
  forcePidIntegralNSeconds = 0.0f;
  forcePidLastErrorN = 0.0f;
  forcePidLastUpdateMs = 0;
  forcePidHasLastError = false;
}

bool testModeActive() {
  return test.phase != TEST_NONE;
}

bool testNeedsHeartbeat() {
  return (
      test.phase == TEST_WAITING_STEP ||
      test.phase == TEST_RAMPING ||
      test.phase == TEST_HOLDING ||
      test.phase == TEST_PAUSED ||
      test.frameMode == FRAME_TESTING);
}

void stopMotionNow() {
  jogDirection = 0;
  targetStepRateStepsS = 0.0f;
  commandedStepRateStepsS = 0.0f;
  stepper.setSpeed(0.0f);
  resetForcePid();
}

float updateForcePid(float commandedForceN, float measuredForceN, uint32_t nowMs) {
  /*
    The test state machine decides the commanded force.
    This PID turns force error into motor speed for both ramp and hold.

    D is wired in but intentionally configured to zero for now. HX711 readings
    can be noisy, and derivative gain can make the actuator twitch until the
    rest of the machine is characterized.
  */
  const float errorN = commandedForceN - measuredForceN;
  if (absoluteFloat(errorN) <= HardwareConfig::Test::ForceDeadbandN) {
    forcePidIntegralNSeconds = 0.0f;
    forcePidLastErrorN = errorN;
    forcePidLastUpdateMs = nowMs;
    forcePidHasLastError = true;
    return 0.0f;
  }

  float dtS = 0.0f;
  if (forcePidLastUpdateMs != 0) {
    dtS = static_cast<float>(nowMs - forcePidLastUpdateMs) / 1000.0f;
  }

  if (
      forcePidHasLastError &&
      signFloat(errorN) != 0.0f &&
      signFloat(forcePidLastErrorN) != 0.0f &&
      signFloat(errorN) != signFloat(forcePidLastErrorN)) {
    forcePidIntegralNSeconds = 0.0f;
  }

  if (dtS > 0.0f) {
    forcePidIntegralNSeconds += errorN * dtS;
    forcePidIntegralNSeconds = clampFloat(
        forcePidIntegralNSeconds,
        -HardwareConfig::Test::ForceIntegralLimitNSeconds,
        HardwareConfig::Test::ForceIntegralLimitNSeconds);
  }

  const float derivativeNPerS =
      (forcePidHasLastError && dtS > 0.0f)
          ? ((errorN - forcePidLastErrorN) / dtS)
          : 0.0f;
  forcePidLastErrorN = errorN;
  forcePidLastUpdateMs = nowMs;
  forcePidHasLastError = true;

  const float pidStepRate =
      (HardwareConfig::Test::ForceKpStepsPerSecondPerNewton * errorN) +
      (HardwareConfig::Test::ForceKiStepsPerSecondPerNewtonSecond * forcePidIntegralNSeconds) +
      (HardwareConfig::Test::ForceKdStepsPerSecondPerNewtonPerSecond * derivativeNPerS);

  const float requestedStepRate =
      pidStepRate * static_cast<float>(HardwareConfig::Test::IncreaseLoadDirection);
  return clampFloat(
      requestedStepRate,
      -testMaxStepRateStepsS,
      testMaxStepRateStepsS);
}

bool targetReached(float startValue, float currentValue, float targetValue) {
  const float direction = signFloat(targetValue - startValue);
  if (direction == 0.0f) {
    return true;
  }
  return direction > 0.0f
      ? currentValue >= targetValue
      : currentValue <= targetValue;
}

float forceRampDirection() {
  if (test.step.targetType == TEST_VALUE_FORCE) {
    return signFloat(test.step.targetValue - test.stepStartForceN);
  }
  return (
      signFloat(test.step.targetValue - test.stepStartDisplacementMm) *
      static_cast<float>(HardwareConfig::Test::IncreaseLoadDirection));
}

float displacementRampDirection() {
  if (test.step.targetType == TEST_VALUE_DISPLACEMENT) {
    return signFloat(test.step.targetValue - test.stepStartDisplacementMm);
  }
  return (
      signFloat(test.step.targetValue - test.stepStartForceN) *
      static_cast<float>(HardwareConfig::Test::IncreaseLoadDirection));
}

void enterTestHold(uint32_t nowMs) {
  test.holdStartedAtMs = nowMs;
  if (test.step.targetType == TEST_VALUE_FORCE) {
    test.controlMode = TEST_CONTROL_FORCE;
    test.setpointForceN = test.step.targetValue;
    resetForcePid();
  } else {
    test.controlMode = TEST_CONTROL_DISPLACEMENT;
    test.setpointDisplacementMm = test.step.targetValue;
    stopMotionNow();
  }
  test.frameMode = FRAME_TESTING;
  test.phase = TEST_HOLDING;
}

void emitTestEvent(const __FlashStringHelper* eventName) {
  Serial.print(F("EVT,"));
  Serial.print(eventName);
  Serial.print(',');
  Serial.println(test.runId);
}

void emitTestEventWithStep(const __FlashStringHelper* eventName) {
  Serial.print(F("EVT,"));
  Serial.print(eventName);
  Serial.print(',');
  Serial.print(test.runId);
  Serial.print(',');
  Serial.println(test.stepIndex);
}

void armTestProgram(uint16_t runId, uint16_t stepCount, uint32_t nowMs) {
  stopMotionNow();
  setMotorEnabled(true);
  test = TestControllerState{};
  test.runId = runId;
  test.stepCount = stepCount;
  test.setpointForceN = measuredForceN();
  test.setpointDisplacementMm = positionMm();
  test.step.targetValue = test.setpointForceN;
  test.lastHeartbeatMs = nowMs;
  test.frameMode = FRAME_ARMED;
  test.phase = TEST_WAITING_STEP;
}

bool startTestStep(
    uint16_t runId,
    uint16_t stepIndex,
    TestValueType targetType,
    float targetValue,
    TestValueType rateType,
    float rateValuePerS,
    uint32_t holdDurationMs,
    uint32_t nowMs) {
  if (runId != test.runId || test.frameMode != FRAME_ARMED || test.phase != TEST_WAITING_STEP) {
    return false;
  }
  if (stepIndex == 0 || stepIndex > test.stepCount || rateValuePerS <= 0.0f) {
    return false;
  }

  stopMotionNow();
  setMotorEnabled(true);
  test.stepIndex = stepIndex;
  test.step.targetType = targetType;
  test.step.targetValue = targetValue;
  test.step.rateType = rateType;
  test.step.rateValuePerS = rateValuePerS;
  test.step.holdDurationMs = holdDurationMs;
  test.controlMode = rateType == TEST_VALUE_FORCE
      ? TEST_CONTROL_FORCE
      : TEST_CONTROL_DISPLACEMENT;
  test.stepStartForceN = measuredForceN();
  test.stepStartDisplacementMm = positionMm();
  test.setpointForceN = test.stepStartForceN;
  test.setpointDisplacementMm = test.stepStartDisplacementMm;
  test.stepStartedAtMs = nowMs;
  test.holdStartedAtMs = 0;
  test.lastHeartbeatMs = nowMs;
  test.frameMode = FRAME_TESTING;
  test.phase = TEST_RAMPING;
  return true;
}

void pauseActiveTest(uint32_t nowMs) {
  if (test.phase != TEST_RAMPING && test.phase != TEST_HOLDING) {
    return;
  }
  test.resumePhase = test.phase;
  test.pauseStartedAtMs = nowMs;
  test.frameMode = FRAME_TESTING;
  test.phase = TEST_PAUSED;
  stopMotionNow();
  setMotorEnabled(true);
  emitTestEvent(F("TEST_PAUSED"));
}

void resumeActiveTest(uint32_t nowMs) {
  if (test.phase != TEST_PAUSED) {
    return;
  }
  const uint32_t pausedMs = nowMs - test.pauseStartedAtMs;
  test.stepStartedAtMs += pausedMs;
  if (test.resumePhase == TEST_HOLDING && test.holdStartedAtMs > 0) {
    test.holdStartedAtMs += pausedMs;
  }
  test.lastHeartbeatMs = nowMs;
  test.frameMode = FRAME_TESTING;
  test.phase = test.resumePhase;
  test.resumePhase = TEST_NONE;
  emitTestEvent(F("TEST_RESUMED"));
}

void stopActiveTest() {
  if (!testModeActive()) {
    return;
  }
  const uint16_t stoppedRunId = test.runId;
  stopMotionNow();
  test = TestControllerState{};
  setMotorEnabled(!HardwareConfig::Motion::DisableMotorWhenIdle);
  Serial.print(F("EVT,TEST_STOPPED,"));
  Serial.println(stoppedRunId);
}

void returnToSetupAfterSuccessfulTest() {
  test = TestControllerState{};
  setMotorEnabled(!HardwareConfig::Motion::DisableMotorWhenIdle);
}

void faultActiveTest(FaultReason reason) {
  if (!testModeActive()) {
    return;
  }
  stopMotionNow();
  test.controlMode = TEST_CONTROL_NONE;
  setMotorEnabled(true);
  test.faultReason = reason;
  test.frameMode = FRAME_FAULT;
  test.phase = TEST_FAULTED;
  Serial.print(F("EVT,TEST_FAULT,"));
  Serial.print(test.runId);
  Serial.print(',');
  Serial.println(faultReasonName());
}

bool handleTestButtons(uint32_t nowMs) {
  const bool pausePressed = upButton.stableDown && !lastPauseButtonDown;
  const bool button2StopPressed = downButton.stableDown && !lastStopButtonDown;
  const bool button3StopPressed = stopButton.stableDown && !lastButton3StopDown;
  const bool stopPressed = button2StopPressed || button3StopPressed;
  lastPauseButtonDown = upButton.stableDown;
  lastStopButtonDown = downButton.stableDown;
  lastButton3StopDown = stopButton.stableDown;

  if (!testModeActive()) {
    return false;
  }

  if (stopPressed) {
    stopActiveTest();
  } else if (test.frameMode != FRAME_FAULT && pausePressed) {
    if (test.phase == TEST_PAUSED) {
      resumeActiveTest(nowMs);
    } else {
      pauseActiveTest(nowMs);
    }
  }

  if (test.phase == TEST_NONE) {
    return true;
  }
  setMotorEnabled(true);
  return true;
}

void updateLoadCell(uint32_t nowMs) {
  /*
    Pseudo-code:
    - if the HX711 has a fresh reading, read it
    - if an operator tare is active, collect fresh readings for five seconds
    - on the first valid reading after startup, treat that as zero load

    This means the machine starts with a simple automatic tare. Later,
    ZERO_LOAD can be sent from the Pi to start an averaged tare.
  */
  if (
      tareAveraging &&
      (nowMs - tareStartedAtMs) >= HardwareConfig::LoadCell::TareDurationMs) {
    completeTareAverage();
    return;
  }

  if (!loadCell.is_ready()) {
    return;
  }

  rawAdc = loadCell.read();
  if (tareAveraging) {
    addTareSample();
    return;
  }

  if (!tareSet) {
    // First valid load-cell reading becomes zero load for this power-up.
    tareForceN = rawForceN();
    tareSet = true;
  }
}

void updateButtons(uint32_t nowMs) {
  /*
    Pseudo-code:
    - debounce all physical buttons
    - during a test: Button 1 pauses/resumes, Button 2 or Button 3 stops
    - outside a test: Button 1 jogs up and Button 2 jogs down
    - if neither or both are pressed, request stop
    - decide whether the motor driver should be held enabled
  */
  upButton.update(nowMs);
  downButton.update(nowMs);
  stopButton.update(nowMs);

  if (handleTestButtons(nowMs)) {
    return;
  }

  const bool up = upButton.stableDown;
  const bool down = downButton.stableDown;

  int8_t nextJogDirection = 0;
  // Press exactly one direction button to move; pressing both is treated as stop.
  if (up && !down) {
    nextJogDirection = 1;
  } else if (down && !up) {
    nextJogDirection = -1;
  }

  if (nextJogDirection != jogDirection) {
    jogDirection = nextJogDirection;
  }
  targetStepRateStepsS = static_cast<float>(jogDirection) * jogSpeedStepsS;

  const bool movingOrHolding =
      // Keep outputs active while a move is requested or while the ramp is still slowing down.
      jogDirection != 0 ||
      absoluteFloat(commandedStepRateStepsS) > 0.5f ||
      !HardwareConfig::Motion::DisableMotorWhenIdle;
  setMotorEnabled(movingOrHolding);
}

void completeCurrentStep() {
  stopMotionNow();
  test.controlMode = TEST_CONTROL_NONE;
  setMotorEnabled(true);
  emitTestEventWithStep(F("STEP_COMPLETE"));
  if (test.stepIndex >= test.stepCount) {
    emitTestEvent(F("TEST_COMPLETE"));
    returnToSetupAfterSuccessfulTest();
  } else {
    test.frameMode = FRAME_ARMED;
    test.phase = TEST_WAITING_STEP;
  }
}

void updateTestController(uint32_t nowMs) {
  if (testNeedsHeartbeat() && (nowMs - test.lastHeartbeatMs) > HardwareConfig::Test::HeartbeatTimeoutMs) {
    faultActiveTest(FAULT_HEARTBEAT_TIMEOUT);
    return;
  }

  /*
    State-machine responsibility:
    - RAMPING follows the selected force or displacement rate.
    - The target type determines when ramping is finished.
    - HOLDING keeps a force target under PID control, or holds a displacement
      target by stopping motion with the stepper still enabled.
  */
  if (test.phase == TEST_RAMPING) {
    const float elapsedS = static_cast<float>(nowMs - test.stepStartedAtMs) / 1000.0f;
    if (test.step.rateType == TEST_VALUE_FORCE) {
      test.controlMode = TEST_CONTROL_FORCE;
      test.setpointForceN =
          test.stepStartForceN + (forceRampDirection() * test.step.rateValuePerS * elapsedS);

      const bool reached = test.step.targetType == TEST_VALUE_FORCE
          ? targetReached(test.stepStartForceN, test.setpointForceN, test.step.targetValue)
          : targetReached(test.stepStartDisplacementMm, positionMm(), test.step.targetValue);
      if (reached) {
        enterTestHold(nowMs);
      }
    } else {
      test.controlMode = TEST_CONTROL_DISPLACEMENT;
      const float direction = displacementRampDirection();
      const float boundedRateMmPerS = clampFloat(
          test.step.rateValuePerS,
          0.0f,
          testMaxStepRateStepsS / stepsPerMm());
      test.setpointDisplacementMm =
          test.stepStartDisplacementMm + (direction * boundedRateMmPerS * elapsedS);
      targetStepRateStepsS = clampFloat(
          direction * boundedRateMmPerS * stepsPerMm(),
          -testMaxStepRateStepsS,
          testMaxStepRateStepsS);

      const bool reached = test.step.targetType == TEST_VALUE_FORCE
          ? targetReached(test.stepStartForceN, measuredForceN(), test.step.targetValue)
          : targetReached(test.stepStartDisplacementMm, positionMm(), test.step.targetValue);
      if (reached) {
        enterTestHold(nowMs);
      }
    }
  }

  if (test.phase == TEST_HOLDING) {
    if ((nowMs - test.holdStartedAtMs) >= test.step.holdDurationMs) {
      completeCurrentStep();
      return;
    }
  }

  if (test.phase == TEST_RAMPING || test.phase == TEST_HOLDING) {
    if (test.controlMode == TEST_CONTROL_FORCE) {
      targetStepRateStepsS = updateForcePid(test.setpointForceN, measuredForceN(), nowMs);
    } else if (test.phase == TEST_HOLDING) {
      targetStepRateStepsS = 0.0f;
    }
    setMotorEnabled(true);
  }
}

void updateStepper() {
  /*
    Pseudo-code:
    - calculate how much time has passed
    - move commanded speed toward target speed by at most acceleration * time
    - give the resulting speed to AccelStepper
    - call runSpeed() often so AccelStepper can generate step pulses

    The important practical point:
    runSpeed() must be called repeatedly and quickly. If loop() gets blocked,
    step pulses become uneven or stop.
  */
  const uint32_t nowMicros = micros();
  if (lastMotionUpdateMicros == 0) {
    lastMotionUpdateMicros = nowMicros;
  }

  const uint32_t elapsedMicros = nowMicros - lastMotionUpdateMicros;
  if (elapsedMicros >= 1000) {
    const float dtS = static_cast<float>(elapsedMicros) / 1000000.0f;
    const float maxDelta = accelerationStepsS2 * dtS;
    const float error = targetStepRateStepsS - commandedStepRateStepsS;
    // Ramp speed instead of jumping instantly, reducing shock on the frame and specimen.
    if (absoluteFloat(error) <= maxDelta) {
      commandedStepRateStepsS = targetStepRateStepsS;
    } else {
      commandedStepRateStepsS += error > 0.0f ? maxDelta : -maxDelta;
    }
    // Avoid tiny near-zero speeds that would look like noise rather than useful motion.
    if (absoluteFloat(commandedStepRateStepsS) < 0.5f && absoluteFloat(targetStepRateStepsS) < 0.5f) {
      commandedStepRateStepsS = 0.0f;
    }
    stepper.setSpeed(commandedStepRateStepsS);
    lastMotionUpdateMicros = nowMicros;
  }

  if (motorEnabled && absoluteFloat(commandedStepRateStepsS) > 0.5f) {
    // This is the line that actually lets AccelStepper emit step pulses.
    stepper.runSpeed();
  }
}

void applyMotionSettings(
    float jogSpeedStepsSValue,
    float testMaxStepRateStepsSValue,
    float accelerationStepsS2Value) {
  /*
    Apply settings sent from the Pi web UI.

    The Arduino clamps values again even though the browser also has limits.
    That keeps the firmware safe if a malformed command is sent over serial.
  */
  jogSpeedStepsS = clampFloat(
      jogSpeedStepsSValue,
      HardwareConfig::Motion::MinJogStepRateStepsS,
      HardwareConfig::Motion::MaxJogStepRateStepsS);
  testMaxStepRateStepsS = clampFloat(
      testMaxStepRateStepsSValue,
      HardwareConfig::Test::MinStepRateStepsS,
      HardwareConfig::Test::MaxStepRateStepsS);
  accelerationStepsS2 = clampFloat(
      accelerationStepsS2Value,
      HardwareConfig::Motion::MinAccelerationStepsS2,
      HardwareConfig::Motion::MaxAccelerationStepsS2);
  // Keep the driver's physical ceiling independent from the active speed settings.
  // Setup jog and automated test control each apply their own lower rate limit.
  const float driverMaxSpeed = HardwareConfig::Motion::MaxJogStepRateStepsS >
          HardwareConfig::Test::MaxStepRateStepsS
      ? HardwareConfig::Motion::MaxJogStepRateStepsS
      : HardwareConfig::Test::MaxStepRateStepsS;
  stepper.setMaxSpeed(driverMaxSpeed);
  stepper.setAcceleration(accelerationStepsS2);
  // If a button is already held, update the active target speed immediately.
  targetStepRateStepsS = static_cast<float>(jogDirection) * jogSpeedStepsS;
}

void emitMachinePayload() {
  /*
    Send the fields that describe the machine at this instant.

    Field order:
      frame mode,
      test phase,
      fault reason,
      raw HX711 count,
      measured force in newtons,
      current step rate,
      estimated crosshead position,
      up button pressed,
      down button pressed,
      stop button pressed,
      jog speed setting,
      acceleration setting,
      test maximum step rate,
      test run id,
      test step index,
      test step count,
      active test control mode,
      test setpoint force,
      test setpoint displacement,
      test elapsed time

    TEL and STATUS both use this same payload so the Pi parses one format.
  */
  Serial.print(frameModeName());
  Serial.print(',');
  Serial.print(testPhaseName());
  Serial.print(',');
  Serial.print(faultReasonName());
  Serial.print(',');
  Serial.print(rawAdc);
  Serial.print(',');
  Serial.print(measuredForceN(), 4);
  Serial.print(',');
  Serial.print(stepper.speed(), 2);
  Serial.print(',');
  Serial.print(positionMm(), 5);
  Serial.print(',');
  Serial.print(upButton.stableDown ? 1 : 0);
  Serial.print(',');
  Serial.print(downButton.stableDown ? 1 : 0);
  Serial.print(',');
  Serial.print(stopButton.stableDown ? 1 : 0);
  Serial.print(',');
  Serial.print(jogSpeedStepsS, 2);
  Serial.print(',');
  Serial.print(accelerationStepsS2, 2);
  Serial.print(',');
  Serial.print(testMaxStepRateStepsS, 2);
  Serial.print(',');
  Serial.print(test.runId);
  Serial.print(',');
  Serial.print(test.stepIndex);
  Serial.print(',');
  Serial.print(test.stepCount);
  Serial.print(',');
  Serial.print(testControlModeName());
  Serial.print(',');
  Serial.print(test.setpointForceN, 4);
  Serial.print(',');
  Serial.print(test.setpointDisplacementMm, 5);
  Serial.print(',');
  const uint32_t elapsedMs = test.stepStartedAtMs > 0 ? (millis() - test.stepStartedAtMs) : 0;
  Serial.println(elapsedMs);
}

void emitTelemetry(uint32_t nowMs) {
  /*
    Periodic report to the Pi.

    TEL includes:
      - a sequence number so you can see messages advancing
      - Arduino time in milliseconds
      - the shared machine payload
  */
  ++telemetrySeq;
  Serial.print(F("TEL,"));
  Serial.print(telemetrySeq);
  Serial.print(',');
  Serial.print(nowMs);
  Serial.print(',');
  emitMachinePayload();
}

void emitStatus() {
  // On-demand snapshot. The Pi asks for this after connecting with GET_STATUS.
  Serial.print(F("STATUS,"));
  emitMachinePayload();
}

void handleCommand(char* line) {
  /*
    Handle one full command line from the Pi.

    Current command vocabulary:
      PING
      GET_STATUS
      ZERO_LOAD
      ZERO_DISPLACEMENT
      SET_MOTION_LIMITS,<jog_speed_steps_s>,<test_max_step_rate_steps_s>,<acceleration_steps_s2>
      START_TEST,<run_id>,<step_count>
      TEST_STEP,<run_id>,<step_index>,<target_type>,<target_value>,<rate_type>,<rate_value_per_s>,<hold_duration_ms>
      TEST_HB,<run_id>
      PAUSE_TEST,<run_id>
      RESUME_TEST,<run_id>
      STOP_TEST,<run_id>

    The Pi treats SET_MOTION_LIMITS as successful only after this firmware replies
    with ACK,SET_MOTION_LIMITS and the values that were actually applied.
  */
  char* savePtr = nullptr;
  char* command = strtok_r(line, ",", &savePtr);
  if (command == nullptr) {
    return;
  }
  char receivedCommand[32] = {0};
  strncpy(receivedCommand, command, sizeof(receivedCommand) - 1);

  if (strcmp(command, "PING") == 0) {
    // Basic communication test.
    Serial.println(F("ACK,PING"));
  } else if (strcmp(command, "GET_STATUS") == 0) {
    // Pi wants a fresh one-line snapshot right now.
    emitStatus();
  } else if (strcmp(command, "ZERO_LOAD") == 0) {
    if (testModeActive()) {
      Serial.println(F("ERR,TEST_ACTIVE"));
      return;
    }
    // Operator wants the averaged current load reading to become zero.
    beginTareAverage(millis());
  } else if (strcmp(command, "ZERO_DISPLACEMENT") == 0) {
    if (testModeActive()) {
      Serial.println(F("ERR,TEST_ACTIVE"));
      return;
    }
    // Offset reported displacement without modifying AccelStepper's internal position state.
    displacementZeroSteps = stepper.currentPosition();
    Serial.println(F("ACK,ZERO_DISPLACEMENT"));
  } else if (strcmp(command, "SET_MOTION_LIMITS") == 0) {
    if (testModeActive()) {
      Serial.println(F("ERR,TEST_ACTIVE"));
      return;
    }
    // Read the three numbers after SET_MOTION_LIMITS.
    char* jogSpeedToken = strtok_r(nullptr, ",", &savePtr);
    char* testMaxSpeedToken = strtok_r(nullptr, ",", &savePtr);
    char* accelerationToken = strtok_r(nullptr, ",", &savePtr);
    if (jogSpeedToken == nullptr || testMaxSpeedToken == nullptr || accelerationToken == nullptr) {
      Serial.println(F("ERR,INVALID_SET_MOTION_LIMITS"));
      return;
    }
    applyMotionSettings(
        atof(jogSpeedToken),
        atof(testMaxSpeedToken),
        atof(accelerationToken));
    // Echo the applied values, not the raw requested values, because clamping may occur.
    Serial.print(F("ACK,SET_MOTION_LIMITS,"));
    Serial.print(jogSpeedStepsS, 2);
    Serial.print(',');
    Serial.print(testMaxStepRateStepsS, 2);
    Serial.print(',');
    Serial.println(accelerationStepsS2, 2);
  } else if (strcmp(command, "START_TEST") == 0) {
    char* runToken = strtok_r(nullptr, ",", &savePtr);
    char* countToken = strtok_r(nullptr, ",", &savePtr);
    if (runToken == nullptr || countToken == nullptr || testModeActive()) {
      Serial.println(F("ERR,INVALID_START_TEST"));
      return;
    }
    const uint16_t runId = static_cast<uint16_t>(atoi(runToken));
    const uint16_t stepCount = static_cast<uint16_t>(atoi(countToken));
    if (runId == 0 || stepCount == 0) {
      Serial.println(F("ERR,INVALID_START_TEST"));
      return;
    }
    armTestProgram(runId, stepCount, millis());
    Serial.print(F("ACK,START_TEST,"));
    Serial.println(test.runId);
  } else if (strcmp(command, "TEST_STEP") == 0) {
    char* runToken = strtok_r(nullptr, ",", &savePtr);
    char* indexToken = strtok_r(nullptr, ",", &savePtr);
    char* targetTypeToken = strtok_r(nullptr, ",", &savePtr);
    char* targetToken = strtok_r(nullptr, ",", &savePtr);
    char* rateTypeToken = strtok_r(nullptr, ",", &savePtr);
    char* rateToken = strtok_r(nullptr, ",", &savePtr);
    char* holdToken = strtok_r(nullptr, ",", &savePtr);
    if (
        runToken == nullptr ||
        indexToken == nullptr ||
        targetTypeToken == nullptr ||
        targetToken == nullptr ||
        rateTypeToken == nullptr ||
        rateToken == nullptr ||
        holdToken == nullptr) {
      Serial.println(F("ERR,INVALID_TEST_STEP"));
      return;
    }
    const uint16_t runId = static_cast<uint16_t>(atoi(runToken));
    const uint16_t stepIndex = static_cast<uint16_t>(atoi(indexToken));
    TestValueType targetType;
    TestValueType rateType;
    if (!parseTestValueType(targetTypeToken, targetType) || !parseTestValueType(rateTypeToken, rateType)) {
      Serial.println(F("ERR,INVALID_TEST_STEP"));
      return;
    }
    const float targetValue = atof(targetToken);
    const float rateValuePerS = atof(rateToken);
    const uint32_t holdDurationMs = static_cast<uint32_t>(atol(holdToken));
    if (!startTestStep(
            runId,
            stepIndex,
            targetType,
            targetValue,
            rateType,
            rateValuePerS,
            holdDurationMs,
            millis())) {
      Serial.println(F("ERR,INVALID_TEST_STEP"));
      return;
    }
    Serial.print(F("ACK,TEST_STEP,"));
    Serial.print(test.runId);
    Serial.print(',');
    Serial.println(test.stepIndex);
  } else if (strcmp(command, "TEST_HB") == 0) {
    char* runToken = strtok_r(nullptr, ",", &savePtr);
    const uint16_t runId = runToken == nullptr ? 0 : static_cast<uint16_t>(atoi(runToken));
    if (runId == test.runId && testNeedsHeartbeat()) {
      test.lastHeartbeatMs = millis();
    }
  } else if (strcmp(command, "PAUSE_TEST") == 0) {
    char* runToken = strtok_r(nullptr, ",", &savePtr);
    const uint16_t runId = runToken == nullptr ? 0 : static_cast<uint16_t>(atoi(runToken));
    if (runId != test.runId) {
      Serial.println(F("ERR,INVALID_PAUSE_TEST"));
      return;
    }
    pauseActiveTest(millis());
    Serial.print(F("ACK,PAUSE_TEST,"));
    Serial.println(runId);
  } else if (strcmp(command, "RESUME_TEST") == 0) {
    char* runToken = strtok_r(nullptr, ",", &savePtr);
    const uint16_t runId = runToken == nullptr ? 0 : static_cast<uint16_t>(atoi(runToken));
    if (runId != test.runId) {
      Serial.println(F("ERR,INVALID_RESUME_TEST"));
      return;
    }
    resumeActiveTest(millis());
    Serial.print(F("ACK,RESUME_TEST,"));
    Serial.println(runId);
  } else if (strcmp(command, "STOP_TEST") == 0) {
    char* runToken = strtok_r(nullptr, ",", &savePtr);
    const uint16_t runId = runToken == nullptr ? 0 : static_cast<uint16_t>(atoi(runToken));
    if (runId != test.runId) {
      Serial.println(F("ERR,INVALID_STOP_TEST"));
      return;
    }
    stopActiveTest();
    Serial.print(F("ACK,STOP_TEST,"));
    Serial.println(runId);
  } else {
    // Echoing the unknown token helps diagnose truncated or garbled serial commands.
    Serial.print(F("ERR,UNKNOWN_COMMAND,"));
    Serial.println(receivedCommand);
  }
}

void processSerial() {
  /*
    Serial bytes arrive one at a time.

    Pseudo-code:
    - ignore carriage returns
    - append normal characters into serialBuffer
    - when newline arrives, treat the buffer as one complete command
    - if the line is too long for the buffer, drop it and report an error

    This protects the command parser from trying to interpret half a command.
  */
  while (Serial.available() > 0) {
    const char next = static_cast<char>(Serial.read());
    if (next == '\r') {
      continue;
    }
    if (next == '\n') {
      serialBuffer[serialBufferIndex] = '\0';
      if (serialBufferIndex > 0) {
        handleCommand(serialBuffer);
      }
      serialBufferIndex = 0;
      continue;
    }
    if (serialBufferIndex < (sizeof(serialBuffer) - 1)) {
      serialBuffer[serialBufferIndex++] = next;
    } else {
      serialBufferIndex = 0;
      Serial.println(F("ERR,LINE_TOO_LONG"));
    }
  }
}

}  // namespace

void setup() {
  /*
    One-time startup.

    Nothing moves here. setup() only prepares hardware and reports BOOT.
  */
  Serial.begin(115200);

  // Tell AccelStepper which Arduino pin controls the driver's enable input.
  stepper.setEnablePin(HardwareConfig::Pins::StepEnable);
  // Match the step, direction, and enable signal polarity to the physical driver wiring.
  stepper.setPinsInverted(
      HardwareConfig::Motion::InvertDirection,
      HardwareConfig::Motion::InvertStepPulse,
      HardwareConfig::Motion::InvertEnable);
  // Keep the step pulse high long enough for the driver to reliably see it.
  stepper.setMinPulseWidth(HardwareConfig::Motion::StepPulseHighMicros);
  // Load default setup motion settings.
  applyMotionSettings(jogSpeedStepsS, testMaxStepRateStepsS, accelerationStepsS2);
  // Start disabled, then let setMotorEnabled() apply the configured idle behavior.
  stepper.disableOutputs();
  setMotorEnabled(!HardwareConfig::Motion::DisableMotorWhenIdle);

  // Start reading physical inputs and the load-cell amplifier.
  upButton.begin(HardwareConfig::Pins::ButtonUp);
  downButton.begin(HardwareConfig::Pins::ButtonDown);
  stopButton.begin(HardwareConfig::Pins::ButtonStop);
  loadCell.begin(
      HardwareConfig::Pins::Hx711Data,
      HardwareConfig::Pins::Hx711Clock,
      HardwareConfig::LoadCell::Hx711Gain);

  // Let the Pi know the firmware has booted and serial is alive.
  emitStatus();
}

void loop() {
  uint32_t nowMs = millis();

  /*
    Main control loop, repeated as fast as possible:

    1. processSerial()
       Check whether the Pi sent a command.

    2. updateLoadCell()
       Read force if the HX711 has a fresh value and advance any tare average.

    3. updateButtons()
       Convert physical button state into jog or test-control commands.

    4. updateTestController()
       During a test, apply the selected force or displacement rate control.

    5. updateStepper()
       Ramp toward the target speed and generate step pulses.

    6. emitTelemetry()
       Every TelemetryPeriodMs, report the current machine state to the Pi.
  */
  processSerial();
  // Commands handled above may use millis() internally. Refresh nowMs so the
  // test heartbeat check cannot compare an older loop timestamp to a newer
  // command timestamp and look like it timed out.
  nowMs = millis();
  updateLoadCell(nowMs);
  updateButtons(nowMs);
  updateTestController(nowMs);
  updateStepper();

  if ((nowMs - lastTelemetryMs) >= HardwareConfig::Timing::TelemetryPeriodMs) {
    emitTelemetry(nowMs);
    lastTelemetryMs = nowMs;
  }
}
