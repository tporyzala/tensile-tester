# Tensile tester ground-zero stack

This `src/` workspace is intentionally small. The current behavior is:

- Arduino Uno reads two physical jog buttons.
- Button 1 on D2 jogs the load head up while held.
- Button 2 on D3 jogs the load head down while held.
- Releasing both buttons stops step pulses.
- Arduino reads the HX711 load cell and streams telemetry over USB serial.
- Raspberry Pi serves one web page with live force, raw ADC, position, step rate, button state, and a raw serial log.

There is no database, run storage, method editor, closed-loop load control, limit switch handling, overload shutoff, or test workflow in this version.

## Layout

```text
src/
|-- app/
|   |-- __init__.py
|   `-- main.py
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

## Current Pinout

The firmware uses `INPUT_PULLUP` for the physical buttons, so each button should connect its Arduino pin to ground when pressed.

```text
Stepper PUL-       D12
Stepper DIR-       D11
Stepper ENA-       D10, currently not driven by firmware
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
ENA+ -> +5V, only if ENA is used
D12  -> PUL-
D11  -> DIR-
D10  -> ENA-, only if ENA is used
```

By default `UseEnablePin = false`, so the firmware does not control ENA. Leave ENA disconnected or inactive unless you deliberately enable it in `HardwareConfig.h`.

## Firmware

Stepper pulses and direction are handled by AccelStepper in `DRIVER` mode. The current jog implementation uses constant-speed `setSpeed(...)` plus `runSpeed()` because the machine only needs hold-to-jog behavior right now.

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

constexpr float JogStepRateStepsS = 500.0f;
constexpr uint32_t StepPulseHighMicros = 20;
constexpr bool InvertStepPulse = true;
constexpr bool UseEnablePin = false;
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
- controller state
- raw HX711 ADC count
- estimated position
- step rate
- live Button 1 / Button 2 state
- raw serial telemetry sent to and from the Arduino

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
ZERO_LOAD
```

Arduino telemetry:

```text
TEL,<seq>,<time_ms>,<state>,<raw_adc>,<force_n>,<step_rate_steps_s>,<position_mm>,<button_up>,<button_down>
```

Arduino status:

```text
STATUS,<state>,<raw_adc>,<force_n>,<step_rate_steps_s>,<position_mm>,<button_up>,<button_down>
```

The raw serial log in the web UI prefixes lines with `TX`, `RX`, or `SYS`.

## Current Troubleshooting Notes

If the UI shows button state, step rate, and position changing, the Arduino firmware is commanding steps. Remaining no-motion causes are then outside the button logic:

- driver enable input is disabling the driver
- motor power is missing
- motor coil pairs are incorrect
- current limit is too low
- driver fault/alarm is active
- PUL/DIR polarity does not match the driver wiring

The current firmware avoids the most common ENA polarity issue by not driving ENA unless `UseEnablePin` is changed to `true`.

## Safety Scope

This is a bring-up build. It intentionally has no limit switches, software travel limits, overload shutoff, or emergency-stop behavior. Use only at low jog speed until those protections are added back.
