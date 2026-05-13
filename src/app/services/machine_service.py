from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.config import config
from app.database.models import FaultLog, Run
from app.database.session import SessionLocal
from app.serial.client import SerialMachineTransport, SimulatedMachineTransport
from app.serial.protocol import (
    AckFrame,
    ErrorFrame,
    EventFrame,
    StatusFrame,
    TelemetryFrame,
    parse_line,
)
from app.services.calibration_service import get_calibration
from app.services.method_service import get_method
from app.services.run_service import append_telemetry, mark_run_started, mark_run_terminal
from app.services.settings_service import get_settings, settings_to_protocol
from app.websocket.manager import WebSocketManager


@dataclass(slots=True)
class MachineSnapshot:
    state: str = "BOOT"
    configured: bool = False
    raw_adc: int = 0
    force_n: float = 0.0
    target_force_n: float = 0.0
    step_rate_steps_s: float = 0.0
    estimated_crosshead_mm: float = 0.0
    telemetry_seq: int = 0
    last_message: str = "Application starting."
    active_run_id: int | None = None


class MachineCoordinator:
    def __init__(self, websocket_manager: WebSocketManager) -> None:
        self.websocket_manager = websocket_manager
        self.snapshot = MachineSnapshot()
        self.active_run_id: int | None = None
        self._lock = asyncio.Lock()
        if config.machine_transport == "serial":
            self.transport = SerialMachineTransport(config.serial_port, config.serial_baudrate)
        else:
            self.transport = SimulatedMachineTransport()

    async def start(self) -> None:
        await self.transport.start(self.handle_line)
        await self.reload_config()
        await self.send("GET_STATUS")

    async def stop(self) -> None:
        await self.transport.stop()

    async def send(self, command: str) -> None:
        await self.transport.send(command)

    async def reload_config(self) -> None:
        with SessionLocal() as db:
            settings = get_settings(db)
            calibration = get_calibration(db)
            line = settings_to_protocol(settings, calibration.slope, calibration.intercept)
        await self.send(line)

    async def arm_run(self, db: Session, run: Run) -> None:
        method = get_method(db, run.method_id)
        if method is None:
            raise ValueError("Method not found.")
        async with self._lock:
            await self.send(f"LOAD_METHOD,{method.id},{len(method.steps)}")
            for step in method.steps:
                if step.step_type == "RAMP_TO_LOAD":
                    line = ",".join(
                        [
                            "METHOD_STEP",
                            str(step.position),
                            step.step_type,
                            f"{step.target_force_n:.6f}",
                            f"{(step.rate_n_per_s or 0.0):.6f}",
                            f"{(step.timeout_s or 0.0):.6f}",
                            str(len(method.steps)),
                        ]
                    )
                else:
                    line = ",".join(
                        [
                            "METHOD_STEP",
                            str(step.position),
                            step.step_type,
                            f"{step.target_force_n:.6f}",
                            f"{(step.duration_s or 0.0):.6f}",
                            "0.000000",
                            str(len(method.steps)),
                        ]
                    )
                await self.send(line)
            self.active_run_id = run.id
            self.snapshot.active_run_id = run.id
            self.snapshot.last_message = f"Run {run.run_name} armed."
        await self.broadcast_snapshot()

    async def start_run(self) -> None:
        await self.send("START")

    async def cancel_armed_run(self) -> None:
        await self.send("CANCEL_ARM")

    async def pause(self) -> None:
        await self.send("PAUSE")

    async def resume(self) -> None:
        await self.send("RESUME")

    async def abort(self) -> None:
        await self.send("ABORT")

    async def return_zero(self) -> None:
        await self.send("RETURN_ZERO")

    async def enter_setup(self) -> None:
        await self.send("ENTER_SETUP")

    async def exit_setup(self) -> None:
        await self.send("EXIT_SETUP")

    async def zero_load(self) -> None:
        await self.send("ZERO_LOAD")

    async def reset_fault(self) -> None:
        await self.send("RESET_FAULT")

    async def handle_line(self, line: str) -> None:
        frame = parse_line(line)
        self.snapshot.last_message = line
        if isinstance(frame, TelemetryFrame):
            await self._handle_telemetry(frame)
        elif isinstance(frame, EventFrame):
            await self._handle_event(frame)
        elif isinstance(frame, StatusFrame):
            self._apply_status(frame)
        elif isinstance(frame, AckFrame):
            pass
        elif isinstance(frame, ErrorFrame):
            await self._record_fault_like_message(frame.code, ",".join(frame.details) or "Protocol error.")
        await self.broadcast_snapshot()

    async def broadcast_snapshot(self) -> None:
        await self.websocket_manager.broadcast({"type": "snapshot", "payload": asdict(self.snapshot)})

    def public_snapshot(self) -> dict[str, Any]:
        return asdict(self.snapshot)

    async def _handle_telemetry(self, frame: TelemetryFrame) -> None:
        self.snapshot.state = frame.state
        self.snapshot.telemetry_seq = frame.seq
        self.snapshot.raw_adc = frame.raw_adc
        self.snapshot.force_n = frame.force_n
        self.snapshot.target_force_n = frame.target_force_n
        self.snapshot.step_rate_steps_s = frame.step_rate_steps_s
        self.snapshot.estimated_crosshead_mm = frame.estimated_mm
        if self.active_run_id is not None:
            with SessionLocal() as db:
                append_telemetry(
                    db,
                    self.active_run_id,
                    frame.seq,
                    frame.time_ms,
                    frame.state,
                    frame.raw_adc,
                    frame.force_n,
                    frame.target_force_n,
                    frame.step_rate_steps_s,
                    frame.estimated_mm,
                )
        await self.websocket_manager.broadcast(
            {
                "type": "telemetry",
                "payload": {
                    "seq": frame.seq,
                    "time_ms": frame.time_ms,
                    "state": frame.state,
                    "raw_adc": frame.raw_adc,
                    "force_n": frame.force_n,
                    "target_force_n": frame.target_force_n,
                    "step_rate_steps_s": frame.step_rate_steps_s,
                    "estimated_crosshead_mm": frame.estimated_mm,
                },
            }
        )

    async def _handle_event(self, frame: EventFrame) -> None:
        name = frame.name.upper()
        if name == "TEST_STARTED" and self.active_run_id is not None:
            with SessionLocal() as db:
                mark_run_started(db, self.active_run_id)
        elif name == "TEST_COMPLETE" and self.active_run_id is not None:
            with SessionLocal() as db:
                mark_run_terminal(db, self.active_run_id, "COMPLETE", "Automatic return to zero finished.")
            self.active_run_id = None
            self.snapshot.active_run_id = None
        elif name == "ABORTED" and self.active_run_id is not None:
            with SessionLocal() as db:
                mark_run_terminal(db, self.active_run_id, "ABORTED", "Aborted by operator or controller.")
            self.active_run_id = None
            self.snapshot.active_run_id = None
        elif name == "ARM_CANCELLED" and self.active_run_id is not None:
            with SessionLocal() as db:
                mark_run_terminal(db, self.active_run_id, "CANCELLED", "Armed run cancelled before start.")
            self.active_run_id = None
            self.snapshot.active_run_id = None
        elif name == "ESTOPPED" and self.active_run_id is not None:
            with SessionLocal() as db:
                mark_run_terminal(db, self.active_run_id, "ESTOPPED", "Physical E-stop.")
            self.active_run_id = None
            self.snapshot.active_run_id = None
        elif name == "FAULT":
            detail = ",".join(frame.details) if frame.details else "Controller fault."
            await self._record_fault_like_message("FAULT", detail)

    def _apply_status(self, frame: StatusFrame) -> None:
        self.snapshot.state = frame.state
        self.snapshot.configured = frame.configured
        self.snapshot.force_n = frame.force_n
        self.snapshot.target_force_n = frame.target_force_n
        self.snapshot.step_rate_steps_s = frame.step_rate_steps_s
        self.snapshot.estimated_crosshead_mm = frame.estimated_mm

    async def _record_fault_like_message(self, code: str, detail: str) -> None:
        run_id = self.active_run_id
        with SessionLocal() as db:
            db.add(FaultLog(run_id=run_id, state=self.snapshot.state, code=code, message=detail))
            db.commit()
