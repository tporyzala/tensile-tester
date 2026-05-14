# Tensile tester ground-zero stack

This `src/` workspace is intentionally small. The current behavior is only:

- Arduino reads two physical buttons.
- Button 1 jogs the load head up while held.
- Button 2 jogs the load head down while held.
- Releasing both buttons stops motion.
- Arduino reads the HX711 load cell and streams telemetry over USB serial.
- Raspberry Pi serves one web page showing the live force reading.

There is no database, run storage, method editor, closed-loop load control, limit switch handling, or test workflow in this version.

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

## Hardware pins

The firmware uses `INPUT_PULLUP` for the physical buttons, so each button should connect its Arduino pin to ground when pressed.

```text
Stepper pulse      D9
Stepper direction  D8
Stepper enable     D7
HX711 data         D4
HX711 clock        D5
Button 1, up       D10
Button 2, down     D11
Button 3           unused
E-stop             unused in this build
```

Stepper pulses, acceleration, and direction control are handled by AccelStepper. Mechanical constants, jog speed, calibration slope, and telemetry timing live in:

```text
firmware/arduino_uno/include/HardwareConfig.h
```

By default the stepper driver stays enabled when idle so the head holds position after button release. Set `HardwareConfig::Motion::DisableMotorWhenIdle` to `true` if you want the driver disabled whenever neither jog button is held.

## Flash firmware

Install PlatformIO, connect the Arduino Uno, then run:

```bash
cd ~/tensile-tester/src/firmware/arduino_uno
platformio run --target upload
```

For a specific upload port:

```bash
platformio run --target upload --upload-port /dev/ttyACM0
```

## Run the Pi web app

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

On Windows, the default serial port is `COM3`. On Linux/Raspberry Pi, the default is `/dev/ttyACM0`.

Optional environment variables:

```text
TENSILE_SERIAL_PORT=/dev/ttyACM0
TENSILE_SERIAL_BAUDRATE=115200
TENSILE_SERIAL_RECONNECT_S=2.0
```

## Serial telemetry

The Arduino streams:

```text
TEL,<seq>,<time_ms>,<state>,<raw_adc>,<force_n>,<step_rate_steps_s>,<position_mm>,<button_up>,<button_down>
```

The web app also understands `STATUS` frames with the same payload shape after the `STATUS` prefix.

## Safety scope

This is a bring-up build. It intentionally has no limit switches, software travel limits, overload shutoff, or emergency-stop behavior. Use only at low jog speed until those protections are added back.
