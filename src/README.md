# Tensile tester ground-zero stack

This `src/` workspace is intentionally small. The current behavior is:

- Arduino Uno reads the HX711 load cell, two jog buttons, and one stop button.
- In setup mode, Button 1 on D2 jogs the load head up while held and Button 2 on D3 jogs it down while held.
- Releasing both jog buttons stops setup jog step pulses. Button 3 on D4 is reported as the stop button.
- During an automated test, Button 1 pauses/resumes and Button 2 or Button 3 stops the run.
- Raspberry Pi serves one automated-test page where the operator defines any number of force or displacement steps.
- The automated-test page keeps an in-memory sample set: one specimen at a time, editable step methods, setup motion sliders, sample ID/notes, include/exclude controls, force-displacement overlays, setup tare/displacement zeroing, live charts, a raw serial log, and one XLSX workbook export.
- Plots keep accumulating data until the operator clicks the plot Clear button.

There is no database, persistent run storage across app restarts, limit switch handling, overload shutoff, audit trail, ASTM report generation, barcode workflow, or emergency-stop chain in this version.

## Layout

```text
src/
|-- app/
|   |-- __init__.py
|   |-- main.py              FastAPI web API and serial bridge
|   `-- static/
|       |-- test.html        Automated-test page
|       |-- styles.css       Shared page appearance
|       |-- live-charts.js   Plotly chart adapter and navigation
|       |-- vendor/          Vendored browser libraries
|       `-- test.js          Automated-test page behavior
|-- firmware/
|   `-- arduino_uno/
|       |-- include/
|       |   `-- HardwareConfig.h
|       |-- src/
|       |   `-- main.cpp
|       `-- platformio.ini
|-- requirements.txt
|-- tests/
|   `-- test_multisample.py
`-- README.md
```

## How It Works

```text
Arduino:
  HX711 + buttons -> motion/test state machine -> serial telemetry

Python app:
  serial telemetry -> MachineSnapshot/TestRunState -> web API
  web commands -> ACK-confirmed Arduino serial commands
  finalized specimen telemetry -> in-memory sample set

Browser:
  automated page polls /api/test/state
  sends setup motion, test, stop, sample-set, and return-to-zero commands
```

The Arduino owns the hardware loop. It reads the HX711, debounces the buttons, updates the stepper, runs the automated-test state machine, and emits `TEL` telemetry every 100 ms.

The Python app owns serial communication, command acknowledgement, API state, sample-set memory, and XLSX workbook generation. Motion, tare, displacement-zero, and test commands are not treated as successful until the expected Arduino `ACK,...` is consumed.

The browser owns only the operator interface. It does not persist test records. Sample sets live in the Python process and disappear when the app restarts or when the operator clicks Clear Set.

## Current Pinout

The firmware uses `INPUT_PULLUP` for the physical buttons, so each button should connect its Arduino pin to ground when pressed.

```text
Stepper PUL-       D12
Stepper DIR-       D11
Stepper ENA-       D10
HX711 data         D5
HX711 clock        D6
Button 1, up       D2
Button 2, down     D3
Button 3, stop     D4
E-stop             unused in this build
```

The current driver assumption is common-positive signal wiring:

```text
PUL+ -> +5V
DIR+ -> +5V
ENA+ -> +5V
D12  -> PUL-
D11  -> DIR-
D10  -> ENA-
```

The firmware always controls ENA on D10. If your driver enables at the wrong logic level, flip `InvertEnable` in `firmware/arduino_uno/include/HardwareConfig.h`.

## Firmware

Stepper pulses and direction are handled by AccelStepper in `DRIVER` mode. Setup jog uses `setSpeed(...)` plus `runSpeed()` with a small firmware-side speed ramp so acceleration changes take effect while keeping the known-good pulse path.

Important motion, test, load-cell, and timing settings live in:

```text
firmware/arduino_uno/include/HardwareConfig.h
```

Current key values:

```cpp
constexpr uint8_t StepPulse = 12;
constexpr uint8_t StepDirection = 11;
constexpr uint8_t StepEnable = 10;
constexpr uint8_t Hx711Data = 5;
constexpr uint8_t Hx711Clock = 6;
constexpr uint8_t ButtonUp = 2;
constexpr uint8_t ButtonDown = 3;
constexpr uint8_t ButtonStop = 4;

constexpr float MotorStepsPerRev = 200.0f;
constexpr float GearboxRatio = 19.203f;
constexpr float ScrewPitchMmPerRev = 4.0f;
constexpr uint16_t Microstepping = 8;

constexpr float MinJogStepRateStepsS = 50.0f;
constexpr float MaxJogStepRateStepsS = 4000.0f;
constexpr float DefaultJogStepRateStepsS = 4000.0f;
constexpr float MinAccelerationStepsS2 = 100.0f;
constexpr float MaxAccelerationStepsS2 = 10000.0f;
constexpr float DefaultAccelerationStepsS2 = 10000.0f;
constexpr uint32_t StepPulseHighMicros = 20;
constexpr bool InvertDirection = false;
constexpr bool InvertStepPulse = true;
constexpr bool InvertEnable = false;
constexpr bool DisableMotorWhenIdle = false;

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

constexpr uint8_t Hx711Gain = 128;
constexpr float CalibrationSlopeNPerCount = 0.002283289f;
constexpr float CalibrationInterceptN = 0.0f;
constexpr bool InvertSign = false;
constexpr uint32_t TareDurationMs = 5000;

constexpr uint16_t TelemetryPeriodMs = 100;
constexpr uint16_t ButtonDebounceMs = 25;
```

Setup jog and automated tests intentionally have different speed ceilings:

- Setup jog slider: `50` to `4000 steps/s`, default `4000 steps/s`.
- Automated test max-speed slider: `50` to `4000 steps/s`, default `2000 steps/s`.
- Shared acceleration slider: `100` to `10000 steps/s^2`, default `10000 steps/s^2`.
- With the current mechanics, `2000 steps/s` is about `0.2604 mm/s`, and `10000 steps/s^2` is about `1.3019 mm/s^2`, because the conversion is `200 * 19.203 * 8 / 4 = 7681.2 steps/mm`.

PlatformIO dependencies:

```text
bogde/HX711
waspinator/AccelStepper
```

PlatformIO is the firmware build/upload tool. On a clean Raspberry Pi, install it into the repo virtual environment:

```bash
cd ~/tensile-tester/src
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install platformio
```

Build firmware on the Pi:

```bash
cd ~/tensile-tester/src/firmware/arduino_uno
platformio run
```

Upload firmware:

```bash
platformio run --target upload --upload-port /dev/ttyACM0
```

If `platformio` is not found, activate the repo virtual environment first:

```bash
cd ~/tensile-tester/src
source .venv/bin/activate
```

If PlatformIO reports a broken JSON file under `.pio/libdeps`, clear the generated local PlatformIO cache and rebuild:

```bash
cd ~/tensile-tester/src/firmware/arduino_uno
rm -rf .pio
platformio run
```

On this Windows checkout, the verified local build command is:

```powershell
cd C:\Users\tomek\Desktop\tensile-tester\src
.\.venv\Scripts\platformio.exe run -d firmware\arduino_uno
```

## Web App

Python dependencies are listed in `requirements.txt`:

```text
fastapi
uvicorn[standard]
pyserial
XlsxWriter
```

Run on the Raspberry Pi:

```bash
cd ~/tensile-tester/src
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
export TENSILE_SERIAL_PORT=/dev/ttyACM0
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open:

```text
http://<raspberry-pi-ip>:8000/test
```

The root URL `http://<raspberry-pi-ip>:8000/` redirects to `/test`.

Optional environment variables:

```text
TENSILE_SERIAL_PORT=/dev/ttyACM0
TENSILE_SERIAL_BAUDRATE=115200
TENSILE_SERIAL_RECONNECT_S=2.0
TENSILE_SERIAL_LOG_MAX_LINES=500
```

On Windows, the default serial port is `COM3`. On Linux/Raspberry Pi, the default is `/dev/ttyACM0`.

## Automated Test Mode

Automated test mode is served at:

```text
http://<raspberry-pi-ip>:8000/test
```

The root URL redirects to `/test`, so there is no separate manual-mode page. The sidebar remains in place with the automated-test entry so more pages can be added later.

The left sidebar switches between Automated Test, Calibration, and Setup. Automated Test keeps the live plots, compact machine status, test actions, method steps, sample set, and raw serial log. Calibration captures averaged raw ADC points, fits a linear load-cell calibration, and reports firmware-ready slope/intercept constants for `HardwareConfig.h`. Setup contains tester motion sliders for physical button jog speed, automated-test maximum step rate, and shared acceleration. Slider changes post to `/api/motion`; the Python app sends `SET_MOTION_LIMITS`, waits for `ACK,SET_MOTION_LIMITS`, and only then reports the applied settings.

An automated test has any number of steps. Each step contains:

- target type and value: force in newtons, or crosshead displacement in millimeters
- rate type and value: force in newtons per second, or crosshead displacement in millimeters per second
- hold duration in seconds; zero means ramp-only

Target and rate types are independent. For example, a step can move at `0.02 mm/s` until it reaches `100 N`, or increase the commanded force at `10 N/s` until it reaches `2.0 mm` displacement. Signed target values allow tension/compression or either crosshead direction; rate values are entered as positive magnitudes.

The step table and motion settings can be saved as named test methods. The save icon opens a name prompt; saving with the same sanitized name overwrites that method, while saving with a new name creates a new method. The load icon opens a scrollable method picker. Loaded methods remain editable before running, and can be saved back to the same method or saved under a new name. Method files are JSON documents stored in `data/test-methods` by default; set `TENSILE_METHOD_DIR` to use another directory.

Each sample has a sample ID and optional notes. The default sample ID increments as `Sample 1`, `Sample 2`, and so on. Sample IDs are limited to 64 characters and notes are limited to 200 characters.

Completed samples are included by default. Stopped or faulted samples are retained in the sample set but excluded from overlays until the operator includes them. The sample table shows index, ID, status, method, point count, peak force, final displacement, include/exclude state, and notes.

The Tare button posts to `/api/tare`. The Python app sends `ZERO_LOAD`, waits for `ACK,ZERO_LOAD`, then the Arduino treats the average of all fresh load-cell readings collected over 5 seconds as the new zero point. Tare collection is non-blocking, so serial handling, button reads, and stepper updates continue while tare is in progress.

The Zero Displacement button posts to `/api/zero-displacement`. The Python app sends `ZERO_DISPLACEMENT` and waits for `ACK,ZERO_DISPLACEMENT`; the Arduino stores the current step count as the new displayed displacement origin without changing stepper motion tracking.

The automated-test page has fixed `+100`, `+10`, `+1`, `-1`, `-10`, and `-100 mm` relative-move buttons. A button posts its fixed offset to `/api/test/move-relative`; the Python app converts the offset to an absolute displacement target from the latest stationary position and runs one displacement step at the configured automated-test maximum step rate. Relative moves use the same stop and fault handling as Return to Zero and are not archived as specimen samples.

The live charts are rendered with the vendored Plotly basic bundle. The Python app retains every received periodic telemetry point for the live plot buffer until the operator clicks Clear, while the browser incrementally renders only the most recent 5,000 live points. The force-displacement chart can overlay finalized included samples; overlay display traces are uniformly reduced to at most 2,000 points per sample and drawn without markers. Chart rendering pauses while the Automated Test tab or browser tab is hidden. The workbook export keeps full retained specimen telemetry.

The Set XLSX link downloads one workbook from `/api/test/samples/csv`. The route name is historical; the response is an `.xlsx` file named `tensile-sample-set.xlsx`. Each sample gets its own worksheet named from the sample ID, with Excel-invalid worksheet characters replaced and duplicate names made unique. The workbook includes the method ID, method name, method hash, and full method snapshot used when that sample started.

The Clear Set button clears all in-memory sample records, active sample metadata, current test steps, and retained test telemetry. It is rejected while a specimen test, return-to-zero move, relative move, or faulted run is active.

Live plots do not clear automatically when a new sample starts or finishes. Use the Live Plots Clear button to clear the plot buffer and reset the chart view. Clearing the plots does not delete retained specimen samples or XLSX export data.

## Return To Zero

Return to Zero is a utility operation, not a specimen sample. Utility telemetry is not archived into the sample set and is not included in the workbook export.

The automated-test page supports two return modes:

- Load: one force-target step to `0 N` at the selected load rate, followed by a 1 second hold. The default rate is `10 N/s`.
- Displacement: one displacement-target step to `0 mm` at the selected displacement rate. The default rate tracks the automated-test maximum step-rate slider.

Return to Zero uses the same `START_TEST` / `TEST_STEP` machinery as specimen tests, but it is tracked as `RETURN_ZERO` in the Python app instead of `SPECIMEN`.

Return to Zero is enabled only when the run state is setup/idle or complete. It is disabled while a specimen test is starting, running, paused, waiting for a step, faulted, or while tare/displacement-zero commands are in flight.

## Stop Behavior

There is no separate abort concept in the current codebase.

Stop means:

- stop motor motion
- finalize any active specimen sample as `STOPPED`
- exclude that stopped sample from overlays by default
- return the controller and web run state to setup/idle
- keep the partial sample in the sample set until Clear Set or app restart

In automated test mode, the web Stop button posts to `/api/test/stop`. During a test, physical Button 2 and Button 3 both perform the same stop action. If a run faults, Stop is the operator path back to setup before taring, zeroing displacement, clearing the sample set, or starting a return-to-zero move.

## Lite Operator Protection Model

The firmware separates machine permission from test progress:

```text
Frame mode = what the machine is allowed to do
Test phase = where the current firmware test program is
Fault reason = why the machine stopped abnormally
```

Frame modes:

```text
SETUP    setup jog, tare, and displacement zeroing are allowed
ARMED    a test run exists and the Arduino is waiting for the Pi
TESTING  the Arduino owns automated motion or pause
FAULT    motion is stopped; STOP_TEST returns to SETUP
```

Firmware test phases:

```text
NONE          no firmware test is active
WAITING_STEP  waiting for the Pi to send the next TEST_STEP
RAMPING       following the selected force or displacement rate toward the endpoint
HOLDING       holding the force or displacement endpoint for the requested duration
PAUSED        test is paused by Button 1 or PAUSE_TEST
FAULTED       run stopped due to a fault
```

Python run statuses:

```text
IDLE          no active run
STARTING      Pi is sending START_TEST / first TEST_STEP
RUNNING       active automated step is ramping or holding
PAUSED        run is paused
WAITING_NEXT  Pi is sending the next TEST_STEP
COMPLETE      completed run result remains visible until another run or Clear Set
FAULT         faulted run must be stopped before setup actions are allowed
```

Specimen sample statuses:

```text
COMPLETE  sample completed and is included by default
STOPPED   operator stopped the run; partial sample is retained but excluded by default
FAULT     run faulted; partial sample is retained but excluded by default
```

Normal specimen flow:

```text
SETUP / NONE
  -> START_TEST
ARMED / WAITING_STEP
  -> TEST_STEP
TESTING / RAMPING
  -> target reached
TESTING / HOLDING
  -> hold time reached
ARMED / WAITING_STEP
  -> next TEST_STEP when steps remain

Final hold complete
  -> EVT,TEST_COMPLETE
  -> Arduino returns to SETUP / NONE
  -> Python finalizes the sample as COMPLETE

STOP_TEST from any active or faulted test
  -> motion stops
  -> any active specimen sample is finalized as STOPPED
  -> Arduino and Python return to SETUP / NONE
```

For a force-rate ramp, the Arduino moves the commanded force at the requested rate and the PID converts force error to motor step rate. For a displacement-rate ramp, the Arduino commands crosshead speed directly until the endpoint is reached. A force endpoint is then held under PID control; a displacement endpoint is held by keeping the motor enabled at the reached position. `Kd` is currently `0.0` because the HX711 force signal can be noisy; start with `Kp` and `Ki` before adding derivative action.

## Web API

Pages:

```text
GET  /      redirects to /test
GET  /test
```

Setup/support API:

```text
POST /api/motion
POST /api/tare
POST /api/zero-displacement
GET  /health
```

Automated-test API:

```text
GET  /api/test/state
POST /api/test/start
POST /api/test/return-zero
POST /api/test/move-relative
POST /api/test/pause
POST /api/test/resume
POST /api/test/stop
POST /api/test/samples/clear
POST /api/test/samples/include
GET  /api/test/samples/overlay
GET  /api/test/plots
POST /api/test/plots/clear
GET  /api/test/samples/csv
```

`POST /api/test/start` accepts the current backward-compatible shape:

```json
{
  "steps": [
    {
      "target_type": "FORCE",
      "target_value": 100.0,
      "rate_type": "FORCE",
      "rate_value_per_s": 10.0,
      "hold_duration_s": 5.0
    }
  ]
}
```

It also accepts optional sample metadata:

```json
{
  "sample": {
    "id": "A-1",
    "notes": "first coupon"
  },
  "steps": [
    {
      "target_type": "DISPLACEMENT",
      "target_value": 2.0,
      "rate_type": "DISPLACEMENT",
      "rate_value_per_s": 0.02,
      "hold_duration_s": 0.0
    }
  ]
}
```

## Serial Protocol

The app sends `GET_STATUS` after connecting so the raw serial panel shows both transmit and receive traffic.

Pi to Arduino:

```text
PING
GET_STATUS
SET_MOTION_LIMITS,<jog_speed_steps_s>,<test_max_step_rate_steps_s>,<acceleration_steps_s2>
ZERO_LOAD
ZERO_DISPLACEMENT
START_TEST,<run_id>,<step_count>
TEST_STEP,<run_id>,<step_index>,<target_type>,<target_value>,<rate_type>,<rate_value_per_s>,<hold_duration_ms>
TEST_HB,<run_id>
PAUSE_TEST,<run_id>
RESUME_TEST,<run_id>
STOP_TEST,<run_id>
```

For `TEST_STEP`, both `<target_type>` and `<rate_type>` are `FORCE` or `DISPLACEMENT`. Force PID output and displacement-rate commands are limited by the automated-test maximum step-rate setting before they are applied to the motor.

Acknowledgements:

```text
ACK,PING
ACK,SET_MOTION_LIMITS,<applied_jog_speed_steps_s>,<applied_test_max_step_rate_steps_s>,<applied_acceleration_steps_s2>
ACK,ZERO_LOAD
ACK,ZERO_DISPLACEMENT
ACK,START_TEST,<run_id>
ACK,TEST_STEP,<run_id>,<step_index>
ACK,PAUSE_TEST,<run_id>
ACK,RESUME_TEST,<run_id>
ACK,STOP_TEST,<run_id>
```

Test events:

```text
EVT,STEP_COMPLETE,<run_id>,<step_index>
EVT,TEST_COMPLETE,<run_id>
EVT,TEST_PAUSED,<run_id>
EVT,TEST_RESUMED,<run_id>
EVT,TEST_STOPPED,<run_id>
EVT,TEST_FAULT,<run_id>,HEARTBEAT_TIMEOUT
```

The Pi sends `TEST_HB` every 0.5 seconds during active tests. If the Arduino does not receive a heartbeat for 2 seconds while the test needs one, it stops motion, keeps the motor enabled, enters `FAULT / FAULTED`, and emits `EVT,TEST_FAULT,<run_id>,HEARTBEAT_TIMEOUT`.

Arduino telemetry:

```text
TEL,<seq>,<time_ms>,<frame_mode>,<test_phase>,<fault_reason>,<raw_adc>,<force_n>,<step_rate_steps_s>,<position_mm>,<button_up>,<button_down>,<button_stop>,<jog_speed_steps_s>,<acceleration_steps_s2>,<test_max_step_rate_steps_s>,<test_run_id>,<test_step_index>,<test_step_count>,<test_control_mode>,<test_setpoint_force_n>,<test_setpoint_displacement_mm>,<test_elapsed_ms>
```

Arduino status:

```text
STATUS,<frame_mode>,<test_phase>,<fault_reason>,<raw_adc>,<force_n>,<step_rate_steps_s>,<position_mm>,<button_up>,<button_down>,<button_stop>,<jog_speed_steps_s>,<acceleration_steps_s2>,<test_max_step_rate_steps_s>,<test_run_id>,<test_step_index>,<test_step_count>,<test_control_mode>,<test_setpoint_force_n>,<test_setpoint_displacement_mm>,<test_elapsed_ms>
```

The raw serial log in the web UI prefixes lines with `TX`, `RX`, or `SYS`. To keep long-running browser refreshes responsive, the UI receives only the most recent `TENSILE_SERIAL_LOG_MAX_LINES` lines, defaulting to `500`.

## Local Checks

Useful local verification commands from this checkout:

```powershell
cd C:\Users\tomek\Desktop\tensile-tester\src
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe -m compileall app tests
node --check app\static\live-charts.js
node --check app\static\test.js
.\.venv\Scripts\platformio.exe run -d firmware\arduino_uno
```

## Current Troubleshooting Notes

If the UI shows button state, step rate, and position changing, the Arduino firmware is commanding steps. Remaining no-motion causes are then outside the button logic:

- driver enable input is disabling the driver
- motor power is missing
- motor coil pairs are incorrect
- current limit is too low
- driver fault/alarm is active
- PUL/DIR polarity does not match the driver wiring

If the motor only moves when it should be disabled, or disables when it should move, flip `InvertEnable` in `HardwareConfig.h`.

If a command works from the UI but the machine state does not change, check the raw serial log for missing or mismatched `ACK,...` lines. The Python app waits for exact ACK tokens and will reject commands that the Arduino does not confirm.

## Safety Scope

This is a bring-up build. It intentionally has no limit switches, software travel limits, overload shutoff, or emergency-stop behavior. Stop is a software/serial stop path, not a safety-rated E-stop. Use low loads and low speeds until independent safety protections are added.
