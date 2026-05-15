from __future__ import annotations

import asyncio
import os
import platform
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

import serial
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles


STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_SERIAL_PORT = "COM3" if platform.system(
).lower().startswith("win") else "/dev/ttyACM0"
# Motion values sent to the Arduino are in motor steps, not millimeters.
MOTION_SPEED_MIN = 50.0
MOTION_SPEED_MAX = 4000.0
MOTION_SPEED_STEP = 50.0
MOTION_SPEED_DEFAULT = 500.0
MOTION_ACCELERATION_MIN = 100.0
MOTION_ACCELERATION_MAX = 10000.0
MOTION_ACCELERATION_STEP = 100.0
MOTION_ACCELERATION_DEFAULT = 4000.0
MOTION_ACK_TIMEOUT_S = 2.0
MOTION_COMMAND_ATTEMPTS = 3
MOTION_RETRY_DELAY_S = 0.1


@dataclass(slots=True)
class AppConfig:
    serial_port: str = os.getenv("TENSILE_SERIAL_PORT", DEFAULT_SERIAL_PORT)
    serial_baudrate: int = int(os.getenv("TENSILE_SERIAL_BAUDRATE", "115200"))
    reconnect_delay_s: float = float(
        os.getenv("TENSILE_SERIAL_RECONNECT_S", "2.0"))


@dataclass(slots=True)
class MachineSnapshot:
    # The latest machine reading that the web page polls and displays.
    connected: bool = False
    state: str = "DISCONNECTED"
    raw_adc: int = 0
    force_n: float = 0.0
    step_rate_steps_s: float = 0.0
    position_mm: float = 0.0
    button_up: bool = False
    button_down: bool = False
    jog_speed_steps_s: float = MOTION_SPEED_DEFAULT
    acceleration_steps_s2: float = MOTION_ACCELERATION_DEFAULT
    telemetry_seq: int = 0
    controller_time_ms: int = 0
    updated_at: float = 0.0
    last_message: str = "Waiting for Arduino."


@dataclass(slots=True)
class MachinePayload:
    state: str
    raw_adc: int
    force_n: float
    step_rate_steps_s: float
    position_mm: float
    button_up: bool
    button_down: bool
    jog_speed_steps_s: float | None
    acceleration_steps_s2: float | None


class MotionCommandError(RuntimeError):
    pass


def parse_machine_payload(payload: list[str]) -> MachinePayload:
    # Arduino machine payload:
    # state, raw_adc, force_n, step_rate, position_mm, button_up, button_down, jog_speed, acceleration
    if len(payload) < 7:
        raise ValueError("Machine payload is too short.")

    jog_speed_steps_s = float(payload[7]) if len(payload) >= 9 else None
    acceleration_steps_s2 = float(payload[8]) if len(payload) >= 9 else None

    return MachinePayload(
        state=payload[0],
        raw_adc=int(float(payload[1])),
        force_n=float(payload[2]),
        step_rate_steps_s=float(payload[3]),
        position_mm=float(payload[4]),
        button_up=payload[5] == "1",
        button_down=payload[6] == "1",
        jog_speed_steps_s=jog_speed_steps_s,
        acceleration_steps_s2=acceleration_steps_s2,
    )


class SerialMonitor:
    def __init__(self, config: AppConfig) -> None:
        # This object owns the Pi-to-Arduino serial link and the live UI state.
        self.config = config
        self.snapshot = MachineSnapshot()
        self._serial_log: list[str] = []
        self._serial: serial.Serial | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()
        self._write_lock = asyncio.Lock()
        self._motion_lock = asyncio.Lock()
        self._motion_pending_until = 0.0
        self._motion_expected: tuple[float, float] | None = None
        self._motion_ack_future: asyncio.Future[tuple[float,
                                                      float]] | None = None

    async def start(self) -> None:
        self._stopped.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopped.set()
        if self._task is not None:
            await self._task
            self._task = None
        await self._close_serial()

    def public_snapshot(self) -> dict[str, object]:
        data = asdict(self.snapshot)
        data["raw_serial"] = list(self._serial_log)
        return data

    async def send(self, line: str) -> None:
        if self._serial is None:
            raise RuntimeError("Serial transport is not connected.")
        # Commands are simple ASCII lines; the newline tells the Arduino to parse them.
        payload = f"{line}\n".encode("ascii", errors="ignore")
        connection = self._serial

        def write_and_flush() -> None:
            connection.write(payload)
            connection.flush()

        async with self._write_lock:
            self._log_serial("TX", line)
            await asyncio.to_thread(write_and_flush)

    async def set_motion_settings(self, speed_steps_s: float, acceleration_steps_s2: float) -> tuple[float, float]:
        async with self._motion_lock:
            # Treat each speed/acceleration update as a transaction that must be acknowledged.
            self.snapshot.jog_speed_steps_s = speed_steps_s
            self.snapshot.acceleration_steps_s2 = acceleration_steps_s2
            self.snapshot.last_message = (
                f"Sending motion settings: {speed_steps_s:.0f} steps/s, "
                f"{acceleration_steps_s2:.0f} steps/s^2."
            )
            self._motion_pending_until = time.monotonic() + 4.0
            if self._serial is None:
                self._log_serial(
                    "SYS", f"Not sent: {self._motion_command()}; Arduino is disconnected.")
                raise MotionCommandError(
                    "Arduino is disconnected; motion settings were not delivered.")

            return await self._send_motion_with_retries(speed_steps_s, acceleration_steps_s2)

    async def _send_motion_with_retries(
        self,
        speed_steps_s: float,
        acceleration_steps_s2: float,
    ) -> tuple[float, float]:
        last_error: Exception | None = None
        for attempt in range(1, MOTION_COMMAND_ATTEMPTS + 1):
            try:
                return await self._send_motion_attempt(speed_steps_s, acceleration_steps_s2)
            except TimeoutError as exc:
                last_error = exc
                message = "Arduino did not acknowledge SET_MOTION before the timeout."
            except MotionCommandError as exc:
                last_error = exc
                message = str(exc)

            if attempt < MOTION_COMMAND_ATTEMPTS:
                retry_message = (
                    f"{message} Retrying SET_MOTION "
                    f"({attempt + 1}/{MOTION_COMMAND_ATTEMPTS})."
                )
                self.snapshot.last_message = retry_message
                self._log_serial("SYS", retry_message)
                await asyncio.sleep(MOTION_RETRY_DELAY_S)

        final_message = f"Arduino did not confirm SET_MOTION after {MOTION_COMMAND_ATTEMPTS} attempts."
        self.snapshot.last_message = final_message
        self._log_serial("SYS", final_message)
        raise MotionCommandError(final_message) from last_error

    async def _send_motion_attempt(
        self,
        speed_steps_s: float,
        acceleration_steps_s2: float,
    ) -> tuple[float, float]:
        loop = asyncio.get_running_loop()
        ack_future: asyncio.Future[tuple[float, float]] = loop.create_future()
        # The future is completed when the serial reader sees ACK,SET_MOTION.
        self._motion_expected = (speed_steps_s, acceleration_steps_s2)
        self._motion_ack_future = ack_future
        try:
            await self.send(self._motion_command())
            return await asyncio.wait_for(ack_future, timeout=MOTION_ACK_TIMEOUT_S)
        finally:
            self._clear_motion_ack_state(ack_future)

    def _clear_motion_ack_state(self, ack_future: asyncio.Future[tuple[float, float]]) -> None:
        if self._motion_ack_future is ack_future:
            self._motion_ack_future = None
            self._motion_expected = None

    async def _run(self) -> None:
        while not self._stopped.is_set():
            if self._serial is None:
                await self._connect_or_wait()
                continue

            try:
                raw_line = await asyncio.to_thread(self._serial.readline)
            except Exception as exc:
                await self._mark_disconnected(exc)
                continue

            if not raw_line:
                continue

            line = raw_line.decode("ascii", errors="ignore").strip()
            if line:
                self._log_serial("RX", line)
                self._apply_line(line)

    async def _connect_or_wait(self) -> None:
        try:
            self._serial = await asyncio.to_thread(
                serial.Serial,
                self.config.serial_port,
                self.config.serial_baudrate,
                timeout=0.25,
            )
            self.snapshot.connected = True
            self.snapshot.state = "CONNECTED"
            self.snapshot.updated_at = time.time()
            self.snapshot.last_message = f"Arduino connected on {self.config.serial_port}."
            self._log_serial("SYS", self.snapshot.last_message)
            # After reconnect, ask for fresh telemetry and reapply the current slider settings.
            await self.send("GET_STATUS")
            await self.send(self._motion_command())
        except Exception as exc:
            self.snapshot.connected = False
            self.snapshot.state = "DISCONNECTED"
            self.snapshot.updated_at = time.time()
            self.snapshot.last_message = f"Arduino not detected on {self.config.serial_port}: {exc}"
            self._log_serial("SYS", self.snapshot.last_message)
            await asyncio.sleep(max(self.config.reconnect_delay_s, 0.25))

    async def _mark_disconnected(self, exc: Exception) -> None:
        await self._close_serial()
        self.snapshot.connected = False
        self.snapshot.state = "DISCONNECTED"
        self.snapshot.step_rate_steps_s = 0.0
        self.snapshot.button_up = False
        self.snapshot.button_down = False
        self.snapshot.updated_at = time.time()
        self.snapshot.last_message = f"Serial link lost: {exc}"
        self._log_serial("SYS", self.snapshot.last_message)

    async def _close_serial(self) -> None:
        if self._serial is None:
            return
        connection = self._serial
        self._serial = None
        await asyncio.to_thread(connection.close)

    def _apply_line(self, line: str) -> None:
        parts = [part.strip() for part in line.split(",")]
        kind = parts[0].upper() if parts else ""
        try:
            # TEL is periodic telemetry; STATUS is an on-demand snapshot; ACK/ERR answer commands.
            if kind == "TEL" and len(parts) >= 10:
                self.snapshot.telemetry_seq = int(parts[1])
                self.snapshot.controller_time_ms = int(parts[2])
                self._apply_payload(parts[3:])
            elif kind == "STATUS" and len(parts) >= 8:
                self._apply_payload(parts[1:])
            elif kind == "ACK" and len(parts) >= 2:
                self._apply_ack(parts)
            elif kind == "ERR" and len(parts) >= 2:
                self._apply_error(parts)
            else:
                self.snapshot.last_message = line
                self.snapshot.updated_at = time.time()
        except ValueError:
            self.snapshot.last_message = f"Could not parse serial line: {line}"
            self.snapshot.updated_at = time.time()

    def _apply_payload(self, payload: list[str]) -> None:
        # Payload order matches the Arduino emitTelemetry/emitStatus CSV fields.
        machine = parse_machine_payload(payload)
        self.snapshot.connected = True
        self.snapshot.state = machine.state
        self.snapshot.raw_adc = machine.raw_adc
        self.snapshot.force_n = machine.force_n
        self.snapshot.step_rate_steps_s = machine.step_rate_steps_s
        self.snapshot.position_mm = machine.position_mm
        self.snapshot.button_up = machine.button_up
        self.snapshot.button_down = machine.button_down
        if machine.jog_speed_steps_s is not None and machine.acceleration_steps_s2 is not None:
            if self._motion_update_should_apply(machine.jog_speed_steps_s, machine.acceleration_steps_s2):
                self.snapshot.jog_speed_steps_s = machine.jog_speed_steps_s
                self.snapshot.acceleration_steps_s2 = machine.acceleration_steps_s2
        self.snapshot.updated_at = time.time()
        self.snapshot.last_message = "Telemetry received."

    def _apply_ack(self, parts: list[str]) -> None:
        command = parts[1].upper()
        if command == "SET_MOTION" and len(parts) >= 4:
            try:
                self.snapshot.jog_speed_steps_s = float(parts[2])
                self.snapshot.acceleration_steps_s2 = float(parts[3])
                self._motion_pending_until = 0.0
                self.snapshot.last_message = (
                    f"Arduino accepted motion settings: {self.snapshot.jog_speed_steps_s:.0f} steps/s, "
                    f"{self.snapshot.acceleration_steps_s2:.0f} steps/s^2."
                )
                self._complete_motion_ack(
                    self.snapshot.jog_speed_steps_s,
                    self.snapshot.acceleration_steps_s2,
                )
            except ValueError:
                self.snapshot.last_message = ",".join(parts)
        else:
            self.snapshot.last_message = ",".join(parts)
        self.snapshot.updated_at = time.time()

    def _apply_error(self, parts: list[str]) -> None:
        message = ",".join(parts)
        if parts[1].upper() == "UNKNOWN_COMMAND" and time.monotonic() < self._motion_pending_until:
            token = parts[2] if len(parts) >= 3 else ""
            if token == "SET_MOTION":
                message = "Arduino rejected SET_MOTION. Reflash the latest firmware."
            elif token:
                message = f"Arduino received an unknown or partial command token: {token}."
            else:
                message = "Arduino received an unknown command while SET_MOTION was pending."
        if self._motion_ack_future is not None and not self._motion_ack_future.done():
            self._motion_ack_future.set_exception(MotionCommandError(message))
        self.snapshot.last_message = message
        self.snapshot.updated_at = time.time()

    def _log_serial(self, direction: str, line: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self._serial_log.append(f"{timestamp} {direction} {line}")

    def _motion_command(self) -> str:
        return f"SET_MOTION,{self.snapshot.jog_speed_steps_s:.2f},{self.snapshot.acceleration_steps_s2:.2f}"

    def _motion_update_should_apply(self, reported_speed: float, reported_acceleration: float) -> bool:
        # Ignore stale telemetry during a pending command so the slider does not jump backward.
        if time.monotonic() >= self._motion_pending_until:
            return True
        speed_matches = abs(
            reported_speed - self.snapshot.jog_speed_steps_s) < 0.5
        acceleration_matches = abs(
            reported_acceleration - self.snapshot.acceleration_steps_s2) < 0.5
        if speed_matches and acceleration_matches:
            self._motion_pending_until = 0.0
            return True
        return False

    def _complete_motion_ack(self, confirmed_speed: float, confirmed_acceleration: float) -> None:
        if self._motion_ack_future is None or self._motion_ack_future.done():
            return
        if self._motion_expected is None:
            self._motion_ack_future.set_result(
                (confirmed_speed, confirmed_acceleration))
            return
        expected_speed, expected_acceleration = self._motion_expected
        speed_matches = abs(confirmed_speed - expected_speed) < 0.5
        acceleration_matches = abs(
            confirmed_acceleration - expected_acceleration) < 0.5
        if speed_matches and acceleration_matches:
            self._motion_ack_future.set_result(
                (confirmed_speed, confirmed_acceleration))
        else:
            self._motion_ack_future.set_exception(
                MotionCommandError(
                    "Arduino acknowledged different motion settings: "
                    f"{confirmed_speed:.0f} steps/s, {confirmed_acceleration:.0f} steps/s^2."
                )
            )


config = AppConfig()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.monitor = SerialMonitor(config)
    await app.state.monitor.start()
    try:
        yield
    finally:
        await app.state.monitor.stop()


app = FastAPI(title="Tensile Tester", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/snapshot")
async def snapshot(request: Request) -> JSONResponse:
    return JSONResponse(request.app.state.monitor.public_snapshot())


@app.post("/api/motion")
async def set_motion(request: Request) -> JSONResponse:
    body = await request.json()
    speed_steps_s = clamp_float(
        float(body.get("speed_steps_s", MOTION_SPEED_DEFAULT)),
        MOTION_SPEED_MIN,
        MOTION_SPEED_MAX,
    )
    acceleration_steps_s2 = clamp_float(
        float(body.get("acceleration_steps_s2", MOTION_ACCELERATION_DEFAULT)),
        MOTION_ACCELERATION_MIN,
        MOTION_ACCELERATION_MAX,
    )
    try:
        confirmed_speed, confirmed_acceleration = await request.app.state.monitor.set_motion_settings(
            speed_steps_s,
            acceleration_steps_s2,
        )
    except MotionCommandError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    data = request.app.state.monitor.public_snapshot()
    data["motion_confirmed"] = True
    data["confirmed_speed_steps_s"] = confirmed_speed
    data["confirmed_acceleration_steps_s2"] = confirmed_acceleration
    return JSONResponse(data)


@app.get("/health")
async def health(request: Request) -> dict[str, object]:
    data = request.app.state.monitor.public_snapshot()
    return {"ok": True, "arduino_connected": data["connected"], "state": data["state"]}


def clamp_float(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))
