# Arduino Uno firmware

This firmware owns the machine-side real-time behavior:

- HX711 sampling
- force conversion and tare handling
- state machine enforcement
- PID force control mapped to signed step rate
- acceleration-limited stepper pulse generation
- physical button handling
- overload, abort, E-stop, and fault behavior
- ASCII serial acknowledgements, events, status, and telemetry

## Hardware pin map

| Signal | Arduino pin |
|---|---|
| TB6600 `PUL` | D9 |
| TB6600 `DIR` | D8 |
| TB6600 `ENA` | D7 |
| HX711 `DT` | D4 |
| HX711 `SCK` | D5 |
| Button 1 | D10 |
| Button 2 | D11 |
| Button 3 | D12 |
| E-stop input | D6 |

Buttons are configured with `INPUT_PULLUP`, so active press is logic low.

## Build

Install PlatformIO first if it is not already available:

```powershell
python -m pip install platformio
```

```powershell
cd firmware\arduino_uno
pio run
```

Upload:

```powershell
pio run --target upload
```

Monitor:

```powershell
pio device monitor
```

## Timing choices

- Control loop: `10 ms` period, nominal `100 Hz`
- Telemetry: `50 ms` period, nominal `20 Hz`
- Step pulse high time: `4 us`
- Step pulse scheduling: non-blocking `micros()` state machine

The control loop rate stays comfortably above the requested `80-100 Hz` range while leaving headroom on an Uno for serial parsing and HX711 polling. Telemetry at `20 Hz` is fast enough for the Pi live view without creating excessive serial or database load.

## State behavior

The firmware boots into `WAITING_FOR_PI_CONFIG`. It accepts only:

- `PING`
- `GET_STATUS`
- `LOAD_CONFIG`

until the Pi provides machine configuration.

After configuration, the normal test path is:

```text
IDLE -> ARMED -> RUNNING -> RETURNING_TO_ZERO -> IDLE
```

`TEST_COMPLETE` is emitted immediately before the firmware returns to `IDLE`. There is no persistent firmware `COMPLETE` state.
An armed run can also be cancelled before motion with `CANCEL_ARM`, which emits `ARM_CANCELLED` and returns the controller to `IDLE`.

## Safety behavior

- `ESTOPPED` disables `ENA` and requires acknowledgement after the physical input is released.
- `FAULT` is entered on overload.
- `ABORTED`, `ESTOPPED`, and `FAULT` all emit events so the Pi can finalize a partial run record.
- `SETUP` disables motor holding torque and blocks normal motion.
