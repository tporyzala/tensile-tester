from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

import serial


MessageHandler = Callable[[str], Awaitable[None]]


class MachineTransport(Protocol):
    async def start(self, on_line: MessageHandler) -> None: ...

    async def stop(self) -> None: ...

    async def send(self, line: str) -> None: ...


class SerialMachineTransport:
    def __init__(self, port: str, baudrate: int) -> None:
        self._port = port
        self._baudrate = baudrate
        self._serial: serial.Serial | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._on_line: MessageHandler | None = None
        self._stopped = asyncio.Event()

    async def start(self, on_line: MessageHandler) -> None:
        self._on_line = on_line
        self._serial = await asyncio.to_thread(
            serial.Serial,
            self._port,
            self._baudrate,
            timeout=0.25,
        )
        self._stopped.clear()
        self._reader_task = asyncio.create_task(self._reader_loop())

    async def stop(self) -> None:
        self._stopped.set()
        if self._reader_task is not None:
            await self._reader_task
            self._reader_task = None
        if self._serial is not None:
            await asyncio.to_thread(self._serial.close)
            self._serial = None

    async def send(self, line: str) -> None:
        if self._serial is None:
            raise RuntimeError("Serial transport is not connected.")
        payload = f"{line}\n".encode("ascii", errors="ignore")
        await asyncio.to_thread(self._serial.write, payload)

    async def _reader_loop(self) -> None:
        while not self._stopped.is_set():
            if self._serial is None:
                return
            raw_line = await asyncio.to_thread(self._serial.readline)
            if not raw_line:
                continue
            line = raw_line.decode("ascii", errors="ignore").strip()
            if line and self._on_line is not None:
                await self._on_line(line)


@dataclass(slots=True)
class _SimStep:
    step_type: str
    target_force_n: float
    ramp_rate_n_s: float
    timeout_s: float
    hold_duration_s: float


class SimulatedMachineTransport:
    def __init__(self) -> None:
        self._on_line: MessageHandler | None = None
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._seq = 0
        self._boot_time = time.monotonic()
        self._state = "WAITING_FOR_PI_CONFIG"
        self._configured = False
        self._method_id = 0
        self._steps: list[_SimStep] = []
        self._step_index = 0
        self._step_started_at = 0.0
        self._prior_state = "RUNNING"
        self._force_n = 0.0
        self._target_force_n = 0.0
        self._step_rate_steps_s = 0.0
        self._estimated_mm = 0.0
        self._return_rate_n_s = 50.0
        self._max_step_rate_steps_s = 2200.0
        self._overload_threshold_n = 1000.0

    async def start(self, on_line: MessageHandler) -> None:
        self._on_line = on_line
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            await self._task
            self._task = None

    async def send(self, line: str) -> None:
        parts = [part.strip() for part in line.strip().split(",")]
        command = parts[0].upper()
        if command == "PING":
            await self._emit("ACK,PING")
        elif command == "GET_STATUS":
            await self._emit_status()
        elif command == "LOAD_CONFIG":
            await self._load_config(parts)
        elif command == "LOAD_METHOD":
            await self._load_method(parts)
        elif command == "METHOD_STEP":
            await self._load_method_step(parts)
        elif command == "START":
            await self._start_run()
        elif command == "CANCEL_ARM":
            await self._cancel_arm()
        elif command == "PAUSE":
            await self._pause()
        elif command == "RESUME":
            await self._resume()
        elif command == "RETURN_ZERO":
            await self._return_zero()
        elif command == "ABORT":
            await self._abort()
        elif command == "ENTER_SETUP":
            await self._enter_setup()
        elif command == "EXIT_SETUP":
            await self._exit_setup()
        elif command == "ZERO_LOAD":
            await self._zero_load()
        elif command == "RESET_FAULT":
            await self._reset_fault()
        else:
            await self._emit("ERR,INVALID_COMMAND")

    async def _loop(self) -> None:
        telemetry_period_s = 0.05
        control_period_s = 0.02
        last_control = time.monotonic()
        last_telemetry = time.monotonic()
        while self._running:
            now = time.monotonic()
            if now - last_control >= control_period_s:
                self._tick(now - last_control)
                last_control = now
            if now - last_telemetry >= telemetry_period_s:
                await self._emit_telemetry()
                last_telemetry = now
            await asyncio.sleep(0.005)

    async def _load_config(self, parts: list[str]) -> None:
        if len(parts) < 15:
            await self._emit("ERR,INVALID_CONFIG")
            return
        self._max_step_rate_steps_s = max(float(parts[5]), 1.0)
        self._return_rate_n_s = max(float(parts[8]), 1.0)
        self._overload_threshold_n = max(float(parts[9]), 1.0)
        self._configured = True
        if self._state == "WAITING_FOR_PI_CONFIG":
            self._state = "IDLE"
        await self._emit("ACK,LOAD_CONFIG")
        await self._emit_status()

    async def _load_method(self, parts: list[str]) -> None:
        if not self._configured:
            await self._emit("ERR,CONFIG_REQUIRED")
            return
        if self._state != "IDLE" or len(parts) < 3:
            await self._emit("ERR,INVALID_STATE")
            return
        self._method_id = int(parts[1])
        expected_steps = int(parts[2])
        self._steps = []
        self._step_index = 0
        if expected_steps <= 0:
            await self._emit("ERR,INVALID_METHOD")
            return
        await self._emit("ACK,LOAD_METHOD")

    async def _load_method_step(self, parts: list[str]) -> None:
        if len(parts) < 7:
            await self._emit("ERR,INVALID_METHOD_STEP")
            return
        if self._state != "IDLE":
            await self._emit("ERR,INVALID_STATE")
            return
        step_type = parts[2].upper()
        target_force_n = float(parts[3])
        scalar_a = float(parts[4])
        scalar_b = float(parts[5])
        if step_type == "RAMP_TO_LOAD":
            step = _SimStep(step_type, target_force_n, scalar_a, scalar_b, 0.0)
        elif step_type == "HOLD_LOAD":
            step = _SimStep(step_type, target_force_n, 0.0, 0.0, scalar_a)
        else:
            await self._emit("ERR,INVALID_STEP_TYPE")
            return
        self._steps.append(step)
        await self._emit("ACK,METHOD_STEP")
        if len(self._steps) == int(parts[6]):
            self._state = "ARMED"
            await self._emit("EVENT,METHOD_ARMED")
            await self._emit_status()

    async def _start_run(self) -> None:
        if self._state != "ARMED" or not self._steps:
            await self._emit("ERR,INVALID_STATE")
            return
        self._state = "RUNNING"
        self._step_index = 0
        self._step_started_at = time.monotonic()
        await self._emit("ACK,START")
        await self._emit("EVENT,TEST_STARTED")
        await self._emit(f"EVENT,STEP_STARTED,{self._step_index + 1}")

    async def _cancel_arm(self) -> None:
        if self._state != "ARMED":
            await self._emit("ERR,INVALID_STATE")
            return
        self._steps = []
        self._step_index = 0
        self._target_force_n = 0.0
        self._step_rate_steps_s = 0.0
        self._state = "IDLE"
        await self._emit("ACK,CANCEL_ARM")
        await self._emit("EVENT,ARM_CANCELLED")
        await self._emit_status()

    async def _pause(self) -> None:
        if self._state not in {"RUNNING", "RETURNING_TO_ZERO"}:
            await self._emit("ERR,INVALID_STATE")
            return
        self._prior_state = self._state
        self._state = "PAUSED"
        self._step_rate_steps_s = 0.0
        await self._emit("ACK,PAUSE")
        await self._emit("EVENT,PAUSED")

    async def _resume(self) -> None:
        if self._state != "PAUSED":
            await self._emit("ERR,INVALID_STATE")
            return
        self._state = self._prior_state
        await self._emit("ACK,RESUME")
        await self._emit("EVENT,RESUMED")

    async def _return_zero(self) -> None:
        if self._state != "PAUSED":
            await self._emit("ERR,INVALID_STATE")
            return
        self._state = "RETURNING_TO_ZERO"
        await self._emit("ACK,RETURN_ZERO")
        await self._emit("EVENT,RETURN_ZERO_STARTED")

    async def _abort(self) -> None:
        if self._state not in {"ARMED", "RUNNING", "RETURNING_TO_ZERO", "PAUSED"}:
            await self._emit("ERR,INVALID_STATE")
            return
        self._state = "ABORTED"
        self._step_rate_steps_s = 0.0
        await self._emit("ACK,ABORT")
        await self._emit("EVENT,ABORTED")

    async def _enter_setup(self) -> None:
        if self._state != "IDLE":
            await self._emit("ERR,INVALID_STATE")
            return
        self._state = "SETUP"
        self._step_rate_steps_s = 0.0
        await self._emit("ACK,ENTER_SETUP")
        await self._emit_status()

    async def _exit_setup(self) -> None:
        if self._state != "SETUP":
            await self._emit("ERR,INVALID_STATE")
            return
        self._state = "IDLE"
        await self._emit("ACK,EXIT_SETUP")
        await self._emit_status()

    async def _zero_load(self) -> None:
        if self._state != "IDLE":
            await self._emit("ERR,INVALID_STATE")
            return
        self._force_n = 0.0
        self._target_force_n = 0.0
        await self._emit("ACK,ZERO_LOAD")
        await self._emit("EVENT,TARE_APPLIED")

    async def _reset_fault(self) -> None:
        if self._state not in {"ABORTED", "ESTOPPED", "FAULT"}:
            await self._emit("ERR,INVALID_STATE")
            return
        self._state = "IDLE"
        self._target_force_n = 0.0
        self._step_rate_steps_s = 0.0
        await self._emit("ACK,RESET_FAULT")
        await self._emit_status()

    def _tick(self, dt_s: float) -> None:
        if abs(self._force_n) > self._overload_threshold_n:
            self._state = "FAULT"
            self._step_rate_steps_s = 0.0
            self._target_force_n = self._force_n
            return

        if self._state == "RUNNING" and self._step_index < len(self._steps):
            self._tick_test_step(dt_s)
        elif self._state == "RETURNING_TO_ZERO":
            self._tick_return_to_zero(dt_s)
        else:
            self._step_rate_steps_s *= 0.7
            if abs(self._step_rate_steps_s) < 0.1:
                self._step_rate_steps_s = 0.0

        if self._state in {"RUNNING", "RETURNING_TO_ZERO"}:
            tracking_error = self._target_force_n - self._force_n
            delta_force = max(min(tracking_error * 0.25, 25.0 * dt_s), -25.0 * dt_s)
            self._force_n += delta_force
            normalized_rate = 0.0 if math.isclose(delta_force, 0.0, abs_tol=0.001) else delta_force / max(dt_s, 0.001)
            self._step_rate_steps_s = max(
                min(normalized_rate * 18.0, self._max_step_rate_steps_s),
                -self._max_step_rate_steps_s,
            )
            self._estimated_mm += self._step_rate_steps_s * dt_s / 960.15

    def _tick_test_step(self, dt_s: float) -> None:
        step = self._steps[self._step_index]
        elapsed = time.monotonic() - self._step_started_at
        if step.step_type == "RAMP_TO_LOAD":
            difference = step.target_force_n - self._target_force_n
            allowed_change = max(step.ramp_rate_n_s, 0.1) * dt_s
            if abs(difference) <= allowed_change:
                self._target_force_n = step.target_force_n
            else:
                self._target_force_n += math.copysign(allowed_change, difference)
            reached_target = math.isclose(self._target_force_n, step.target_force_n, abs_tol=0.05)
            stabilized = math.isclose(self._force_n, step.target_force_n, abs_tol=2.0)
            timed_out = step.timeout_s > 0 and elapsed >= step.timeout_s
            if (reached_target and stabilized) or timed_out:
                self._advance_step()
        elif step.step_type == "HOLD_LOAD":
            self._target_force_n = step.target_force_n
            if elapsed >= step.hold_duration_s:
                self._advance_step()

    def _advance_step(self) -> None:
        self._step_index += 1
        self._step_started_at = time.monotonic()
        if self._step_index >= len(self._steps):
            self._state = "RETURNING_TO_ZERO"
            asyncio.create_task(self._emit("EVENT,STEP_COMPLETED,FINAL"))
            asyncio.create_task(self._emit("EVENT,RETURN_ZERO_STARTED"))
            return
        asyncio.create_task(self._emit(f"EVENT,STEP_COMPLETED,{self._step_index}"))
        asyncio.create_task(self._emit(f"EVENT,STEP_STARTED,{self._step_index + 1}"))

    def _tick_return_to_zero(self, dt_s: float) -> None:
        allowed_change = max(self._return_rate_n_s, 0.1) * dt_s
        if abs(self._target_force_n) <= allowed_change:
            self._target_force_n = 0.0
        else:
            self._target_force_n -= math.copysign(allowed_change, self._target_force_n)

        if abs(self._target_force_n) <= 0.05 and abs(self._force_n) <= 1.5:
            self._state = "IDLE"
            self._target_force_n = 0.0
            self._step_rate_steps_s = 0.0
            asyncio.create_task(self._emit("EVENT,TEST_COMPLETE"))

    async def _emit_telemetry(self) -> None:
        self._seq += 1
        elapsed_ms = int((time.monotonic() - self._boot_time) * 1000)
        raw_adc = int(self._force_n * 1000)
        await self._emit(
            ",".join(
                [
                    "TEL",
                    str(self._seq),
                    str(elapsed_ms),
                    self._state,
                    str(raw_adc),
                    f"{self._force_n:.4f}",
                    f"{self._target_force_n:.4f}",
                    f"{self._step_rate_steps_s:.4f}",
                    f"{self._estimated_mm:.5f}",
                ]
            )
        )

    async def _emit_status(self) -> None:
        await self._emit(
            ",".join(
                [
                    "STATUS",
                    self._state,
                    "1" if self._configured else "0",
                    f"{self._force_n:.4f}",
                    f"{self._target_force_n:.4f}",
                    f"{self._step_rate_steps_s:.4f}",
                    f"{self._estimated_mm:.5f}",
                ]
            )
        )

    async def _emit(self, line: str) -> None:
        if self._on_line is not None:
            await self._on_line(line)
