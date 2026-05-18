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
     The Raspberry Pi tells it settings such as jog speed and acceleration, but
     the Arduino decides every loop whether the motor should step right now.
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
AccelStepper stepper(
    AccelStepper::DRIVER,
    HardwareConfig::Pins::StepPulse,
    HardwareConfig::Pins::StepDirection);

/*
  Load-cell zeroing:
  - rawAdc is the raw HX711 number.
  - rawForceN() converts rawAdc into force using the calibration constants.
  - tareForceN stores the force reading that should be treated as zero.
  - measuredForceN() reports rawForceN() minus tareForceN.
*/
long rawAdc = 0;
bool tareSet = false;
float tareForceN = 0.0f;

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
float accelerationStepsS2 = HardwareConfig::Motion::DefaultAccelerationStepsS2;
float targetStepRateStepsS = 0.0f;
float commandedStepRateStepsS = 0.0f;
uint32_t lastMotionUpdateMicros = 0;

uint32_t telemetrySeq = 0;
uint32_t lastTelemetryMs = 0;

// Serial commands arrive as bytes. This buffer collects them until a newline.
char serialBuffer[48] = {0};
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
  // AccelStepper tracks position in motor steps. Convert it for the web UI.
  return static_cast<float>(stepper.currentPosition()) / stepsPerMm();
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

const char* motionStateName() {
  /*
    The web UI needs a human-readable state.
    We call the machine UP or DOWN if either:
    - the motor is actually moving that direction, or
    - the button command is asking for that direction.
  */
  const float speed = stepper.speed();
  if (speed > 0.5f || jogDirection > 0) {
    return "UP";
  }
  if (speed < -0.5f || jogDirection < 0) {
    return "DOWN";
  }
  return "IDLE";
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

void updateLoadCell() {
  /*
    Pseudo-code:
    - if the HX711 has a fresh reading, read it
    - on the first valid reading after startup, treat that as zero load

    This means the machine starts with a simple automatic tare. Later,
    ZERO_LOAD can be sent from the Pi to tare again.
  */
  if (!loadCell.is_ready()) {
    return;
  }

  rawAdc = loadCell.read();
  if (!tareSet) {
    // First valid load-cell reading becomes zero load for this power-up.
    tareForceN = rawForceN();
    tareSet = true;
  }
}

void updateButtons(uint32_t nowMs) {
  /*
    Pseudo-code:
    - debounce both direction buttons
    - if only UP is pressed, request positive jog speed
    - if only DOWN is pressed, request negative jog speed
    - if neither or both are pressed, request stop
    - decide whether the motor driver should be held enabled
  */
  upButton.update(nowMs);
  downButton.update(nowMs);

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

void applyMotionSettings(float speedStepsS, float accelerationStepsS2Value) {
  /*
    Apply settings sent from the Pi web UI.

    The Arduino clamps values again even though the browser also has limits.
    That keeps the firmware safe if a malformed command is sent over serial.
  */
  jogSpeedStepsS = clampFloat(
      speedStepsS,
      HardwareConfig::Motion::MinJogStepRateStepsS,
      HardwareConfig::Motion::MaxJogStepRateStepsS);
  accelerationStepsS2 = clampFloat(
      accelerationStepsS2Value,
      HardwareConfig::Motion::MinAccelerationStepsS2,
      HardwareConfig::Motion::MaxAccelerationStepsS2);
  // AccelStepper stores these limits internally.
  stepper.setMaxSpeed(jogSpeedStepsS);
  stepper.setAcceleration(accelerationStepsS2);
  // If a button is already held, update the active target speed immediately.
  targetStepRateStepsS = static_cast<float>(jogDirection) * jogSpeedStepsS;
}

void tareNow() {
  // Make the current load-cell force reading the new displayed zero.
  tareForceN = rawForceN();
  tareSet = true;
}

void emitMachinePayload(const char* stateName) {
  /*
    Send the fields that describe the machine at this instant.

    Field order:
      state,
      raw HX711 count,
      measured force in newtons,
      current step rate,
      estimated crosshead position,
      up button pressed,
      down button pressed,
      jog speed setting,
      acceleration setting

    TEL and STATUS both use this same payload so the Pi parses one format.
  */
  Serial.print(stateName);
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
  Serial.print(jogSpeedStepsS, 2);
  Serial.print(',');
  Serial.println(accelerationStepsS2, 2);
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
  emitMachinePayload(motionStateName());
}

void emitStatus() {
  // On-demand snapshot. The Pi asks for this after connecting with GET_STATUS.
  Serial.print(F("STATUS,"));
  emitMachinePayload(motionStateName());
}

void emitStatus(const char* stateName) {
  // Startup snapshot. This lets the Pi see BOOT before normal telemetry begins.
  Serial.print(F("STATUS,"));
  emitMachinePayload(stateName);
}

void handleCommand(char* line) {
  /*
    Handle one full command line from the Pi.

    Current command vocabulary:
      PING
      GET_STATUS
      ZERO_LOAD
      SET_MOTION,<speed_steps_s>,<acceleration_steps_s2>

    The Pi treats SET_MOTION as successful only after this firmware replies
    with ACK,SET_MOTION and the values that were actually applied.
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
    // Operator wants the current load reading to become zero.
    tareNow();
    Serial.println(F("ACK,ZERO_LOAD"));
  } else if (strcmp(command, "SET_MOTION") == 0) {
    // Read the two numbers after SET_MOTION.
    char* speedToken = strtok_r(nullptr, ",", &savePtr);
    char* accelerationToken = strtok_r(nullptr, ",", &savePtr);
    if (speedToken == nullptr || accelerationToken == nullptr) {
      Serial.println(F("ERR,INVALID_SET_MOTION"));
      return;
    }
    applyMotionSettings(atof(speedToken), atof(accelerationToken));
    // Echo the applied values, not the raw requested values, because clamping may occur.
    Serial.print(F("ACK,SET_MOTION,"));
    Serial.print(jogSpeedStepsS, 2);
    Serial.print(',');
    Serial.println(accelerationStepsS2, 2);
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
  // Load default jog speed and acceleration.
  applyMotionSettings(jogSpeedStepsS, accelerationStepsS2);
  // Start disabled, then let setMotorEnabled() apply the configured idle behavior.
  stepper.disableOutputs();
  setMotorEnabled(!HardwareConfig::Motion::DisableMotorWhenIdle);

  // Start reading physical inputs and the load-cell amplifier.
  upButton.begin(HardwareConfig::Pins::ButtonUp);
  downButton.begin(HardwareConfig::Pins::ButtonDown);
  loadCell.begin(
      HardwareConfig::Pins::Hx711Data,
      HardwareConfig::Pins::Hx711Clock,
      HardwareConfig::LoadCell::Hx711Gain);

  // Let the Pi know the firmware has booted and serial is alive.
  emitStatus("BOOT");
}

void loop() {
  const uint32_t nowMs = millis();

  /*
    Main control loop, repeated as fast as possible:

    1. processSerial()
       Check whether the Pi sent a command.

    2. updateLoadCell()
       Read force if the HX711 has a fresh value.

    3. updateButtons()
       Convert physical button state into desired jog direction.

    4. updateStepper()
       Ramp toward the target speed and generate step pulses.

    5. emitTelemetry()
       Every TelemetryPeriodMs, report the current machine state to the Pi.
  */
  processSerial();
  updateLoadCell();
  updateButtons(nowMs);
  updateStepper();

  if ((nowMs - lastTelemetryMs) >= HardwareConfig::Timing::TelemetryPeriodMs) {
    emitTelemetry(nowMs);
    lastTelemetryMs = nowMs;
  }
}
