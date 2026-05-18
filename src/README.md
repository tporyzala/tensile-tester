# Tensile tester ground-zero stack

This `src/` workspace is intentionally small. The current behavior is:

- Arduino Uno reads two physical jog buttons.
- Button 1 on D2 jogs the load head up while held.
- Button 2 on D3 jogs the load head down while held.
- Releasing both buttons stops step pulses.
- Arduino reads the HX711 load cell and streams telemetry over USB serial.
- Raspberry Pi serves one web page with live force, raw ADC, position, step rate, button state, speed/acceleration sliders, and a raw serial log.

There is no database, run storage, method editor, closed-loop load control, limit switch handling, overload shutoff, or test workflow in this version.

## Layout

```text
src/
|-- app/
|   |-- __init__.py
|   |-- main.py              Python web API and serial bridge
|   `-- static/
|       |-- index.html       Page structure
|       |-- styles.css       Page appearance
|       `-- app.js           Browser polling and slider behavior
|-- firmware/
|   `-- arduino_uno/
|       |-- include/
|       |   `-- HardwareConfig.h
|       |-- src/
|       |   `-- main.cpp
|       `-- platformio.ini
|-- requirements.txt
`-- README.md
```

## How It Works

```text
Arduino:
  load cell + buttons -> motor control -> serial telemetry

Raspberry Pi Python app:
  serial telemetry -> MachineSnapshot -> web API

Browser:
  polls web API -> displays force/position/state
  sends slider changes -> Python -> Arduino SET_MOTION
  sends tare click -> Python -> Arduino ZERO_LOAD
```

The Arduino owns the real-time hardware work. It reads the HX711, reads the jog buttons, ramps the stepper speed, and sends `TEL` telemetry lines.

The Python app owns communication and display state. It reads serial lines from the Arduino, turns them into a `MachineSnapshot`, and exposes that snapshot to the browser.

The browser owns only the user interface. It polls `/api/snapshot` and posts slider changes to `/api/motion`.

## Current Pinout

The firmware uses `INPUT_PULLUP` for the physical buttons, so each button should connect its Arduino pin to ground when pressed.

```text
Stepper PUL-       D12
Stepper DIR-       D11
Stepper ENA-       D10
HX711 data         D4
HX711 clock        D5
Button 1, up       D2
Button 2, down     D3
Button 3           unused in firmware
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

The firmware always controls ENA on D10. If your driver enables at the wrong logic level, flip `InvertEnable` in `HardwareConfig.h`.

## Firmware

Stepper pulses and direction are handled by AccelStepper in `DRIVER` mode. The current jog implementation uses `setSpeed(...)` plus `runSpeed()` with a small firmware-side speed ramp so the acceleration setting has an immediate effect while keeping the known-good pulse path.

Important motion settings live in:

```text
firmware/arduino_uno/include/HardwareConfig.h
```

Current key values:

```cpp
constexpr uint8_t StepPulse = 12;
constexpr uint8_t StepDirection = 11;
constexpr uint8_t StepEnable = 10;
constexpr uint8_t ButtonUp = 2;
constexpr uint8_t ButtonDown = 3;

constexpr uint16_t Microstepping = 8;
constexpr float MinJogStepRateStepsS = 50.0f;
constexpr float MaxJogStepRateStepsS = 4000.0f;
constexpr float DefaultJogStepRateStepsS = 500.0f;
constexpr float MinAccelerationStepsS2 = 100.0f;
constexpr float MaxAccelerationStepsS2 = 10000.0f;
constexpr float DefaultAccelerationStepsS2 = 4000.0f;
constexpr uint32_t StepPulseHighMicros = 20;
constexpr bool InvertStepPulse = true;
constexpr bool InvertEnable = false;
```

PlatformIO dependencies:

```text
bogde/HX711
waspinator/AccelStepper
```

Build firmware:

```bash
cd ~/tensile-tester/src/firmware/arduino_uno
platformio run
```

Upload firmware:

```bash
platformio run --target upload --upload-port /dev/ttyACM0
```

On this Windows checkout, the verified local build command is:

```powershell
cd C:\Users\tomek\Desktop\tensile-tester\src
.\.venv\Scripts\platformio.exe run -d firmware\arduino_uno
```

## Web App

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
http://<raspberry-pi-ip>:8000
```

The page shows:

- force in newtons
- tare button to zero the displayed force at the current load
- controller state
- raw HX711 ADC count
- estimated position
- step rate
- live Button 1 / Button 2 state
- jog speed slider, 50 to 4000 steps/s
- acceleration slider, 100 to 10000 steps/s^2
- raw serial telemetry sent to and from the Arduino

Slider changes are sent when the slider edit is committed. They are not treated as confirmed just because the Pi sent a serial line. The web API waits for the Arduino to reply with `ACK,SET_MOTION,<speed>,<acceleration>`, retries up to three times if the command is not acknowledged or the Arduino reports a partial/unknown command token, and returns an error if no matching acknowledgement arrives.

The tare button posts to `/api/tare`. The Python app sends `ZERO_LOAD`, waits for `ACK,ZERO_LOAD`, then the Arduino treats the averaged current load-cell force as the new zero point for future telemetry. The firmware collects 10 HX711-ready readings without blocking the main loop, so serial handling, button reads, and stepper updates continue while tare is in progress.

Optional environment variables:

```text
TENSILE_SERIAL_PORT=/dev/ttyACM0
TENSILE_SERIAL_BAUDRATE=115200
TENSILE_SERIAL_RECONNECT_S=2.0
```

On Windows, the default serial port is `COM3`. On Linux/Raspberry Pi, the default is `/dev/ttyACM0`.

## Serial Protocol

The app sends `GET_STATUS` after connecting so the raw serial panel shows both transmit and receive traffic.

Pi to Arduino:

```text
PING
GET_STATUS
SET_MOTION,<jog_speed_steps_s>,<acceleration_steps_s2>
ZERO_LOAD
```

The Arduino applies `SET_MOTION` immediately, clamps values to firmware bounds, and only then replies:

```text
ACK,SET_MOTION,<applied_jog_speed_steps_s>,<applied_acceleration_steps_s2>
```

The Arduino applies `ZERO_LOAD` after averaging 10 HX711-ready readings and replies:

```text
ACK,ZERO_LOAD
```

Arduino telemetry:

```text
TEL,<seq>,<time_ms>,<state>,<raw_adc>,<force_n>,<step_rate_steps_s>,<position_mm>,<button_up>,<button_down>,<jog_speed_steps_s>,<acceleration_steps_s2>
```

Arduino status:

```text
STATUS,<state>,<raw_adc>,<force_n>,<step_rate_steps_s>,<position_mm>,<button_up>,<button_down>,<jog_speed_steps_s>,<acceleration_steps_s2>
```

The raw serial log in the web UI prefixes lines with `TX`, `RX`, or `SYS`. It is kept for the lifetime of the running web app process and is cleared when the app restarts.

## Current Troubleshooting Notes

If the UI shows button state, step rate, and position changing, the Arduino firmware is commanding steps. Remaining no-motion causes are then outside the button logic:

- driver enable input is disabling the driver
- motor power is missing
- motor coil pairs are incorrect
- current limit is too low
- driver fault/alarm is active
- PUL/DIR polarity does not match the driver wiring

If the motor only moves when it should be disabled, or disables when it should move, flip `InvertEnable` in `HardwareConfig.h`.

## Safety Scope

This is a bring-up build. It intentionally has no limit switches, software travel limits, overload shutoff, or emergency-stop behavior. Use only at low jog speed until those protections are added back.
