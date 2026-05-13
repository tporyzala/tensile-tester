# Tensile tester software stack

This `src/` workspace contains the full software and firmware MVP for a personal tensile/compression tester:

- FastAPI + SQLAlchemy + SQLite Raspberry Pi application
- Jinja2 + HTMX pages
- Plotly live telemetry view
- WebSocket telemetry fanout
- CSV export for immutable saved telemetry
- Arduino Uno / PlatformIO firmware
- ASCII serial command and telemetry protocol

## Layout

```text
src/
|-- app/
|   |-- database/
|   |-- models/
|   |-- routes/
|   |-- serial/
|   |-- services/
|   |-- static/
|   |-- templates/
|   |-- websocket/
|   `-- main.py
|-- firmware/
|   `-- arduino_uno/
|-- scripts/
|-- requirements.txt
`-- README.md
```

## Backend responsibilities

The Pi application owns:

- method authoring and reuse
- sample metadata capture
- run creation and lifecycle tracking
- settings and calibration persistence
- configuration delivery to the Uno
- telemetry persistence
- WebSocket fanout and live plotting
- CSV export

The Uno remains the source of truth for real-time state and motion behavior.

## Local setup

```powershell
cd C:\Users\tomek\Desktop\tensile-tester\src
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python scripts\seed_demo_data.py
uvicorn app.main:app --reload
```

Open:

```text
http://127.0.0.1:8000
```

The default app transport is a simulator so the full operator UI can be exercised without connected hardware.

To use physical serial hardware:

```powershell
$env:TENSILE_MACHINE_TRANSPORT = "serial"
$env:TENSILE_SERIAL_PORT = "COM3"
$env:TENSILE_SERIAL_BAUDRATE = "115200"
uvicorn app.main:app --reload
```

## Raspberry Pi deployment

Use this sequence when the Pi is the machine host and your PC is only used for development.

### 1. Prepare the Raspberry Pi

Install Raspberry Pi OS, create your user, and enable SSH if you plan to administer the Pi remotely.

On the Pi:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip
```

Raspberry Pi OS expects Python packages to be installed in a virtual environment, so the app setup below intentionally uses `.venv`.

### 2. Pull the repository onto the Pi

Fresh clone:

```bash
cd ~
git clone <your-repo-url> tensile-tester
cd ~/tensile-tester/src
```

Later updates:

```bash
cd ~/tensile-tester
git pull
cd src
```

### 3. Create the Pi Python environment

```bash
cd ~/tensile-tester/src
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python scripts/seed_demo_data.py
```

The seed command is safe to re-run. It only ensures the demo method exists.

### 4. Flash the Arduino firmware

Flash the Arduino Uno once before trying to run the hardware-backed web app.

You can do this:

- from your PC before moving the Uno to the Pi, or
- directly from the Pi while the Uno is connected over USB.

From the Pi:

```bash
cd ~/tensile-tester/src
. .venv/bin/activate
python -m pip install platformio
cd firmware/arduino_uno
../../.venv/bin/python -m platformio run --target upload
```

If PlatformIO cannot auto-detect the Uno, provide the upload port explicitly:

```bash
../../.venv/bin/python -m platformio run --target upload --upload-port /dev/ttyACM0
```

### 5. Connect the Arduino Uno to the Pi

Connect the Uno to the Raspberry Pi with a USB data cable.

List likely serial devices:

```bash
ls /dev/ttyACM* /dev/ttyUSB* 2>/dev/null
```

You can also ask `pyserial` to enumerate serial ports:

```bash
cd ~/tensile-tester/src
. .venv/bin/activate
python -m serial.tools.list_ports -v
```

For an Uno over USB, `/dev/ttyACM0` is a common device path. If your Pi reports a different path, use the reported path everywhere below.

If opening the serial device fails with a permission error, add your user to the `dialout` group, then log out and back in:

```bash
sudo usermod -aG dialout "$USER"
```

### 6. Verify Pi-to-Arduino serial communication before starting the app

Run the included handshake probe:

```bash
cd ~/tensile-tester/src
. .venv/bin/activate
python scripts/check_arduino_link.py --port /dev/ttyACM0
```

Expected success output:

```text
Serial handshake passed.
Controller status: STATUS,...
```

The probe sends:

- `PING`
- `GET_STATUS`

and expects:

- `ACK,PING`
- `STATUS,...`

Before the full Pi app starts, the firmware will normally report `WAITING_FOR_PI_CONFIG` because the controller boots safe and waits for configuration.

### 7. Start the Pi web app in real serial mode

```bash
cd ~/tensile-tester/src
. .venv/bin/activate
export TENSILE_MACHINE_TRANSPORT=serial
export TENSILE_SERIAL_PORT=/dev/ttyACM0
export TENSILE_SERIAL_BAUDRATE=115200
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

On Linux, the app defaults to `/dev/ttyACM0` if `TENSILE_SERIAL_PORT` is not set, but setting it explicitly is clearer and safer during bring-up.

Open from another machine on the same network:

```text
http://<raspberry-pi-ip>:8000
```

### 8. Confirm the app configured the Arduino

From another Pi terminal:

```bash
curl http://127.0.0.1:8000/api/status
```

Healthy startup should show:

- `"configured": true`
- `"state": "IDLE"`

That means:

1. the Pi opened the serial device,
2. the app sent `LOAD_CONFIG`,
3. the Arduino accepted it,
4. the controller transitioned out of `WAITING_FOR_PI_CONFIG`.

### 9. If communication is not established

Use this checklist:

| Symptom | Likely cause | Check |
|---|---|---|
| No `/dev/ttyACM*` or `/dev/ttyUSB*` device | USB cable, power, or Uno not enumerated | Try another USB data cable and rerun `ls /dev/ttyACM* /dev/ttyUSB*` |
| `Permission denied` opening the port | Pi user lacks serial-device access | Add the user to `dialout`, then log out and back in |
| `check_arduino_link.py` receives no reply | Wrong port, wrong firmware, or the Uno is not actually running | Recheck port, reflash firmware, rerun the probe |
| App startup leaves state at `WAITING_FOR_PI_CONFIG` | App did not complete serial config handoff | Confirm `TENSILE_MACHINE_TRANSPORT=serial` and the port path are correct |
| App logs serial open failure | Wrong port or port already in use | Close serial monitors, then restart the app |

## Firmware build

Install PlatformIO once in the same virtualenv or use an existing PlatformIO installation:

```powershell
.\.venv\Scripts\python.exe -m pip install platformio
cd firmware\arduino_uno
..\..\.venv\Scripts\python.exe -m platformio run
```

The same project can be uploaded with PlatformIO after selecting the connected Uno serial port.

## Pages

- `/dashboard` - run orchestration, zeroing, state-aware machine actions, live force plot
- `/methods` - create methods and ordered `RAMP_TO_LOAD` / `HOLD_LOAD` steps
- `/results` - saved run listing and CSV export
- `/calibration` - slope/intercept update while IDLE
- `/admin` - PID, motion, overload, inversion, return-to-zero settings, and setup mode access while IDLE

## Force calibration and tare

The controller evaluates:

```text
force_N = slope * raw_adc + intercept - tare_offset
```

Persisted:

- slope
- intercept

Temporary:

- tare offset from `ZERO_LOAD`

The tare offset is held for the active run path and cleared when the run completes or the terminal state is acknowledged/reset.

## Motion conversion

With the stated mechanical stack:

- motor steps per rev: `200`
- gearbox ratio: `19.203:1`
- screw pitch: `4 mm/rev`
- configurable microstepping

The firmware estimates crosshead travel from signed emitted microsteps:

```text
estimated_crosshead_mm =
  emitted_microsteps / ((200 * 19.203 * microstepping) / 4)
```

This is an estimate only. There is no closed-loop displacement sensing in the MVP.

## Timing choices

Firmware:

- control loop: `100 Hz`
- telemetry: `20 Hz`
- pulse scheduler: non-blocking `micros()` timing

UI:

- WebSocket receives every telemetry frame
- Plotly keeps a rolling `600` points by default

The firmware timing keeps control deterministic enough for an MVP, while the Pi receives a lower-rate telemetry stream that is practical to persist and plot continuously.

## Serial protocol

### Pi to Arduino

```text
PING
GET_STATUS
LOAD_CONFIG,p,i,d,deadband,max_rate,max_accel,jog_speed,return_zero_rate,overload,microstepping,invert_motor,invert_load,slope,intercept
LOAD_METHOD,method_id,step_count
METHOD_STEP,position,RAMP_TO_LOAD,target_force_N,rate_N_per_s,timeout_s,total_steps
METHOD_STEP,position,HOLD_LOAD,target_force_N,duration_s,0,total_steps
START
CANCEL_ARM
PAUSE
RESUME
RETURN_ZERO
ABORT
ENTER_SETUP
EXIT_SETUP
ZERO_LOAD
RESET_FAULT
```

### Arduino to Pi

```text
ACK,START
ERR,INVALID_STATE
STATUS,state,configured,force_N,target_force_N,step_rate_steps_s,estimated_mm
TEL,seq,time_ms,state,raw_adc,force_N,target_force_N,step_rate_steps_s,estimated_mm
EVENT,TEST_STARTED
EVENT,ARM_CANCELLED
EVENT,STEP_STARTED,1
EVENT,STEP_COMPLETED,1
EVENT,RETURN_ZERO_STARTED
EVENT,TEST_COMPLETE
EVENT,ABORTED
EVENT,ESTOPPED
EVENT,FAULT,OVERLOAD
```

Telemetry contains both raw ADC and calibrated Newtons, along with the estimated crosshead position.

## Database coverage

SQLite stores:

- test methods
- ordered test steps
- sample metadata
- runs
- immutable telemetry points
- calibration
- admin settings
- fault logs

Telemetry export is currently CSV-per-run from the Results page.

## Hardware-facing caveats

This MVP intentionally does not attempt:

- certification-grade safety
- end-stop or limit-switch handling
- encoder or true displacement feedback
- closed-loop motor position verification
- live tuning during active runs
- arbitrary test-step types beyond ramp and hold

Those are natural next layers once the physical machine is commissioned.
