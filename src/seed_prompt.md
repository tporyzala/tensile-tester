# Tensile Testing Machine MVP – Full Stack + Firmware Mega Prompt

You are a senior embedded systems engineer, controls engineer, and full-stack software architect.

Your task is to design and implement a complete MVP software + firmware stack for a personal tensile/compression testing machine using:

- Raspberry Pi 3B
- Arduino Uno R3
- TB6600 stepper driver
- Stepper motor with 19.203:1 gearbox
- 200 step/rev motor
- 4 mm screw pitch
- HX711 load cell amplifier
- 1000 kg load cell

This is NOT a production-certified industrial machine. It is a personal engineering/research machine MVP focused on simplicity, maintainability, readability, and fast iteration.

The goal is to create a clean architecture that can later evolve into a more advanced system.

---

# High-Level Architecture

## System Responsibilities

### Arduino Responsibilities (Real-Time Controller)

Arduino owns all real-time behavior:

- HX711 reading
- force calculation
- PID loop
- stepper control
- button handling
- machine state machine
- safety handling
- serial telemetry output
- serial command handling

The Arduino must continue operating safely if the Raspberry Pi crashes or disconnects.

The Arduino must boot into a safe idle state.

The Arduino must refuse motion until configuration is received from the Pi.

---

### Raspberry Pi Responsibilities

The Raspberry Pi owns:

- web UI
- database
- test method management
- sample metadata
- run management
- plotting
- telemetry logging
- calibration management
- CSV export
- admin settings
- sending configuration to Arduino
- test orchestration

---

# Technology Stack

Use the following stack unless there is an extremely compelling reason not to.

## Backend

- FastAPI
- SQLAlchemy
- SQLite
- WebSockets
- Jinja2 templates
- HTMX for lightweight interactivity

Avoid React/Vue/etc for MVP.

Keep dependencies minimal.

---

## Frontend

Requirements:

- light mode only
- simple industrial/scientific UI
- responsive but not mobile-focused
- designed for kiosk/fullscreen Pi operation
- modern-ish UX but minimal complexity

Use:

- Plotly.js for live plotting
- HTMX for dynamic updates
- lightweight CSS only

No heavy frontend frameworks.

---

## Firmware

Arduino Uno R3 firmware in C++.

Requirements:

- modular
- non-blocking
- millis()-based timing
- no delay()-based architecture
- clean state machine
- clear separation between:
  - control loop
  - serial handling
  - telemetry
  - motion control
  - button handling

---

# Machine Description

## Motion System

- stepper motor
- TB6600 driver
- PUL/DIR/ENA interface
- open loop
- vertical actuator
- compression and tension both supported
- physical up/down jog buttons
- ENA can disable motor holding torque

Compression uses negative force values.

Tension uses positive force values.

---

## Stepper Parameters

Initial assumptions:

- 200 steps/rev
- gearbox ratio = 19.203:1
- screw pitch = 4 mm/rev
- microstepping configurable in admin panel
- likely 2x or 4x microstepping

Admin settings should include:

- microstepping
- max step rate
- max acceleration
- jog speed
- return-to-zero rate

---

# Load Cell System

## Hardware

- HX711
- 1000 kg load cell

Units:

- Newtons only

---

## Calibration

Calibration should support:

### MVP

Linear calibration only:

```text
force_N = slope * raw_adc + intercept - tare_offset
```

Store calibration in SQLite.

Send calibration to Arduino on boot and whenever updated.

Arduino should use calibration internally for PID.

Arduino should stream BOTH:

- raw ADC
- calibrated force

The architecture should be extensible later for multi-point calibration.

---

## Tare / Zero

- only allowed in IDLE
- temporary per-run offset
- calibration persists globally
- tare offset does not persist between runs

---

# Control Strategy

## Force Control

This machine is force-controlled only for MVP.

No displacement control.

No extensometer.

No DIC yet.

---

## PID

PID runs on Arduino.

Use velocity-based control:

```text
force_error -> PID -> signed step rate
```

DO NOT make PID directly output steps.

PID output determines stepper velocity and direction.

---

## PID Requirements

Implement:

- P gain
- I gain
- D gain
- deadband
- anti-windup
- output clamping
- acceleration limiting

Admin panel must allow editing:

- P
- I
- D
- deadband
- max step rate
- max acceleration

Only editable while IDLE.

No live tuning during active tests.

---

## Suggested Rates

Use fixed rates rather than "maximum possible".

Suggested defaults:

- control loop = 80–100 Hz
- telemetry rate = 20 Hz
- live plot refresh = 10–20 Hz

Explain and document all timing choices.

---

# Test Definition System

A test method contains ordered steps.

Supported step types:

## RAMP_TO_LOAD

Fields:

- target_force_N
- rate_N_per_s
- timeout_s

Behavior:

Ramp toward target force at specified rate.

---

## HOLD_LOAD

Fields:

- target_force_N
- duration_s

Behavior:

Maintain target force for duration.

---

# Test Completion Behavior

There is NO persistent COMPLETE state.

After final step:

```text
RUNNING
-> RETURNING_TO_ZERO
-> TEST_COMPLETE event
-> IDLE
```

Return-to-zero should be automatic.

User does NOT manually define return-to-zero.

Admin settings should include:

```text
return_to_zero_rate_N_per_s
```

When near zero force:

- finalize run
- save telemetry
- mark run complete
- return to IDLE

---

# Test Flow

Normal user flow:

```text
Select method
-> enter sample metadata
-> zero load
-> arm
-> start
-> run steps
-> automatic return to zero
-> save
-> idle
```

---

# Sample Metadata

Minimal MVP fields:

- sample name
- notes

A method may be reused across many runs.

Runs should auto-name like:

```text
MethodName_SampleName_YYYYMMDD_HHMMSS
```

---

# States

Implement the following states ONLY:

```text
BOOT
WAITING_FOR_PI_CONFIG
IDLE
SETUP
ARMED
RUNNING
RETURNING_TO_ZERO
PAUSED
ABORTED
ESTOPPED
FAULT
```

DO NOT implement:

```text
COMPLETE
HOLDING_FINAL_LOAD
MANUAL_TEST
```

---

# State Definitions

## IDLE

- jog allowed
- tare allowed
- test selection allowed
- motors enabled

---

## SETUP

Manual adjustment mode.

Behavior:

- motor disabled via ENA
- no jog
- no test start
- user can physically move mechanism
- exit explicitly back to IDLE

---

## ARMED

Method loaded and ready.

Waiting for Start command.

---

## RUNNING

Executing test steps.

Logging telemetry.

---

## RETURNING_TO_ZERO

Controlled automatic return to zero force.

Still logs telemetry.

---

## PAUSED

Motion stopped.

Motor remains energized.

Resume returns to prior state.

---

## ABORTED

Immediate stop.

Run saved as aborted.

---

## ESTOPPED

Triggered by physical E-stop button.

Behavior:

- stop pulses
- disable ENA
- mark run estopped
- require acknowledgement/reset

---

## FAULT

Entered on software/hardware fault.

---

# Button System

There are:

- 3 normal buttons
- 1 E-stop button

Buttons are wired to Arduino.

---

# Physical Button Mapping

## IDLE

| Button | Action |
|---|---|
| Button 1 | Jog up |
| Button 2 | Jog down |
| Button 3 | Zero/tare |
| E-stop | ESTOPPED |

---

## SETUP

| Button | Action |
|---|---|
| Button 1 | no-op |
| Button 2 | no-op |
| Button 3 | Return to IDLE |
| E-stop | ESTOPPED |

---

## ARMED

| Button | Action |
|---|---|
| Button 1 | Start |
| Button 2 | Cancel to IDLE |
| Button 3 | Abort |
| E-stop | ESTOPPED |

---

## RUNNING

| Button | Action |
|---|---|
| Button 1 | no-op |
| Button 2 | Pause |
| Button 3 | Abort |
| E-stop | ESTOPPED |

---

## RETURNING_TO_ZERO

| Button | Action |
|---|---|
| Button 1 | no-op |
| Button 2 | Pause |
| Button 3 | Abort |
| E-stop | ESTOPPED |

---

## PAUSED

| Button | Action |
|---|---|
| Button 1 | Resume |
| Button 2 | Return to zero |
| Button 3 | Abort |
| E-stop | ESTOPPED |

---

## ABORTED

| Button | Action |
|---|---|
| Button 1 | no-op |
| Button 2 | no-op |
| Button 3 | Acknowledge to IDLE |
| E-stop | ESTOPPED |

---

## ESTOPPED

| Button | Action |
|---|---|
| Button 1 | no-op |
| Button 2 | no-op |
| Button 3 | Acknowledge after release |
| E-stop | stays ESTOPPED |

---

## FAULT

| Button | Action |
|---|---|
| Button 1 | no-op |
| Button 2 | no-op |
| Button 3 | Acknowledge to IDLE |
| E-stop | ESTOPPED |

---

# Safety / Faults

MVP fault conditions:

- overload > global threshold
- serial/config missing
- invalid commands
- PID instability detection if reasonable

Global overload setting:

```text
1000 N default
```

All aborted, faulted, and estopped runs must save partial telemetry.

---

# Serial Protocol

Use human-readable ASCII protocol.

Clearly define syntax.

All commands must be acknowledged.

All telemetry must include timestamps and sequence numbers.

---

## Example Commands

```text
PING
GET_STATUS
LOAD_CONFIG
LOAD_METHOD
START
PAUSE
RESUME
ABORT
ENTER_SETUP
EXIT_SETUP
ZERO_LOAD
RESET_FAULT
```

---

## Example Telemetry

```text
TEL,seq,time_ms,state,raw_adc,force_N,target_force_N,step_rate_steps_s,estimated_mm
```

---

## Example Events

```text
EVENT,TEST_STARTED
EVENT,STEP_STARTED
EVENT,STEP_COMPLETED
EVENT,RETURN_ZERO_STARTED
EVENT,TEST_COMPLETE
EVENT,ABORTED
EVENT,ESTOPPED
EVENT,FAULT
```

---

## Example Acknowledgements

```text
ACK,START
ACK,PAUSE
ERR,INVALID_STATE
```

---

# Displacement Estimate

No true displacement sensor exists yet.

However, estimate displacement from motor motion.

Log:

```text
estimated_crosshead_mm
```

Clearly label it as estimated only.

---

# Database Requirements

Use SQLite.

Store:

- test methods
- test steps
- sample metadata
- runs
- telemetry
- calibration
- admin settings
- fault logs

Raw telemetry should be immutable after save.

Support CSV export.

---

# Web UI Pages

Implement only:

```text
Dashboard / Run Test
Test Methods
Results
Calibration
Admin Settings
```

---

# Dashboard Requirements

Must include:

- live force display
- live target force display
- live plot
- machine state
- test controls
- jog controls
- setup mode control
- sample metadata entry

---

# Plotting Requirements

Use Plotly.js.

Requirements:

- responsive
- rolling live plot
- force vs time
- target vs actual
- efficient updates
- avoid plotting every point forever

---

# Admin Panel Requirements

Admin settings page must support:

- PID gains
- deadband
- overload threshold
- microstepping
- jog speed
- max step rate
- max acceleration
- return-to-zero rate
- calibration slope/intercept
- invert motor direction
- invert load cell sign

---

# Project Structure

The repository already exists as:

```text
tensile-tester/
```

This repository also contains non-software assets such as:

- CAD files
- mechanical design files
- documentation
- experimental data
- electronics files

Therefore, ALL software and firmware source code must live under a dedicated:

```text
src/
```

directory.

Generate the project structure accordingly.

Example:

```text
tensile-tester/
│
├── cad/
├── docs/
├── electronics/
├── data/
├── experiments/
│
├── src/
│   ├── firmware/
│   │   └── arduino_uno/
│   │       ├── include/
│   │       ├── src/
│   │       ├── platformio.ini
│   │       └── README.md
│   │
│   ├── app/
│   │   ├── routes/
│   │   ├── services/
│   │   ├── templates/
│   │   ├── static/
│   │   ├── websocket/
│   │   ├── serial/
│   │   ├── models/
│   │   ├── database/
│   │   ├── telemetry/
│   │   ├── plotting/
│   │   └── main.py
│   │
│   ├── scripts/
│   │
│   ├── requirements.txt
│   └── README.md
│
└── README.md
```

The generated architecture should assume the repository may eventually contain:

- CAD assemblies
- manufacturing files
- PCB designs
- calibration datasets
- test datasets
- images/videos
- future embedded targets

Keep the software stack cleanly isolated within `src/`.

---

# Code Generation Requirements

Generate:

- firmware
- FastAPI backend
- database models
- HTML templates
- JS frontend
- CSS
- serial communication layer
- PID implementation
- telemetry logger
- WebSocket updates
- README
- setup instructions

Focus heavily on:

- readability
- maintainability
- simplicity
- modularity
- deterministic behavior
- minimal dependencies

Avoid overengineering.

Build a robust MVP first.