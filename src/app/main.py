from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import platform
import re
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import TypeVar

import serial
import xlsxwriter
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from app.calibration import CalibrationSample, fit_load_cell_calibration


SetupCommandResult = TypeVar("SetupCommandResult")


PROJECT_DIR = Path(__file__).resolve().parents[1]
STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_METHOD_DIR = PROJECT_DIR / "data" / "test-methods"
DEFAULT_SAMPLE_SET_DIR = PROJECT_DIR / "data" / "test-sets"
DEFAULT_SERIAL_PORT = "COM3" if platform.system(
).lower().startswith("win") else "/dev/ttyACM0"

# Motion values sent to the Arduino are in motor steps, not millimeters.
MOTION_STEPS_PER_REV = 200.0
MOTION_GEARBOX_RATIO = 19.203
MOTION_SCREW_PITCH_MM_PER_REV = 4.0
MOTION_MICROSTEPPING = 8.0
MOTION_SPEED_MIN = 50.0
MOTION_SPEED_MAX = 4000.0
MOTION_SPEED_DEFAULT = 4000.0
MOTION_ACCELERATION_MIN = 100.0
MOTION_ACCELERATION_MAX = 10000.0
MOTION_ACCELERATION_DEFAULT = 10000.0
MOTION_ACK_TIMEOUT_S = 2.0
MOTION_COMMAND_ATTEMPTS = 3
MOTION_RETRY_DELAY_S = 0.1
TARE_ACK_TIMEOUT_S = 7.0
TARE_COMMAND_ATTEMPTS = 3
TARE_RETRY_DELAY_S = 0.1
DISPLACEMENT_ZERO_ACK_TIMEOUT_S = 2.0
DISPLACEMENT_ZERO_COMMAND_ATTEMPTS = 3
DISPLACEMENT_ZERO_RETRY_DELAY_S = 0.1

TEST_ACK_TIMEOUT_S = 2.0
TEST_COMMAND_ATTEMPTS = 3
TEST_RETRY_DELAY_S = 0.1
TEST_HEARTBEAT_PERIOD_S = 0.5
TEST_HOLD_DURATION_MAX_S = 24.0 * 60.0 * 60.0
TEST_SPEED_MIN = 50.0
TEST_SPEED_MAX = 4000.0
TEST_SPEED_DEFAULT = 2000.0
TEST_TARGET_TYPES = {"FORCE", "DISPLACEMENT"}
TEST_RATE_TYPES = {"FORCE", "DISPLACEMENT"}
SERIAL_LOG_DEFAULT_MAX_LINES = 500
SAMPLE_ID_MAX_LENGTH = 64
SAMPLE_NOTES_MAX_LENGTH = 200
SAMPLE_SET_NAME_MAX_LENGTH = 80
SAMPLE_WORKBOOK_FIELDNAMES = [
    "wall_time_s",
    "controller_time_ms",
    "run_id",
    "frame_mode",
    "step_index",
    "phase",
    "fault_reason",
    "control_mode",
    "setpoint_force_n",
    "setpoint_displacement_mm",
    "force_n",
    "position_mm",
    "step_rate_steps_s",
]
SAMPLE_WORKBOOK_NUMERIC_FIELDS = {
    "wall_time_s",
    "controller_time_ms",
    "run_id",
    "step_index",
    "setpoint_force_n",
    "setpoint_displacement_mm",
    "force_n",
    "position_mm",
    "step_rate_steps_s",
}
SUMMARY_WORKBOOK_FIELDNAMES = [
    "sample_index",
    "sample_id",
    "notes",
    "included",
    "status",
    "method_name",
    "point_count",
    "peak_force_n",
    "peak_force_position_mm",
    "final_force_n",
    "final_position_mm",
]
SUMMARY_WORKBOOK_NUMERIC_FIELDS = {
    "sample_index",
    "point_count",
    "peak_force_n",
    "peak_force_position_mm",
    "final_force_n",
    "final_position_mm",
}
WORKBOOK_FORCE_DISPLACEMENT_MAX_POINTS = 2000
METHOD_NAME_MAX_LENGTH = 80
RETURN_ZERO_MODES = {"LOAD", "DISPLACEMENT"}
RETURN_ZERO_LOAD_DEFAULT_RATE_N_S = 10.0
STEPS_PER_MM = (
    MOTION_STEPS_PER_REV *
    MOTION_GEARBOX_RATIO *
    MOTION_MICROSTEPPING /
    MOTION_SCREW_PITCH_MM_PER_REV
)
RETURN_ZERO_DISPLACEMENT_DEFAULT_RATE_MM_S = TEST_SPEED_DEFAULT / STEPS_PER_MM
RETURN_ZERO_HOLD_DURATION_S = 1.0
RELATIVE_MOVE_AMOUNTS_MM = frozenset({-100.0, -10.0, -1.0, 1.0, 10.0, 100.0})
RELATIVE_MOVE_STATIONARY_STEP_RATE_MAX_STEPS_S = 0.5
RUN_KIND_NONE = "NONE"
RUN_KIND_SPECIMEN = "SPECIMEN"
RUN_KIND_INITIALIZATION = "INITIALIZATION"
RUN_KIND_RETURN_ZERO = "RETURN_ZERO"
RUN_KIND_RELATIVE_MOVE = "RELATIVE_MOVE"
INITIALIZATION_MODE_NONE = "NONE"
INITIALIZATION_MODE_PRELOAD_UNLOAD_ZERO = "PRELOAD_UNLOAD_ZERO_DISPLACEMENT"
INITIALIZATION_MODES = {
    INITIALIZATION_MODE_NONE,
    INITIALIZATION_MODE_PRELOAD_UNLOAD_ZERO,
}
INITIALIZATION_PRELOAD_FORCE_DEFAULT_N = 10.0
INITIALIZATION_UNLOAD_FORCE_DEFAULT_N = 0.0
INITIALIZATION_RATE_DEFAULT_MM_S = 0.02
INITIALIZATION_MAX_TRAVEL_DEFAULT_MM = 2.0
INITIALIZATION_HOLD_DURATION_S = 1.0
CALIBRATION_SAMPLE_DURATION_S = 12.0
CALIBRATION_SAMPLE_POLL_S = 0.02
CALIBRATION_SAMPLE_MIN_COUNT = 20
CALIBRATION_STATIONARY_STEP_RATE_MAX_STEPS_S = 0.5


@dataclass(slots=True)
class AppConfig:
    serial_port: str = os.getenv("TENSILE_SERIAL_PORT", DEFAULT_SERIAL_PORT)
    serial_baudrate: int = int(os.getenv("TENSILE_SERIAL_BAUDRATE", "115200"))
    reconnect_delay_s: float = float(
        os.getenv("TENSILE_SERIAL_RECONNECT_S", "2.0"))
    serial_log_max_lines: int = int(
        os.getenv("TENSILE_SERIAL_LOG_MAX_LINES", str(SERIAL_LOG_DEFAULT_MAX_LINES)))
    method_dir: Path = Path(os.getenv("TENSILE_METHOD_DIR", str(DEFAULT_METHOD_DIR)))
    sample_set_dir: Path = Path(
        os.getenv("TENSILE_SAMPLE_SET_DIR", str(DEFAULT_SAMPLE_SET_DIR)))


@dataclass(slots=True)
class MachineSnapshot:
    # The latest machine reading that the web page polls and displays.
    connected: bool = False
    state: str = "DISCONNECTED"
    frame_mode: str = "DISCONNECTED"
    fault_reason: str = "NONE"
    raw_adc: int = 0
    force_n: float = 0.0
    step_rate_steps_s: float = 0.0
    position_mm: float = 0.0
    button_up: bool = False
    button_down: bool = False
    button_stop: bool = False
    jog_speed_steps_s: float = MOTION_SPEED_DEFAULT
    test_max_step_rate_steps_s: float = TEST_SPEED_DEFAULT
    acceleration_steps_s2: float = MOTION_ACCELERATION_DEFAULT
    telemetry_seq: int = 0
    controller_time_ms: int = 0
    test_run_id: int = 0
    test_phase: str = "NONE"
    test_step_index: int = 0
    test_step_count: int = 0
    test_control_mode: str = "NONE"
    test_setpoint_force_n: float = 0.0
    test_setpoint_displacement_mm: float = 0.0
    test_elapsed_ms: int = 0
    updated_at: float = 0.0
    last_message: str = "Waiting for Arduino."


@dataclass(slots=True)
class MachinePayload:
    frame_mode: str
    test_phase: str
    fault_reason: str
    raw_adc: int
    force_n: float
    step_rate_steps_s: float
    position_mm: float
    button_up: bool
    button_down: bool
    jog_speed_steps_s: float | None
    acceleration_steps_s2: float | None
    button_stop: bool = False
    test_run_id: int = 0
    test_max_step_rate_steps_s: float | None = None
    test_step_index: int = 0
    test_step_count: int = 0
    test_control_mode: str = "NONE"
    test_setpoint_force_n: float = 0.0
    test_setpoint_displacement_mm: float = 0.0
    test_elapsed_ms: int = 0


@dataclass(slots=True)
class TestStep:
    target_type: str
    target_value: float
    rate_type: str
    rate_value_per_s: float
    hold_duration_s: float


@dataclass(slots=True)
class TestInitialization:
    mode: str = INITIALIZATION_MODE_NONE
    preload_force_n: float = INITIALIZATION_PRELOAD_FORCE_DEFAULT_N
    unload_force_n: float = INITIALIZATION_UNLOAD_FORCE_DEFAULT_N
    rate_mm_s: float = INITIALIZATION_RATE_DEFAULT_MM_S
    max_travel_mm: float = INITIALIZATION_MAX_TRAVEL_DEFAULT_MM


@dataclass(slots=True)
class TestMethod:
    id: str
    name: str
    steps: list[TestStep]
    motion: dict[str, float] = field(default_factory=dict)
    initialization: TestInitialization = field(default_factory=TestInitialization)
    schema_version: int = 1
    created_at: str = ""
    updated_at: str = ""


@dataclass(slots=True)
class TestRunState:
    run_id: int = 0
    status: str = "IDLE"
    phase: str = "NONE"
    step_index: int = 0
    step_count: int = 0
    control_mode: str = "NONE"
    setpoint_force_n: float = 0.0
    setpoint_displacement_mm: float = 0.0
    message: str = "No automated test running."
    started_at: float = 0.0
    finished_at: float = 0.0


@dataclass(slots=True)
class TestSampleMetadata:
    sample_id: str
    notes: str = ""


@dataclass(slots=True)
class TestSampleRecord:
    index: int
    run_id: int
    sample_id: str
    notes: str
    status: str
    included: bool
    started_at: float
    finished_at: float
    point_count: int
    peak_force_n: float
    peak_force_position_mm: float
    final_force_n: float
    final_position_mm: float
    method_id: str = ""
    method_name: str = ""
    method_hash: str = ""
    method_snapshot: dict[str, object] = field(default_factory=dict)
    samples: list[dict[str, object]] = field(default_factory=list)


@dataclass(slots=True)
class TestSampleSet:
    id: str
    name: str
    samples: list[TestSampleRecord]
    method_snapshot: dict[str, object] = field(default_factory=dict)
    schema_version: int = 1
    created_at: str = ""
    updated_at: str = ""


@dataclass(slots=True)
class ReturnZeroRequest:
    mode: str
    rate_value_per_s: float


class MachineCommandError(RuntimeError):
    pass


class MotionCommandError(MachineCommandError):
    pass


class TareCommandError(MachineCommandError):
    pass


class DisplacementZeroCommandError(MachineCommandError):
    pass


class TestCommandError(MachineCommandError):
    pass


class CalibrationCommandError(MachineCommandError):
    pass


class TestMethodStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def list_methods(self) -> list[dict[str, object]]:
        self.directory.mkdir(parents=True, exist_ok=True)
        methods: list[dict[str, object]] = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                method = self._load_path(path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            methods.append(self._summary(method))
        return sorted(
            methods,
            key=lambda item: str(item.get("updated_at") or ""),
            reverse=True,
        )

    def get_method(self, method_id: str) -> dict[str, object]:
        return method_to_public(self._load_path(self._path_for_id(method_id)))

    def save_method(self, payload: dict[str, object]) -> dict[str, object]:
        parsed = parse_test_method(payload)
        now = utc_timestamp()
        method_id = method_id_from_name(parsed.name)
        path = self._path_for_id(method_id)
        created_at = now
        if path.exists():
            try:
                existing = self._load_path(path)
                created_at = existing.created_at or now
            except (OSError, ValueError, json.JSONDecodeError):
                created_at = now

        method = TestMethod(
            id=method_id,
            name=parsed.name,
            steps=parsed.steps,
            motion=parsed.motion,
            initialization=parsed.initialization,
            schema_version=1,
            created_at=created_at,
            updated_at=now,
        )
        self.directory.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"{json.dumps(method_to_public(method), indent=2)}\n",
            encoding="utf-8",
        )
        return method_to_public(method)

    def _path_for_id(self, method_id: str) -> Path:
        parsed = parse_method_id(method_id)
        return self.directory / f"{parsed}.json"

    def _load_path(self, path: Path) -> TestMethod:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Method file must contain an object.")
        method = parse_test_method(data)
        return TestMethod(
            id=parse_method_id(data.get("id") or method_id_from_name(method.name)),
            name=method.name,
            steps=method.steps,
            motion=method.motion,
            initialization=method.initialization,
            schema_version=1,
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
        )

    def _summary(self, method: TestMethod) -> dict[str, object]:
        return {
            "id": method.id,
            "name": method.name,
            "step_count": len(method.steps),
            "created_at": method.created_at,
            "updated_at": method.updated_at,
            "hash": method_hash(method_to_public(method)),
        }


class TestSampleSetStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def list_sample_sets(self) -> list[dict[str, object]]:
        self.directory.mkdir(parents=True, exist_ok=True)
        sample_sets: list[dict[str, object]] = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                sample_set = self._load_path(path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            sample_sets.append(self._summary(sample_set))
        return sorted(
            sample_sets,
            key=lambda item: str(item.get("updated_at") or ""),
            reverse=True,
        )

    def get_sample_set(self, sample_set_id: str) -> TestSampleSet:
        return self._load_path(self._path_for_id(sample_set_id))

    def save_sample_set(
        self,
        name: str,
        records: list[TestSampleRecord],
        method_snapshot: object = None,
    ) -> TestSampleSet:
        parsed_name = parse_sample_set_name(name)
        if not records:
            raise ValueError("Sample set has no samples to save.")
        parsed_method_snapshot = parse_sample_set_method_snapshot(
            method_snapshot,
            records,
        )

        now = utc_timestamp()
        sample_set_id = sample_set_id_from_name(parsed_name)
        path = self._path_for_id(sample_set_id)
        created_at = now
        if path.exists():
            try:
                existing = self._load_path(path)
                created_at = existing.created_at or now
            except (OSError, ValueError, json.JSONDecodeError):
                created_at = now

        sample_set = TestSampleSet(
            id=sample_set_id,
            name=parsed_name,
            samples=[copy_sample_record(record) for record in records],
            method_snapshot=parsed_method_snapshot,
            schema_version=1,
            created_at=created_at,
            updated_at=now,
        )
        self.directory.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"{json.dumps(sample_set_to_public(sample_set), indent=2)}\n",
            encoding="utf-8",
        )
        return sample_set

    def _path_for_id(self, sample_set_id: str) -> Path:
        parsed = parse_sample_set_id(sample_set_id)
        return self.directory / f"{parsed}.json"

    def _load_path(self, path: Path) -> TestSampleSet:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Sample set file must contain an object.")
        name = parse_sample_set_name(data.get("name"))
        raw_samples = data.get("samples")
        if not isinstance(raw_samples, list):
            raise ValueError("Sample set samples must be a list.")
        samples = [
            parse_sample_record(raw_sample, fallback_index=index)
            for index, raw_sample in enumerate(raw_samples, start=1)
        ]
        return TestSampleSet(
            id=parse_sample_set_id(data.get("id") or sample_set_id_from_name(name)),
            name=name,
            samples=samples,
            method_snapshot=parse_sample_set_method_snapshot(
                data.get("method_snapshot"),
                samples,
            ),
            schema_version=1,
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
        )

    def _summary(self, sample_set: TestSampleSet) -> dict[str, object]:
        return sample_set_summary(sample_set)


def parse_machine_payload(payload: list[str]) -> MachinePayload:
    # Arduino machine payload:
    # frame_mode, test_phase, fault_reason, raw_adc, force_n, step_rate,
    # position_mm, buttons, setup motion settings, test fields...
    if len(payload) < 19:
        raise ValueError("Machine payload is too short.")

    has_test_max_speed = len(payload) >= 20
    test_field_offset = 1 if has_test_max_speed else 0
    test_max_step_rate_steps_s = (
        float(payload[12]) if has_test_max_speed else None
    )

    return MachinePayload(
        frame_mode=payload[0],
        test_phase=payload[1],
        fault_reason=payload[2],
        raw_adc=int(float(payload[3])),
        force_n=float(payload[4]),
        step_rate_steps_s=float(payload[5]),
        position_mm=float(payload[6]),
        button_up=payload[7] == "1",
        button_down=payload[8] == "1",
        button_stop=payload[9] == "1",
        jog_speed_steps_s=float(payload[10]),
        acceleration_steps_s2=float(payload[11]),
        test_max_step_rate_steps_s=test_max_step_rate_steps_s,
        test_run_id=int(float(payload[12 + test_field_offset])),
        test_step_index=int(float(payload[13 + test_field_offset])),
        test_step_count=int(float(payload[14 + test_field_offset])),
        test_control_mode=payload[15 + test_field_offset],
        test_setpoint_force_n=float(payload[16 + test_field_offset]),
        test_setpoint_displacement_mm=float(payload[17 + test_field_offset]),
        test_elapsed_ms=int(float(payload[18 + test_field_offset])),
    )


class SerialMonitor:
    def __init__(self, config: AppConfig) -> None:
        # This object owns the Pi-to-Arduino serial link and live UI state.
        self.config = config
        self.snapshot = MachineSnapshot()
        # Only the recent raw-traffic window is sent on every browser refresh.
        self._serial_log: deque[str] = deque(
            maxlen=max(1, self.config.serial_log_max_lines))
        self._serial: serial.Serial | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()
        self._write_lock = asyncio.Lock()
        self._command_lock = asyncio.Lock()
        self._motion_lock = asyncio.Lock()
        self._motion_pending_until = 0.0
        self._motion_expected: tuple[float, float, float] | None = None
        self._motion_ack_future: asyncio.Future[tuple[float,
                                                      float, float]] | None = None
        self._tare_pending_until = 0.0
        self._tare_ack_future: asyncio.Future[None] | None = None
        self._displacement_zero_pending_until = 0.0
        self._displacement_zero_ack_future: asyncio.Future[None] | None = None

        self._test_steps: list[TestStep] = []
        self._test_samples: list[dict[str, object]] = []
        self._plot_points: list[dict[str, object]] = []
        self._plot_point_index = 0
        self._plot_reset_id = 0
        self._plot_start_controller_time_ms: int | None = None
        self._test_state = TestRunState()
        self._next_test_run_id = 1
        self._test_ack_future: asyncio.Future[None] | None = None
        self._test_ack_expected: tuple[str, int, int | None] | None = None
        self._test_heartbeat_task: asyncio.Task[None] | None = None
        self._utility_completion_future: asyncio.Future[None] | None = None
        self._utility_completion_error: TestCommandError | None = None
        self._initialization_start_position_mm: float | None = None
        self._initialization_max_travel_mm = 0.0
        self._initialization_abort_requested = False
        self._sample_records: list[TestSampleRecord] = []
        self._active_sample: TestSampleMetadata | None = None
        self._active_method_snapshot: dict[str, object] = {}
        self._sample_set_method_snapshot: dict[str, object] = {}
        self._test_run_kind = RUN_KIND_NONE
        self._current_run_finalized = False

    async def start(self) -> None:
        self._stopped.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopped.set()
        self._stop_test_heartbeat()
        if self._task is not None:
            await self._task
            self._task = None
        await self._close_serial()

    def public_snapshot(self) -> dict[str, object]:
        data = asdict(self.snapshot)
        data["raw_serial"] = list(self._serial_log)
        return data

    def public_test_state(self) -> dict[str, object]:
        return {
            "run": asdict(self._test_state),
            "run_kind": self._test_run_kind,
            "steps": [asdict(step) for step in self._test_steps],
            "sample_count": len(self._test_samples),
            "sample_set": self.public_sample_set(),
            "machine": self.public_snapshot(),
        }

    def default_sample_id(self) -> str:
        return self._default_sample_id()

    def sample_set_workbook(self) -> bytes:
        output = BytesIO()
        workbook = xlsxwriter.Workbook(output, {"in_memory": True})
        workbook.set_properties({"title": "Tensile sample set"})
        formats = workbook_formats(workbook)
        write_summary_worksheet(workbook, self._sample_records, formats)
        write_method_worksheet(
            workbook,
            self._workbook_method_snapshot(),
            formats,
        )

        used_names: set[str] = {"summary", "method", "plots"}
        plot_records: list[TestSampleRecord] = []
        for record in self._sample_records:
            sheet_name = safe_excel_sheet_name(record.sample_id, used_names)
            worksheet = workbook.add_worksheet(sheet_name)
            write_sample_worksheet(worksheet, record, formats)
            plot_records.append(record)
        if not self._sample_records:
            worksheet = workbook.add_worksheet("Samples")
            worksheet.write(0, 0, "No samples recorded", formats["title"])
            worksheet.set_column(0, 0, 24)

        write_plots_worksheet(workbook, plot_records, formats)
        workbook.close()
        return output.getvalue()

    def _workbook_method_snapshot(self) -> dict[str, object]:
        if self._active_method_snapshot:
            return dict(self._active_method_snapshot)
        if self._sample_set_method_snapshot:
            return dict(self._sample_set_method_snapshot)
        for record in reversed(self._sample_records):
            if record.method_snapshot:
                return dict(record.method_snapshot)
        return {}

    def public_sample_set(self) -> dict[str, object]:
        return {
            "next_sample_id": self._default_sample_id(),
            "active_sample": asdict(self._active_sample) if self._active_sample else None,
            "samples": [self._sample_record_summary(record) for record in self._sample_records],
        }

    def public_plot_data(self, after_index: int | None = None) -> dict[str, object]:
        if after_index is None or after_index > self._plot_point_index:
            points = self._plot_points
        else:
            points = [
                point for point in self._plot_points
                if int(point.get("index", 0)) > after_index
            ]
        return {
            "reset_id": self._plot_reset_id,
            "last_index": self._plot_point_index,
            "points": points,
        }

    def sample_overlay(self) -> dict[str, object]:
        series = []
        for record in self._sample_records:
            if not record.included:
                continue
            points = []
            for sample in record.samples:
                force = parse_optional_float(sample.get("force_n"))
                position = parse_optional_float(sample.get("position_mm"))
                if force is None or position is None:
                    continue
                points.append({
                    "positionMm": position,
                    "forceN": force,
                })
            if points:
                series.append({
                    "index": record.index,
                    "sample_id": record.sample_id,
                    "status": record.status,
                    "points": points,
                })
        return {"series": series}

    def clear_plot_data(self) -> dict[str, object]:
        self._plot_points = []
        self._plot_point_index = 0
        self._plot_reset_id += 1
        self._plot_start_controller_time_ms = None
        return self.public_plot_data()

    def clear_sample_set(self) -> None:
        if self._test_blocks_setup_control():
            raise TestCommandError(
                "Stop the active test before clearing the sample set.")
        self._sample_records = []
        self._active_sample = None
        self._active_method_snapshot = {}
        self._sample_set_method_snapshot = {}
        self._test_run_kind = RUN_KIND_NONE
        self._current_run_finalized = False
        self._test_steps = []
        self._test_samples = []
        self._test_state = TestRunState()

    def saved_sample_records(self) -> list[TestSampleRecord]:
        if self._test_blocks_setup_control():
            raise TestCommandError(
                "Stop the active test before saving the sample set.")
        return [copy_sample_record(record) for record in self._sample_records]

    def replace_sample_set(self, sample_set: TestSampleSet) -> None:
        if self._test_blocks_setup_control():
            raise TestCommandError(
                "Stop the active test before opening a saved sample set.")
        self._sample_records = [
            copy_sample_record(record)
            for record in sample_set.samples
        ]
        self._active_sample = None
        self._active_method_snapshot = {}
        self._sample_set_method_snapshot = dict(sample_set.method_snapshot)
        self._test_run_kind = RUN_KIND_NONE
        self._current_run_finalized = False
        self._test_steps = []
        self._test_samples = []
        self._test_state = TestRunState(
            message=f"Opened sample set \"{sample_set.name}\".")

    def set_sample_included(self, sample_index: int, included: bool) -> None:
        for record in self._sample_records:
            if record.index == sample_index:
                record.included = included
                return
        raise TestCommandError(f"Sample {sample_index} was not found.")

    def set_sample_notes(self, sample_index: int, notes: str) -> None:
        for record in self._sample_records:
            if record.index == sample_index:
                record.notes = parse_sample_notes(notes)
                return
        raise TestCommandError(f"Sample {sample_index} was not found.")

    def _sample_record_summary(self, record: TestSampleRecord) -> dict[str, object]:
        return {
            "index": record.index,
            "run_id": record.run_id,
            "sample_id": record.sample_id,
            "notes": record.notes,
            "status": record.status,
            "included": record.included,
            "started_at": record.started_at,
            "finished_at": record.finished_at,
            "point_count": record.point_count,
            "peak_force_n": record.peak_force_n,
            "peak_force_position_mm": record.peak_force_position_mm,
            "final_force_n": record.final_force_n,
            "final_position_mm": record.final_position_mm,
            "method_id": record.method_id,
            "method_name": record.method_name,
            "method_hash": record.method_hash,
        }

    async def send(self, line: str, log: bool = True) -> None:
        if self._serial is None:
            raise RuntimeError("Serial transport is not connected.")
        payload = f"{line}\n".encode("ascii", errors="ignore")
        connection = self._serial

        def write_and_flush() -> None:
            connection.write(payload)
            connection.flush()

        async with self._write_lock:
            if log:
                self._log_serial("TX", line)
            await asyncio.to_thread(write_and_flush)

    async def set_motion_settings(
        self,
        jog_speed_steps_s: float,
        test_max_step_rate_steps_s: float,
        acceleration_steps_s2: float,
    ) -> tuple[float, float, float]:
        if self._test_blocks_setup_control():
            raise MotionCommandError(
                "Automated test is active; stop it before changing jog settings.")

        async with self._command_lock, self._motion_lock:
            self.snapshot.jog_speed_steps_s = jog_speed_steps_s
            self.snapshot.test_max_step_rate_steps_s = test_max_step_rate_steps_s
            self.snapshot.acceleration_steps_s2 = acceleration_steps_s2
            self.snapshot.last_message = (
                f"Sending motion settings: jog {jog_speed_steps_s:.0f} steps/s, "
                f"test max {test_max_step_rate_steps_s:.0f} steps/s, "
                f"{acceleration_steps_s2:.0f} steps/s^2."
            )
            self._motion_pending_until = time.monotonic() + 4.0
            if self._serial is None:
                self._log_serial(
                    "SYS", f"Not sent: {self._motion_command()}; Arduino is disconnected.")
                raise MotionCommandError(
                    "Arduino is disconnected; motion settings were not delivered.")

            return await self._send_motion_with_retries(
                jog_speed_steps_s,
                test_max_step_rate_steps_s,
                acceleration_steps_s2,
            )

    async def tare_load(self) -> None:
        if self._test_blocks_setup_control():
            raise TareCommandError(
                "Automated test is active; stop it before taring.")

        async with self._command_lock:
            self.snapshot.last_message = "Collecting tare readings for 5 seconds."
            self._tare_pending_until = time.monotonic() + TARE_ACK_TIMEOUT_S + 1.0
            if self._serial is None:
                self._log_serial(
                    "SYS", "Not sent: ZERO_LOAD; Arduino is disconnected.")
                raise TareCommandError(
                    "Arduino is disconnected; tare command was not delivered.")

            await self._send_tare_with_retries()

    async def zero_displacement(self) -> None:
        if self._test_blocks_setup_control():
            raise DisplacementZeroCommandError(
                "Automated test is active; stop it before zeroing displacement.")

        async with self._command_lock:
            self.snapshot.last_message = "Zeroing displacement at the current position."
            self._displacement_zero_pending_until = (
                time.monotonic() + DISPLACEMENT_ZERO_ACK_TIMEOUT_S + 1.0)
            if self._serial is None:
                self._log_serial(
                    "SYS", "Not sent: ZERO_DISPLACEMENT; Arduino is disconnected.")
                raise DisplacementZeroCommandError(
                    "Arduino is disconnected; displacement zero command was not delivered.")

            await self._send_displacement_zero_with_retries()

    async def sample_calibration_adc(
        self,
        reference_force_n: float,
        duration_s: float = CALIBRATION_SAMPLE_DURATION_S,
        minimum_sample_count: int = CALIBRATION_SAMPLE_MIN_COUNT,
        poll_s: float = CALIBRATION_SAMPLE_POLL_S,
    ) -> CalibrationSample:
        async with self._command_lock:
            self._ensure_calibration_sampling_allowed()
            started_at = time.time()
            started_monotonic = time.monotonic()
            last_telemetry_seq = self.snapshot.telemetry_seq
            samples: list[int] = []
            self.snapshot.last_message = (
                f"Capturing {duration_s:.0f} second calibration ADC average."
            )

            while (time.monotonic() - started_monotonic) < duration_s:
                await asyncio.sleep(poll_s)
                self._ensure_calibration_sampling_allowed()
                if self.snapshot.telemetry_seq == last_telemetry_seq:
                    continue
                last_telemetry_seq = self.snapshot.telemetry_seq
                moving = (
                    abs(self.snapshot.step_rate_steps_s) >
                    CALIBRATION_STATIONARY_STEP_RATE_MAX_STEPS_S
                )
                if moving:
                    raise CalibrationCommandError(
                        "Machine moved during calibration sampling; keep load still and retry.")
                samples.append(self.snapshot.raw_adc)

            finished_at = time.time()
            if len(samples) < minimum_sample_count:
                raise CalibrationCommandError(
                    f"Calibration sample received only {len(samples)} fresh telemetry frames.")

            sample_count = len(samples)
            raw_adc_mean = sum(samples) / sample_count
            variance = sum(
                (sample - raw_adc_mean) * (sample - raw_adc_mean)
                for sample in samples
            ) / sample_count
            result = CalibrationSample(
                reference_force_n=reference_force_n,
                raw_adc_mean=raw_adc_mean,
                raw_adc_stddev=math.sqrt(variance),
                raw_adc_min=min(samples),
                raw_adc_max=max(samples),
                sample_count=sample_count,
                duration_s=time.monotonic() - started_monotonic,
                started_at=started_at,
                finished_at=finished_at,
            )
            self.snapshot.last_message = (
                f"Captured calibration point at {reference_force_n:.3f} N."
            )
            return result

    async def start_test(
        self,
        steps: list[TestStep],
        sample: TestSampleMetadata | None = None,
        method_snapshot: dict[str, object] | None = None,
    ) -> None:
        frozen_method_snapshot = freeze_method_snapshot(method_snapshot, steps)
        initialization = parse_method_initialization(
            frozen_method_snapshot.get("initialization"))
        if initialization.mode == INITIALIZATION_MODE_PRELOAD_UNLOAD_ZERO:
            await self._run_preload_unload_zero_initialization(initialization)

        await self._start_specimen_test(steps, sample, frozen_method_snapshot)

    async def _start_specimen_test(
        self,
        steps: list[TestStep],
        sample: TestSampleMetadata | None,
        method_snapshot: dict[str, object],
    ) -> None:
        async with self._command_lock:
            if self._serial is None:
                raise TestCommandError("Arduino is disconnected; test was not started.")
            if self._test_blocks_setup_control():
                raise TestCommandError(
                    "A test is already active. Stop it before starting a new test.")

            run_id = self._allocate_test_run_id()
            for index, step in enumerate(steps, start=1):
                if len(self._test_step_line(run_id, index, step)) >= 90:
                    raise TestCommandError(
                        f"Step {index} is too long for the Arduino serial command.")

            sample = sample or TestSampleMetadata(sample_id=self._default_sample_id())
            self._test_steps = list(steps)
            self._test_samples = []
            self._active_sample = sample
            self._active_method_snapshot = dict(method_snapshot)
            self._sample_set_method_snapshot = dict(method_snapshot)
            self._test_run_kind = RUN_KIND_SPECIMEN
            self._current_run_finalized = False
            self._test_state = TestRunState(
                run_id=run_id,
                status="STARTING",
                phase="STARTING",
                step_count=len(steps),
                message=f"Starting sample {sample.sample_id}.",
                started_at=time.time(),
            )

            try:
                await self._send_test_command_with_retries(
                    f"START_TEST,{run_id},{len(steps)}",
                    "START_TEST",
                    run_id,
                )
                await self._send_test_step(1)
            except TestCommandError:
                self._mark_test_fault("Arduino did not start the automated test.")
                raise

            self._test_state.status = "RUNNING"
            self._test_state.phase = "RAMPING"
            self._test_state.message = f"Sample {sample.sample_id} running."
            self._ensure_test_heartbeat(run_id)

    async def _run_preload_unload_zero_initialization(
        self,
        initialization: TestInitialization,
    ) -> None:
        completion = await self._start_initialization_utility_run(initialization)
        await completion
        async with self._command_lock:
            if self._serial is None:
                raise TestCommandError(
                    "Arduino is disconnected; displacement was not zeroed.")
            if self._test_blocks_setup_control():
                raise TestCommandError(
                    "Initialization did not return to setup before zeroing displacement.")
            self.snapshot.last_message = "Zeroing displacement after initialization."
            self._displacement_zero_pending_until = (
                time.monotonic() + DISPLACEMENT_ZERO_ACK_TIMEOUT_S + 1.0)
            try:
                await self._send_displacement_zero_with_retries()
            except DisplacementZeroCommandError as exc:
                raise TestCommandError(str(exc)) from exc
            self.clear_plot_data()

    async def _start_initialization_utility_run(
        self,
        initialization: TestInitialization,
    ) -> asyncio.Future[None]:
        async with self._command_lock:
            if self._serial is None:
                raise TestCommandError(
                    "Arduino is disconnected; initialization was not started.")
            if self._test_blocks_setup_control():
                raise TestCommandError(
                    "Stop the active test before initializing the specimen.")

            steps = self._initialization_steps(initialization)
            run_id = self._allocate_test_run_id()
            for index, step in enumerate(steps, start=1):
                if len(self._test_step_line(run_id, index, step)) >= 90:
                    raise TestCommandError(
                        f"Initialization step {index} is too long for the Arduino serial command.")

            loop = asyncio.get_running_loop()
            completion: asyncio.Future[None] = loop.create_future()
            self._utility_completion_future = completion
            self._utility_completion_error = None
            self._initialization_start_position_mm = self.snapshot.position_mm
            self._initialization_max_travel_mm = initialization.max_travel_mm
            self._initialization_abort_requested = False
            self._test_steps = steps
            self._active_sample = None
            self._active_method_snapshot = {}
            self._test_run_kind = RUN_KIND_INITIALIZATION
            self._current_run_finalized = False
            self._test_state = TestRunState(
                run_id=run_id,
                status="STARTING",
                phase="STARTING",
                step_count=len(steps),
                message=(
                    f"Initializing specimen to {initialization.preload_force_n:g} N preload."
                ),
                started_at=time.time(),
            )

            try:
                await self._send_test_command_with_retries(
                    f"START_TEST,{run_id},{len(steps)}",
                    "START_TEST",
                    run_id,
                )
                await self._send_test_step(1)
            except TestCommandError as exc:
                self._mark_test_fault("Arduino did not start specimen initialization.")
                raise TestCommandError(
                    "Arduino did not start specimen initialization.") from exc

            self._test_state.status = "RUNNING"
            self._test_state.phase = "RAMPING"
            self._test_state.message = "Initializing specimen preload."
            self._ensure_test_heartbeat(run_id)
            return completion

    def _initialization_steps(
        self,
        initialization: TestInitialization,
    ) -> list[TestStep]:
        return [
            TestStep(
                target_type="FORCE",
                target_value=initialization.preload_force_n,
                rate_type="DISPLACEMENT",
                rate_value_per_s=initialization.rate_mm_s,
                hold_duration_s=INITIALIZATION_HOLD_DURATION_S,
            ),
            TestStep(
                target_type="FORCE",
                target_value=initialization.unload_force_n,
                rate_type="DISPLACEMENT",
                rate_value_per_s=initialization.rate_mm_s,
                hold_duration_s=INITIALIZATION_HOLD_DURATION_S,
            ),
        ]

    async def return_to_zero(self, request: ReturnZeroRequest) -> None:
        async with self._command_lock:
            if self._serial is None:
                raise TestCommandError("Arduino is disconnected; return to zero was not started.")
            if self._test_blocks_setup_control():
                raise TestCommandError(
                    "Stop the active test before returning to zero.")

            step = self._return_zero_step(request)
            await self._start_utility_test_run(
                step=step,
                run_kind=RUN_KIND_RETURN_ZERO,
                starting_message=f"Starting return to {request.mode.lower()} zero.",
                running_message=f"Returning to {request.mode.lower()} zero.",
                failure_message="Arduino did not start return to zero.",
                length_error_message=(
                    "Return-to-zero command is too long for the Arduino serial command."
                ),
            )

    async def move_relative(self, offset_mm: float) -> None:
        async with self._command_lock:
            if self._serial is None:
                raise TestCommandError("Arduino is disconnected; relative move was not started.")
            if self._test_blocks_setup_control():
                raise TestCommandError(
                    "Stop the active test before moving the load head.")
            if abs(self.snapshot.step_rate_steps_s) > RELATIVE_MOVE_STATIONARY_STEP_RATE_MAX_STEPS_S:
                raise TestCommandError(
                    "Wait for the load head to stop before starting a relative move.")

            signed_offset = f"{offset_mm:+g}"
            await self._start_utility_test_run(
                step=self._relative_move_step(offset_mm),
                run_kind=RUN_KIND_RELATIVE_MOVE,
                starting_message=f"Starting {signed_offset} mm relative move.",
                running_message=f"Moving load head {signed_offset} mm.",
                failure_message="Arduino did not start the relative move.",
                length_error_message=(
                    "Relative-move command is too long for the Arduino serial command."
                ),
            )

    async def _start_utility_test_run(
        self,
        *,
        step: TestStep,
        run_kind: str,
        starting_message: str,
        running_message: str,
        failure_message: str,
        length_error_message: str,
    ) -> None:
        run_id = self._allocate_test_run_id()
        if len(self._test_step_line(run_id, 1, step)) >= 90:
            raise TestCommandError(length_error_message)

        self._test_steps = [step]
        self._active_sample = None
        self._active_method_snapshot = {}
        self._test_run_kind = run_kind
        self._current_run_finalized = False
        self._test_state = TestRunState(
            run_id=run_id,
            status="STARTING",
            phase="STARTING",
            step_count=1,
            message=starting_message,
            started_at=time.time(),
        )

        try:
            await self._send_test_command_with_retries(
                f"START_TEST,{run_id},1",
                "START_TEST",
                run_id,
            )
            await self._send_test_step(1)
        except TestCommandError:
            self._mark_test_fault(failure_message)
            raise

        self._test_state.status = "RUNNING"
        self._test_state.phase = "RAMPING"
        self._test_state.message = running_message
        self._ensure_test_heartbeat(run_id)

    async def pause_test(self) -> None:
        await self._send_simple_test_command("PAUSE_TEST", "Pausing automated test.")

    async def resume_test(self) -> None:
        await self._send_simple_test_command("RESUME_TEST", "Resuming automated test.")

    async def stop_test(self) -> None:
        await self._send_simple_test_command("STOP_TEST", "Stopping automated test and returning to setup.")
        self._mark_test_stopped()

    async def _send_simple_test_command(self, command: str, message: str) -> None:
        async with self._command_lock:
            run_id = self._test_state.run_id or self.snapshot.test_run_id
            if self._serial is None:
                raise TestCommandError("Arduino is disconnected.")
            if run_id == 0:
                raise TestCommandError("No automated test is active.")
            self._test_state.message = message
            await self._send_test_command_with_retries(
                f"{command},{run_id}",
                command,
                run_id,
            )

    def _default_sample_id(self) -> str:
        return f"Sample {len(self._sample_records) + 1}"

    def _return_zero_step(self, request: ReturnZeroRequest) -> TestStep:
        if request.mode == "LOAD":
            return TestStep(
                target_type="FORCE",
                target_value=0.0,
                rate_type="FORCE",
                rate_value_per_s=request.rate_value_per_s,
                hold_duration_s=RETURN_ZERO_HOLD_DURATION_S,
            )
        return TestStep(
            target_type="DISPLACEMENT",
            target_value=0.0,
            rate_type="DISPLACEMENT",
            rate_value_per_s=request.rate_value_per_s,
            hold_duration_s=0.0,
        )

    def _relative_move_step(self, offset_mm: float) -> TestStep:
        return TestStep(
            target_type="DISPLACEMENT",
            target_value=self.snapshot.position_mm + offset_mm,
            rate_type="DISPLACEMENT",
            rate_value_per_s=self.snapshot.test_max_step_rate_steps_s / STEPS_PER_MM,
            hold_duration_s=0.0,
        )

    async def _send_motion_with_retries(
        self,
        jog_speed_steps_s: float,
        test_max_step_rate_steps_s: float,
        acceleration_steps_s2: float,
    ) -> tuple[float, float, float]:
        return await self._send_setup_command_with_retries(
            command="SET_MOTION_LIMITS",
            send_attempt=lambda: self._send_motion_attempt(
                jog_speed_steps_s,
                test_max_step_rate_steps_s,
                acceleration_steps_s2,
            ),
            command_error_type=MotionCommandError,
            attempts=MOTION_COMMAND_ATTEMPTS,
            retry_delay_s=MOTION_RETRY_DELAY_S,
        )

    async def _send_setup_command_with_retries(
        self,
        *,
        command: str,
        send_attempt: Callable[[], Awaitable[SetupCommandResult]],
        command_error_type: type[MachineCommandError],
        attempts: int,
        retry_delay_s: float,
    ) -> SetupCommandResult:
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return await send_attempt()
            except TimeoutError as exc:
                last_error = exc
                message = f"Arduino did not acknowledge {command} before the timeout."
            except command_error_type as exc:
                last_error = exc
                message = str(exc)

            if attempt < attempts:
                retry_message = (
                    f"{message} Retrying {command} "
                    f"({attempt + 1}/{attempts})."
                )
                self.snapshot.last_message = retry_message
                self._log_serial("SYS", retry_message)
                await asyncio.sleep(retry_delay_s)

        final_message = f"Arduino did not confirm {command} after {attempts} attempts."
        self.snapshot.last_message = final_message
        self._log_serial("SYS", final_message)
        raise command_error_type(final_message) from last_error

    async def _send_motion_attempt(
        self,
        jog_speed_steps_s: float,
        test_max_step_rate_steps_s: float,
        acceleration_steps_s2: float,
    ) -> tuple[float, float, float]:
        loop = asyncio.get_running_loop()
        ack_future: asyncio.Future[tuple[float, float, float]] = loop.create_future()
        self._motion_expected = (
            jog_speed_steps_s,
            test_max_step_rate_steps_s,
            acceleration_steps_s2,
        )
        self._motion_ack_future = ack_future
        try:
            await self.send(self._motion_command())
            return await asyncio.wait_for(ack_future, timeout=MOTION_ACK_TIMEOUT_S)
        finally:
            self._clear_motion_ack_state(ack_future)

    def _clear_motion_ack_state(self, ack_future: asyncio.Future[tuple[float, float, float]]) -> None:
        if self._motion_ack_future is ack_future:
            self._motion_ack_future = None
            self._motion_expected = None

    async def _send_tare_with_retries(self) -> None:
        await self._send_setup_command_with_retries(
            command="ZERO_LOAD",
            send_attempt=self._send_tare_attempt,
            command_error_type=TareCommandError,
            attempts=TARE_COMMAND_ATTEMPTS,
            retry_delay_s=TARE_RETRY_DELAY_S,
        )

    async def _send_tare_attempt(self) -> None:
        loop = asyncio.get_running_loop()
        ack_future: asyncio.Future[None] = loop.create_future()
        self._tare_ack_future = ack_future
        try:
            await self.send("ZERO_LOAD")
            await asyncio.wait_for(ack_future, timeout=TARE_ACK_TIMEOUT_S)
        finally:
            self._clear_tare_ack_state(ack_future)

    def _clear_tare_ack_state(self, ack_future: asyncio.Future[None]) -> None:
        if self._tare_ack_future is ack_future:
            self._tare_ack_future = None

    async def _send_displacement_zero_with_retries(self) -> None:
        await self._send_setup_command_with_retries(
            command="ZERO_DISPLACEMENT",
            send_attempt=self._send_displacement_zero_attempt,
            command_error_type=DisplacementZeroCommandError,
            attempts=DISPLACEMENT_ZERO_COMMAND_ATTEMPTS,
            retry_delay_s=DISPLACEMENT_ZERO_RETRY_DELAY_S,
        )

    async def _send_displacement_zero_attempt(self) -> None:
        loop = asyncio.get_running_loop()
        ack_future: asyncio.Future[None] = loop.create_future()
        self._displacement_zero_ack_future = ack_future
        try:
            await self.send("ZERO_DISPLACEMENT")
            await asyncio.wait_for(
                ack_future, timeout=DISPLACEMENT_ZERO_ACK_TIMEOUT_S)
        finally:
            if self._displacement_zero_ack_future is ack_future:
                self._displacement_zero_ack_future = None

    async def _send_test_command_with_retries(
        self,
        line: str,
        expected_command: str,
        expected_run_id: int,
        expected_step_index: int | None = None,
    ) -> None:
        last_error: Exception | None = None
        for attempt in range(1, TEST_COMMAND_ATTEMPTS + 1):
            try:
                await self._send_test_command_attempt(
                    line,
                    expected_command,
                    expected_run_id,
                    expected_step_index,
                )
                return
            except TimeoutError as exc:
                last_error = exc
                message = f"Arduino did not acknowledge {expected_command} before the timeout."
            except TestCommandError as exc:
                last_error = exc
                message = str(exc)

            if attempt < TEST_COMMAND_ATTEMPTS:
                retry_message = (
                    f"{message} Retrying {expected_command} "
                    f"({attempt + 1}/{TEST_COMMAND_ATTEMPTS})."
                )
                self._test_state.message = retry_message
                self._log_serial("SYS", retry_message)
                await asyncio.sleep(TEST_RETRY_DELAY_S)

        final_message = f"Arduino did not confirm {expected_command} after {TEST_COMMAND_ATTEMPTS} attempts."
        self._test_state.message = final_message
        self._log_serial("SYS", final_message)
        raise TestCommandError(final_message) from last_error

    async def _send_test_command_attempt(
        self,
        line: str,
        expected_command: str,
        expected_run_id: int,
        expected_step_index: int | None,
    ) -> None:
        loop = asyncio.get_running_loop()
        ack_future: asyncio.Future[None] = loop.create_future()
        self._test_ack_expected = (
            expected_command, expected_run_id, expected_step_index)
        self._test_ack_future = ack_future
        try:
            await self.send(line)
            await asyncio.wait_for(ack_future, timeout=TEST_ACK_TIMEOUT_S)
        finally:
            if self._test_ack_future is ack_future:
                self._test_ack_future = None
                self._test_ack_expected = None

    async def _send_test_step(self, step_index: int) -> None:
        step = self._test_steps[step_index - 1]
        run_id = self._test_state.run_id
        await self._send_test_command_with_retries(
            self._test_step_line(run_id, step_index, step),
            "TEST_STEP",
            run_id,
            step_index,
        )
        self._test_state.step_index = step_index
        self._test_state.phase = "RAMPING"
        self._test_state.control_mode = step.rate_type
        self._test_state.setpoint_force_n = self.snapshot.force_n
        self._test_state.setpoint_displacement_mm = self.snapshot.position_mm
        self._test_state.message = f"Running step {step_index} of {len(self._test_steps)}."

    def _test_step_line(self, run_id: int, step_index: int, step: TestStep) -> str:
        hold_ms = int(round(step.hold_duration_s * 1000.0))
        return (
            f"TEST_STEP,{run_id},{step_index},"
            f"{step.target_type},{step.target_value:.4f},"
            f"{step.rate_type},{step.rate_value_per_s:.4f},{hold_ms}"
        )

    async def _send_next_step_from_event(self, run_id: int, step_index: int) -> None:
        async with self._command_lock:
            if run_id != self._test_state.run_id:
                return
            if step_index < 1 or step_index > len(self._test_steps):
                return
            if self._test_state.status not in {"RUNNING", "WAITING_NEXT"}:
                return
            try:
                await self._send_test_step(step_index)
                self._test_state.status = "RUNNING"
            except TestCommandError as exc:
                self._mark_test_fault(str(exc))

    def _ensure_test_heartbeat(self, run_id: int) -> None:
        self._stop_test_heartbeat()
        self._test_heartbeat_task = asyncio.create_task(
            self._test_heartbeat_loop(run_id))

    def _stop_test_heartbeat(self) -> None:
        if self._test_heartbeat_task is not None:
            self._test_heartbeat_task.cancel()
            self._test_heartbeat_task = None

    async def _test_heartbeat_loop(self, run_id: int) -> None:
        try:
            while True:
                await asyncio.sleep(TEST_HEARTBEAT_PERIOD_S)
                if run_id != self._test_state.run_id:
                    return
                if self._test_state.status in {"IDLE", "COMPLETE", "FAULT"}:
                    return
                try:
                    await self.send(f"TEST_HB,{run_id}")
                except RuntimeError as exc:
                    self._mark_test_fault(f"Heartbeat failed: {exc}")
                    return
        except asyncio.CancelledError:
            return

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
            self.snapshot.frame_mode = "CONNECTED"
            self.snapshot.updated_at = time.time()
            self.snapshot.last_message = f"Arduino connected on {self.config.serial_port}."
            self._log_serial("SYS", self.snapshot.last_message)
            await self.send("GET_STATUS")
            await self.send(self._motion_command())
        except Exception as exc:
            await self._close_serial()
            self.snapshot.connected = False
            self.snapshot.state = "DISCONNECTED"
            self.snapshot.frame_mode = "DISCONNECTED"
            self.snapshot.updated_at = time.time()
            self.snapshot.last_message = f"Arduino not detected on {self.config.serial_port}: {exc}"
            self._log_serial("SYS", self.snapshot.last_message)
            await asyncio.sleep(max(self.config.reconnect_delay_s, 0.25))

    async def _mark_disconnected(self, exc: Exception) -> None:
        await self._close_serial()
        self.snapshot.connected = False
        self.snapshot.state = "DISCONNECTED"
        self.snapshot.frame_mode = "DISCONNECTED"
        self.snapshot.step_rate_steps_s = 0.0
        self.snapshot.button_up = False
        self.snapshot.button_down = False
        self.snapshot.button_stop = False
        self.snapshot.updated_at = time.time()
        self.snapshot.last_message = f"Serial link lost: {exc}"
        self._log_serial("SYS", self.snapshot.last_message)
        if self._test_state.status in {"STARTING", "RUNNING", "PAUSED", "WAITING_NEXT"}:
            self._mark_test_fault("Serial link lost during automated test.")

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
            if kind == "TEL" and len(parts) >= 22:
                self.snapshot.telemetry_seq = int(parts[1])
                self.snapshot.controller_time_ms = int(parts[2])
                self._apply_payload(parts[3:], record_plot_point=True)
            elif kind == "STATUS" and len(parts) >= 20:
                self._apply_payload(parts[1:])
            elif kind == "ACK" and len(parts) >= 2:
                self._apply_ack(parts)
            elif kind == "EVT" and len(parts) >= 2:
                self._apply_event(parts)
            elif kind == "ERR" and len(parts) >= 2:
                self._apply_error(parts)
            else:
                self.snapshot.last_message = line
                self.snapshot.updated_at = time.time()
        except ValueError:
            self.snapshot.last_message = f"Could not parse serial line: {line}"
            self.snapshot.updated_at = time.time()

    def _apply_payload(
        self,
        payload: list[str],
        *,
        record_plot_point: bool = False,
    ) -> None:
        machine = parse_machine_payload(payload)
        self.snapshot.connected = True
        self.snapshot.state = machine.frame_mode
        self.snapshot.frame_mode = machine.frame_mode
        self.snapshot.fault_reason = machine.fault_reason
        self.snapshot.raw_adc = machine.raw_adc
        self.snapshot.force_n = machine.force_n
        self.snapshot.step_rate_steps_s = machine.step_rate_steps_s
        self.snapshot.position_mm = machine.position_mm
        self.snapshot.button_up = machine.button_up
        self.snapshot.button_down = machine.button_down
        self.snapshot.button_stop = machine.button_stop
        self.snapshot.test_run_id = machine.test_run_id
        self.snapshot.test_phase = machine.test_phase
        self.snapshot.test_step_index = machine.test_step_index
        self.snapshot.test_step_count = machine.test_step_count
        self.snapshot.test_control_mode = machine.test_control_mode
        self.snapshot.test_setpoint_force_n = machine.test_setpoint_force_n
        self.snapshot.test_setpoint_displacement_mm = machine.test_setpoint_displacement_mm
        self.snapshot.test_elapsed_ms = machine.test_elapsed_ms
        if machine.jog_speed_steps_s is not None and machine.acceleration_steps_s2 is not None:
            reported_test_max = (
                machine.test_max_step_rate_steps_s
                if machine.test_max_step_rate_steps_s is not None
                else self.snapshot.test_max_step_rate_steps_s
            )
            if self._motion_update_should_apply(
                machine.jog_speed_steps_s,
                reported_test_max,
                machine.acceleration_steps_s2,
            ):
                self.snapshot.jog_speed_steps_s = machine.jog_speed_steps_s
                if machine.test_max_step_rate_steps_s is not None:
                    self.snapshot.test_max_step_rate_steps_s = machine.test_max_step_rate_steps_s
                self.snapshot.acceleration_steps_s2 = machine.acceleration_steps_s2
        self.snapshot.updated_at = time.time()
        self.snapshot.last_message = "Telemetry received."
        if record_plot_point:
            self._record_plot_point(machine)
        self._apply_test_telemetry(machine)
        self._check_initialization_travel_limit(machine)

    def _record_plot_point(self, machine: MachinePayload) -> None:
        if self._plot_start_controller_time_ms is None:
            self._plot_start_controller_time_ms = self.snapshot.controller_time_ms
        if self.snapshot.controller_time_ms < self._plot_start_controller_time_ms:
            self._plot_start_controller_time_ms = self.snapshot.controller_time_ms

        self._plot_point_index += 1
        commanded_force_n = (
            machine.test_setpoint_force_n
            if machine.test_control_mode == "FORCE"
            else None
        )
        self._plot_points.append({
            "index": self._plot_point_index,
            "timeS": max(
                0.0,
                (self.snapshot.controller_time_ms - self._plot_start_controller_time_ms) /
                1000.0,
            ),
            "controllerTimeMs": self.snapshot.controller_time_ms,
            "telemetrySeq": self.snapshot.telemetry_seq,
            "forceN": machine.force_n,
            "positionMm": machine.position_mm,
            "stepRateStepsS": machine.step_rate_steps_s,
            "controlMode": machine.test_control_mode,
            "testPhase": machine.test_phase,
            "stepIndex": machine.test_step_index,
            "commandedForceN": commanded_force_n,
            "setpointDisplacementMm": machine.test_setpoint_displacement_mm,
        })

    def _apply_test_telemetry(self, machine: MachinePayload) -> None:
        if machine.test_run_id == 0:
            return
        if machine.test_run_id != self._test_state.run_id:
            return

        self._test_state.phase = machine.test_phase
        self._test_state.step_index = machine.test_step_index
        self._test_state.step_count = machine.test_step_count
        self._test_state.control_mode = machine.test_control_mode
        self._test_state.setpoint_force_n = machine.test_setpoint_force_n
        self._test_state.setpoint_displacement_mm = machine.test_setpoint_displacement_mm
        sample_row = {
            "wall_time_s": f"{time.time():.3f}",
            "controller_time_ms": machine.test_elapsed_ms,
            "run_id": machine.test_run_id,
            "frame_mode": machine.frame_mode,
            "step_index": machine.test_step_index,
            "phase": machine.test_phase,
            "fault_reason": machine.fault_reason,
            "control_mode": machine.test_control_mode,
            "setpoint_force_n": f"{machine.test_setpoint_force_n:.4f}",
            "setpoint_displacement_mm": f"{machine.test_setpoint_displacement_mm:.5f}",
            "force_n": f"{machine.force_n:.4f}",
            "position_mm": f"{machine.position_mm:.5f}",
            "step_rate_steps_s": f"{machine.step_rate_steps_s:.2f}",
        }
        if self._test_run_kind == RUN_KIND_SPECIMEN:
            self._test_samples.append(sample_row)

        if machine.test_phase == "PAUSED":
            self._test_state.status = "PAUSED"
        elif machine.test_phase == "WAITING_STEP":
            self._test_state.status = "WAITING_NEXT"
        elif machine.test_phase in {"RAMPING", "HOLDING"}:
            self._test_state.status = "RUNNING"
        elif machine.test_phase == "STOPPED":
            self._mark_test_stopped()
        elif machine.test_phase == "FAULTED":
            reason = machine.fault_reason if machine.fault_reason != "NONE" else "UNKNOWN"
            self._mark_test_fault(f"Automated test fault: {reason}.")

    def _check_initialization_travel_limit(self, machine: MachinePayload) -> None:
        if (
            self._test_run_kind != RUN_KIND_INITIALIZATION
            or self._initialization_start_position_mm is None
            or self._initialization_abort_requested
            or machine.test_run_id != self._test_state.run_id
            or self._test_state.status not in {"STARTING", "RUNNING", "PAUSED", "WAITING_NEXT"}
        ):
            return

        travel_mm = abs(machine.position_mm - self._initialization_start_position_mm)
        if travel_mm <= self._initialization_max_travel_mm:
            return

        message = (
            "Initialization exceeded max travel of "
            f"{self._initialization_max_travel_mm:g} mm before reaching preload."
        )
        self._initialization_abort_requested = True
        self._utility_completion_error = TestCommandError(message)
        asyncio.create_task(
            self._stop_initialization_after_travel_limit(self._test_state.run_id, message))

    async def _stop_initialization_after_travel_limit(
        self,
        run_id: int,
        message: str,
    ) -> None:
        async with self._command_lock:
            if self._test_run_kind != RUN_KIND_INITIALIZATION or run_id != self._test_state.run_id:
                return
            self._utility_completion_error = TestCommandError(message)
            self._test_state.message = message
            if self._serial is None:
                self._mark_test_fault(message)
                return
            try:
                await self._send_test_command_with_retries(
                    f"STOP_TEST,{run_id}",
                    "STOP_TEST",
                    run_id,
                )
            except TestCommandError:
                self._mark_test_fault(message)

    def _apply_ack(self, parts: list[str]) -> None:
        command = parts[1].upper()
        if command == "SET_MOTION_LIMITS" and len(parts) >= 5:
            try:
                self.snapshot.jog_speed_steps_s = float(parts[2])
                self.snapshot.test_max_step_rate_steps_s = float(parts[3])
                self.snapshot.acceleration_steps_s2 = float(parts[4])
                self._motion_pending_until = 0.0
                self.snapshot.last_message = (
                    f"Arduino accepted motion settings: jog {self.snapshot.jog_speed_steps_s:.0f} steps/s, "
                    f"test max {self.snapshot.test_max_step_rate_steps_s:.0f} steps/s, "
                    f"{self.snapshot.acceleration_steps_s2:.0f} steps/s^2."
                )
                self._complete_motion_ack(
                    self.snapshot.jog_speed_steps_s,
                    self.snapshot.test_max_step_rate_steps_s,
                    self.snapshot.acceleration_steps_s2,
                )
            except ValueError:
                self.snapshot.last_message = ",".join(parts)
        elif command == "ZERO_LOAD":
            self._tare_pending_until = 0.0
            self.snapshot.force_n = 0.0
            self.snapshot.last_message = "Load reading tared."
            self._complete_tare_ack()
        elif command == "ZERO_DISPLACEMENT":
            self._displacement_zero_pending_until = 0.0
            self.snapshot.position_mm = 0.0
            self.snapshot.last_message = "Displacement zeroed."
            self._complete_displacement_zero_ack()
        elif command in {"START_TEST", "PAUSE_TEST", "RESUME_TEST", "STOP_TEST"} and len(parts) >= 3:
            self._complete_test_ack(command, int(parts[2]), None)
            self._test_state.message = ",".join(parts)
        elif command == "TEST_STEP" and len(parts) >= 4:
            self._complete_test_ack(command, int(parts[2]), int(parts[3]))
            self._test_state.message = ",".join(parts)
        else:
            self.snapshot.last_message = ",".join(parts)
        self.snapshot.updated_at = time.time()

    def _apply_event(self, parts: list[str]) -> None:
        event = parts[1].upper()
        run_id = int(parts[2]) if len(parts) >= 3 else 0
        if run_id != self._test_state.run_id:
            return

        if event == "STEP_COMPLETE" and len(parts) >= 4:
            completed_step = int(parts[3])
            self._test_state.step_index = completed_step
            self._test_state.message = f"Step {completed_step} complete."
            if completed_step < len(self._test_steps):
                self._test_state.status = "WAITING_NEXT"
                asyncio.create_task(
                    self._send_next_step_from_event(run_id, completed_step + 1))
        elif event == "TEST_COMPLETE":
            self._mark_test_complete()
        elif event == "TEST_STOPPED":
            self._mark_test_stopped()
        elif event == "TEST_FAULT":
            reason = parts[3] if len(parts) >= 4 else "UNKNOWN"
            self._mark_test_fault(f"Automated test fault: {reason}.")
        elif event == "TEST_PAUSED":
            self._test_state.status = "PAUSED"
            self._test_state.phase = "PAUSED"
            self._test_state.message = "Automated test paused."
        elif event == "TEST_RESUMED":
            self._test_state.status = "RUNNING"
            self._test_state.message = "Automated test resumed."

    def _apply_error(self, parts: list[str]) -> None:
        message = ",".join(parts)
        motion_pending = self._motion_ack_future is not None and not self._motion_ack_future.done()
        tare_pending = self._tare_ack_future is not None and not self._tare_ack_future.done()
        displacement_zero_pending = (
            self._displacement_zero_ack_future is not None and
            not self._displacement_zero_ack_future.done())
        test_pending = self._test_ack_future is not None and not self._test_ack_future.done()

        if parts[1].upper() == "UNKNOWN_COMMAND" and (
            time.monotonic() < self._motion_pending_until
            or time.monotonic() < self._tare_pending_until
            or time.monotonic() < self._displacement_zero_pending_until
            or test_pending
        ):
            token = parts[2] if len(parts) >= 3 else ""
            if token:
                message = f"Arduino received an unknown or partial command token: {token}."
            else:
                message = "Arduino received an unknown command while a command was pending."

        if motion_pending:
            self._motion_ack_future.set_exception(MotionCommandError(message))
        if tare_pending:
            self._tare_ack_future.set_exception(TareCommandError(message))
        if displacement_zero_pending:
            self._displacement_zero_ack_future.set_exception(
                DisplacementZeroCommandError(message))
        if test_pending:
            self._test_ack_future.set_exception(TestCommandError(message))
        self.snapshot.last_message = message
        self._test_state.message = message
        self.snapshot.updated_at = time.time()

    def _complete_test_ack(self, command: str, run_id: int, step_index: int | None) -> None:
        if self._test_ack_future is None or self._test_ack_future.done():
            return
        if self._test_ack_expected is None:
            self._test_ack_future.set_result(None)
            return
        expected_command, expected_run_id, expected_step_index = self._test_ack_expected
        command_matches = command == expected_command
        run_matches = run_id == expected_run_id
        step_matches = expected_step_index is None or step_index == expected_step_index
        if command_matches and run_matches and step_matches:
            self._test_ack_future.set_result(None)
        else:
            self._test_ack_future.set_exception(
                TestCommandError(
                    f"Arduino acknowledged {command},{run_id},{step_index} "
                    f"while waiting for {expected_command},{expected_run_id},{expected_step_index}."
                )
            )

    def _log_serial(self, direction: str, line: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self._serial_log.append(f"{timestamp} {direction} {line}")

    def _motion_command(self) -> str:
        return (
            f"SET_MOTION_LIMITS,{self.snapshot.jog_speed_steps_s:.2f},"
            f"{self.snapshot.test_max_step_rate_steps_s:.2f},"
            f"{self.snapshot.acceleration_steps_s2:.2f}"
        )

    def _motion_update_should_apply(
        self,
        reported_jog_speed: float,
        reported_test_max_step_rate: float,
        reported_acceleration: float,
    ) -> bool:
        if time.monotonic() >= self._motion_pending_until:
            return True
        speed_matches = abs(
            reported_jog_speed - self.snapshot.jog_speed_steps_s) < 0.5
        test_speed_matches = abs(
            reported_test_max_step_rate - self.snapshot.test_max_step_rate_steps_s) < 0.5
        acceleration_matches = abs(
            reported_acceleration - self.snapshot.acceleration_steps_s2) < 0.5
        if speed_matches and test_speed_matches and acceleration_matches:
            self._motion_pending_until = 0.0
            return True
        return False

    def _complete_motion_ack(
        self,
        confirmed_jog_speed: float,
        confirmed_test_max_step_rate: float,
        confirmed_acceleration: float,
    ) -> None:
        if self._motion_ack_future is None or self._motion_ack_future.done():
            return
        if self._motion_expected is None:
            self._motion_ack_future.set_result(
                (confirmed_jog_speed, confirmed_test_max_step_rate, confirmed_acceleration))
            return
        expected_jog_speed, expected_test_max_step_rate, expected_acceleration = self._motion_expected
        speed_matches = abs(confirmed_jog_speed - expected_jog_speed) < 0.5
        test_speed_matches = abs(
            confirmed_test_max_step_rate - expected_test_max_step_rate) < 0.5
        acceleration_matches = abs(
            confirmed_acceleration - expected_acceleration) < 0.5
        if speed_matches and test_speed_matches and acceleration_matches:
            self._motion_ack_future.set_result(
                (confirmed_jog_speed, confirmed_test_max_step_rate, confirmed_acceleration))
        else:
            self._motion_ack_future.set_exception(
                MotionCommandError(
                    "Arduino acknowledged different motion settings: "
                    f"jog {confirmed_jog_speed:.0f} steps/s, "
                    f"test max {confirmed_test_max_step_rate:.0f} steps/s, "
                    f"{confirmed_acceleration:.0f} steps/s^2."
                )
            )

    def _complete_tare_ack(self) -> None:
        if self._tare_ack_future is None or self._tare_ack_future.done():
            return
        self._tare_ack_future.set_result(None)

    def _complete_displacement_zero_ack(self) -> None:
        if self._displacement_zero_ack_future is None or self._displacement_zero_ack_future.done():
            return
        self._displacement_zero_ack_future.set_result(None)

    def _allocate_test_run_id(self) -> int:
        run_id = self._next_test_run_id
        self._next_test_run_id += 1
        if self._next_test_run_id > 65000:
            self._next_test_run_id = 1
        return run_id

    def _finalize_active_sample(self, status: str) -> None:
        if (
            self._current_run_finalized
            or self._test_run_kind != RUN_KIND_SPECIMEN
            or self._active_sample is None
        ):
            return

        finished_at = self._test_state.finished_at or time.time()
        point_count = len(self._test_samples)
        peak_force = 0.0
        peak_position = 0.0
        final_force = 0.0
        final_position = 0.0

        if self._test_samples:
            peak_sample = max(
                self._test_samples,
                key=lambda sample: abs(parse_optional_float(sample.get("force_n")) or 0.0),
            )
            peak_force = parse_optional_float(peak_sample.get("force_n")) or 0.0
            peak_position = parse_optional_float(peak_sample.get("position_mm")) or 0.0
            final_sample = self._test_samples[-1]
            final_force = parse_optional_float(final_sample.get("force_n")) or 0.0
            final_position = parse_optional_float(final_sample.get("position_mm")) or 0.0

        method_snapshot = dict(self._active_method_snapshot)
        if method_snapshot:
            self._sample_set_method_snapshot = dict(method_snapshot)
        self._sample_records.append(TestSampleRecord(
            index=len(self._sample_records) + 1,
            run_id=self._test_state.run_id,
            sample_id=self._active_sample.sample_id,
            notes=self._active_sample.notes,
            status=status,
            included=status == "COMPLETE",
            started_at=self._test_state.started_at,
            finished_at=finished_at,
            point_count=point_count,
            peak_force_n=peak_force,
            peak_force_position_mm=peak_position,
            final_force_n=final_force,
            final_position_mm=final_position,
            method_id=str(method_snapshot.get("id") or ""),
            method_name=str(method_snapshot.get("name") or ""),
            method_hash=method_hash(method_snapshot) if method_snapshot else "",
            method_snapshot=method_snapshot,
            samples=[dict(sample) for sample in self._test_samples],
        ))
        self._current_run_finalized = True

    def _clear_active_run_context(self) -> None:
        self._active_sample = None
        self._active_method_snapshot = {}
        self._test_run_kind = RUN_KIND_NONE
        self._current_run_finalized = False

    def _complete_utility_run(self, error: TestCommandError | None = None) -> None:
        future = self._utility_completion_future
        if future is not None and not future.done():
            if error is None:
                future.set_result(None)
            else:
                future.set_exception(error)
        self._utility_completion_future = None
        self._utility_completion_error = None
        self._initialization_start_position_mm = None
        self._initialization_max_travel_mm = 0.0
        self._initialization_abort_requested = False

    def _mark_test_complete(self) -> None:
        was_initialization = self._test_run_kind == RUN_KIND_INITIALIZATION
        was_return_zero = self._test_run_kind == RUN_KIND_RETURN_ZERO
        was_relative_move = self._test_run_kind == RUN_KIND_RELATIVE_MOVE
        self._finalize_active_sample("COMPLETE")
        self._test_state.status = "COMPLETE"
        self._test_state.phase = "COMPLETE"
        self._test_state.finished_at = self._test_state.finished_at or time.time()
        if was_initialization:
            error = self._utility_completion_error
            if error is None:
                self._test_state.message = "Specimen initialization complete."
                self._complete_utility_run()
            else:
                self._test_state.message = str(error)
                self._complete_utility_run(error)
        elif was_return_zero:
            self._test_state.message = "Return to zero complete."
        elif was_relative_move:
            self._test_state.message = "Relative move complete."
        else:
            self._test_state.message = "Automated test complete."
        self._stop_test_heartbeat()
        self._reset_live_test_snapshot_to_setup()
        self._clear_active_run_context()

    def _mark_test_stopped(self) -> None:
        was_initialization = self._test_run_kind == RUN_KIND_INITIALIZATION
        was_return_zero = self._test_run_kind == RUN_KIND_RETURN_ZERO
        was_relative_move = self._test_run_kind == RUN_KIND_RELATIVE_MOVE
        self._finalize_active_sample("STOPPED")
        if was_initialization:
            error = self._utility_completion_error or TestCommandError(
                "Specimen initialization was stopped.")
            message = str(error)
            self._complete_utility_run(error)
        elif was_return_zero:
            message = "Return to zero stopped; controller returned to idle."
        elif was_relative_move:
            message = "Relative move stopped; controller returned to idle."
        else:
            message = "Automated test stopped; partial sample kept and controller returned to idle."
        self._test_state.run_id = 0
        self._test_state.status = "IDLE"
        self._test_state.phase = "NONE"
        self._test_state.step_index = 0
        self._test_state.step_count = 0
        self._test_state.control_mode = "NONE"
        self._test_state.setpoint_force_n = 0.0
        self._test_state.setpoint_displacement_mm = 0.0
        self._test_state.finished_at = self._test_state.finished_at or time.time()
        self._test_state.message = message
        self._stop_test_heartbeat()
        self._reset_live_test_snapshot_to_setup()
        self.snapshot.last_message = message
        self._clear_active_run_context()

    def _reset_live_test_snapshot_to_setup(self) -> None:
        # The controller is ready again while the completed run remains available to the UI.
        self.snapshot.state = "SETUP"
        self.snapshot.frame_mode = "SETUP"
        self.snapshot.fault_reason = "NONE"
        self.snapshot.test_run_id = 0
        self.snapshot.test_phase = "NONE"
        self.snapshot.test_step_index = 0
        self.snapshot.test_step_count = 0
        self.snapshot.test_control_mode = "NONE"
        self.snapshot.test_setpoint_force_n = 0.0
        self.snapshot.test_setpoint_displacement_mm = 0.0
        self.snapshot.test_elapsed_ms = 0
        self.snapshot.updated_at = time.time()

    def _mark_test_fault(self, message: str) -> None:
        was_initialization = self._test_run_kind == RUN_KIND_INITIALIZATION
        should_log = self._test_state.status != "FAULT" or self._test_state.message != message
        self._finalize_active_sample("FAULT")
        self._test_state.status = "FAULT"
        self._test_state.phase = "FAULTED"
        self._test_state.message = message
        self._test_state.finished_at = self._test_state.finished_at or time.time()
        self._stop_test_heartbeat()
        if was_initialization:
            self._complete_utility_run(TestCommandError(message))
        self._clear_active_run_context()
        if should_log:
            self._log_serial("SYS", message)

    def _test_blocks_setup_control(self) -> bool:
        if self.snapshot.test_phase != "NONE":
            return True
        return self._test_state.status in {"STARTING", "RUNNING", "PAUSED", "WAITING_NEXT", "FAULT"}

    def _ensure_calibration_sampling_allowed(self) -> None:
        if not self.snapshot.connected:
            raise CalibrationCommandError(
                "Arduino is disconnected; calibration sample was not captured.")
        if self._test_blocks_setup_control():
            raise CalibrationCommandError(
                "Machine is active; stop the current action before calibration sampling.")
        if self.snapshot.frame_mode == "FAULT" or self.snapshot.fault_reason != "NONE":
            raise CalibrationCommandError(
                "Machine is faulted; clear the fault before calibration sampling.")
        moving = (
            abs(self.snapshot.step_rate_steps_s) >
            CALIBRATION_STATIONARY_STEP_RATE_MAX_STEPS_S
        )
        if moving:
            raise CalibrationCommandError(
                "Machine is moving; stop motion before calibration sampling.")


config = AppConfig()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.method_store = TestMethodStore(config.method_dir)
    app.state.sample_set_store = TestSampleSetStore(config.sample_set_dir)
    app.state.monitor = SerialMonitor(config)
    await app.state.monitor.start()
    try:
        yield
    finally:
        await app.state.monitor.stop()


app = FastAPI(title="Tensile Tester", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index() -> RedirectResponse:
    return RedirectResponse(url="/test", status_code=307)


@app.get("/test", include_in_schema=False)
async def test_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "test.html")


@app.post("/api/motion")
async def set_motion(request: Request) -> JSONResponse:
    body = await request.json()
    monitor = request.app.state.monitor
    jog_speed_steps_s = clamp_float(
        float(body.get("speed_steps_s", monitor.snapshot.jog_speed_steps_s)),
        MOTION_SPEED_MIN,
        MOTION_SPEED_MAX,
    )
    test_max_step_rate_steps_s = clamp_float(
        float(body.get(
            "test_max_step_rate_steps_s",
            monitor.snapshot.test_max_step_rate_steps_s,
        )),
        TEST_SPEED_MIN,
        TEST_SPEED_MAX,
    )
    acceleration_steps_s2 = clamp_float(
        float(body.get("acceleration_steps_s2", monitor.snapshot.acceleration_steps_s2)),
        MOTION_ACCELERATION_MIN,
        MOTION_ACCELERATION_MAX,
    )
    try:
        (
            confirmed_jog_speed,
            confirmed_test_max_step_rate,
            confirmed_acceleration,
        ) = await monitor.set_motion_settings(
            jog_speed_steps_s,
            test_max_step_rate_steps_s,
            acceleration_steps_s2,
        )
    except MotionCommandError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    data = monitor.public_snapshot()
    data["motion_confirmed"] = True
    data["confirmed_speed_steps_s"] = confirmed_jog_speed
    data["confirmed_test_max_step_rate_steps_s"] = confirmed_test_max_step_rate
    data["confirmed_acceleration_steps_s2"] = confirmed_acceleration
    return JSONResponse(data)


@app.post("/api/tare")
async def tare(request: Request) -> JSONResponse:
    try:
        await request.app.state.monitor.tare_load()
    except TareCommandError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    data = request.app.state.monitor.public_snapshot()
    data["tare_confirmed"] = True
    return JSONResponse(data)


@app.post("/api/zero-displacement")
async def zero_displacement(request: Request) -> JSONResponse:
    try:
        await request.app.state.monitor.zero_displacement()
    except DisplacementZeroCommandError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    data = request.app.state.monitor.public_snapshot()
    data["displacement_zero_confirmed"] = True
    return JSONResponse(data)


@app.post("/api/calibration/sample")
async def calibration_sample(request: Request) -> JSONResponse:
    body = await request.json()
    try:
        reference_force_n = parse_finite_float(
            body.get("reference_force_n"), "Reference force")
        sample = await request.app.state.monitor.sample_calibration_adc(
            reference_force_n)
    except (ValueError, CalibrationCommandError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(asdict(sample))


@app.post("/api/calibration/fit")
async def calibration_fit(request: Request) -> JSONResponse:
    body = await request.json()
    try:
        points = parse_calibration_points(body)
        fit = fit_load_cell_calibration(points)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(fit)


@app.get("/api/test/state")
async def test_state(request: Request) -> JSONResponse:
    return JSONResponse(request.app.state.monitor.public_test_state())


@app.get("/api/test/methods")
async def list_test_methods(request: Request) -> JSONResponse:
    return JSONResponse({"methods": request.app.state.method_store.list_methods()})


@app.get("/api/test/methods/{method_id}")
async def get_test_method(request: Request, method_id: str) -> JSONResponse:
    try:
        method = request.app.state.method_store.get_method(method_id)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=404, detail="Test method was not found.") from exc
    return JSONResponse(method)


@app.post("/api/test/methods")
async def save_test_method(request: Request) -> JSONResponse:
    body = await request.json()
    try:
        method = request.app.state.method_store.save_method(body)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(method)


@app.get("/api/test/sample-sets")
async def list_test_sample_sets(request: Request) -> JSONResponse:
    return JSONResponse({
        "sample_sets": request.app.state.sample_set_store.list_sample_sets(),
    })


@app.get("/api/test/sample-sets/{sample_set_id}")
async def get_test_sample_set(request: Request, sample_set_id: str) -> JSONResponse:
    try:
        sample_set = request.app.state.sample_set_store.get_sample_set(sample_set_id)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=404, detail="Sample set was not found.") from exc
    return JSONResponse(sample_set_to_public(sample_set))


@app.post("/api/test/sample-sets")
async def save_test_sample_set(request: Request) -> JSONResponse:
    body = await request.json()
    try:
        name = parse_sample_set_name(body.get("name"))
        records = request.app.state.monitor.saved_sample_records()
        sample_set = request.app.state.sample_set_store.save_sample_set(
            name,
            records,
            body.get("method_snapshot"),
        )
    except (OSError, ValueError, TestCommandError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(sample_set_to_public(sample_set))


@app.post("/api/test/sample-sets/{sample_set_id}/open")
async def open_test_sample_set(request: Request, sample_set_id: str) -> JSONResponse:
    try:
        sample_set = request.app.state.sample_set_store.get_sample_set(sample_set_id)
        request.app.state.monitor.replace_sample_set(sample_set)
    except (OSError, ValueError, json.JSONDecodeError, TestCommandError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    data = request.app.state.monitor.public_test_state()
    data["opened_sample_set"] = sample_set_summary(sample_set)
    data["method_snapshot"] = dict(sample_set.method_snapshot)
    return JSONResponse(data)


@app.post("/api/test/start")
async def start_test(request: Request) -> JSONResponse:
    body = await request.json()
    monitor = request.app.state.monitor
    try:
        steps = parse_test_steps(body)
        sample = parse_sample_metadata(body, monitor.default_sample_id())
        method_snapshot = parse_run_method_snapshot(body, steps)
        await monitor.start_test(steps, sample, method_snapshot)
    except (ValueError, TestCommandError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(monitor.public_test_state())


@app.post("/api/test/return-zero")
async def return_to_zero(request: Request) -> JSONResponse:
    body = await request.json()
    try:
        return_request = parse_return_zero_request(body)
        await request.app.state.monitor.return_to_zero(return_request)
    except (ValueError, TestCommandError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(request.app.state.monitor.public_test_state())


@app.post("/api/test/move-relative")
async def move_relative(request: Request) -> JSONResponse:
    body = await request.json()
    try:
        offset_mm = parse_relative_move_offset(body)
        await request.app.state.monitor.move_relative(offset_mm)
    except (ValueError, TestCommandError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(request.app.state.monitor.public_test_state())


@app.post("/api/test/pause")
async def pause_test(request: Request) -> JSONResponse:
    try:
        await request.app.state.monitor.pause_test()
    except TestCommandError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(request.app.state.monitor.public_test_state())


@app.post("/api/test/resume")
async def resume_test(request: Request) -> JSONResponse:
    try:
        await request.app.state.monitor.resume_test()
    except TestCommandError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(request.app.state.monitor.public_test_state())


@app.post("/api/test/stop")
async def stop_test(request: Request) -> JSONResponse:
    try:
        await request.app.state.monitor.stop_test()
    except TestCommandError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(request.app.state.monitor.public_test_state())


@app.post("/api/test/samples/clear")
async def clear_test_samples(request: Request) -> JSONResponse:
    try:
        request.app.state.monitor.clear_sample_set()
    except TestCommandError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(request.app.state.monitor.public_test_state())


@app.post("/api/test/samples/include")
async def set_test_sample_included(request: Request) -> JSONResponse:
    body = await request.json()
    try:
        sample_index = int(body.get("index"))
        included = parse_boolean(body.get("included"), "included")
        request.app.state.monitor.set_sample_included(sample_index, included)
    except (TypeError, ValueError, TestCommandError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(request.app.state.monitor.public_test_state())


@app.post("/api/test/samples/notes")
async def set_test_sample_notes(request: Request) -> JSONResponse:
    body = await request.json()
    try:
        sample_index = int(body.get("index"))
        notes = parse_sample_notes(body.get("notes"))
        request.app.state.monitor.set_sample_notes(sample_index, notes)
    except (TypeError, ValueError, TestCommandError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(request.app.state.monitor.public_test_state())


@app.get("/api/test/samples/overlay")
async def test_sample_overlay(request: Request) -> JSONResponse:
    return JSONResponse(request.app.state.monitor.sample_overlay())


@app.get("/api/test/plots")
async def test_plot_data(request: Request) -> JSONResponse:
    raw_after = request.query_params.get("after")
    try:
        after_index = int(raw_after) if raw_after is not None else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="after must be an integer") from exc
    return JSONResponse(request.app.state.monitor.public_plot_data(after_index))


@app.post("/api/test/plots/clear")
async def clear_test_plots(request: Request) -> JSONResponse:
    return JSONResponse(request.app.state.monitor.clear_plot_data())


@app.get("/api/test/samples/csv")
async def test_samples_csv(request: Request) -> Response:
    data = request.app.state.monitor.sample_set_workbook()
    return Response(
        data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=tensile-sample-set.xlsx"},
    )


@app.get("/health")
async def health(request: Request) -> dict[str, object]:
    data = request.app.state.monitor.public_snapshot()
    return {
        "ok": True,
        "arduino_connected": data["connected"],
        "frame_mode": data["frame_mode"],
        "test_phase": data["test_phase"],
    }


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def method_id_from_name(name: str) -> str:
    return slug_id_from_name(name, "method")


def sample_set_id_from_name(name: str) -> str:
    return slug_id_from_name(name, "sample-set")


def slug_id_from_name(name: str, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    slug = slug[:64].strip("-")
    return slug or fallback


def parse_method_id(value: object) -> str:
    parsed = str(value or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", parsed):
        raise ValueError("Method ID is not valid.")
    return parsed


def parse_sample_set_id(value: object) -> str:
    parsed = str(value or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", parsed):
        raise ValueError("Sample set ID is not valid.")
    return parsed


def parse_method_name(value: object) -> str:
    parsed = str(value or "").strip()
    if not parsed:
        raise ValueError("Method name is required.")
    if len(parsed) > METHOD_NAME_MAX_LENGTH:
        raise ValueError(
            f"Method name cannot exceed {METHOD_NAME_MAX_LENGTH} characters.")
    return parsed


def parse_sample_set_name(value: object) -> str:
    parsed = str(value or "").strip()
    if not parsed:
        raise ValueError("Sample set name is required.")
    if len(parsed) > SAMPLE_SET_NAME_MAX_LENGTH:
        raise ValueError(
            f"Sample set name cannot exceed {SAMPLE_SET_NAME_MAX_LENGTH} characters.")
    return parsed


def parse_method_motion(raw_motion: object) -> dict[str, float]:
    raw = raw_motion if isinstance(raw_motion, dict) else {}
    values = {
        "jog_speed_steps_s": (
            raw.get("jog_speed_steps_s", MOTION_SPEED_DEFAULT),
            MOTION_SPEED_MIN,
            MOTION_SPEED_MAX,
            "Method jog speed",
        ),
        "test_max_step_rate_steps_s": (
            raw.get("test_max_step_rate_steps_s", TEST_SPEED_DEFAULT),
            TEST_SPEED_MIN,
            TEST_SPEED_MAX,
            "Method test max speed",
        ),
        "acceleration_steps_s2": (
            raw.get("acceleration_steps_s2", MOTION_ACCELERATION_DEFAULT),
            MOTION_ACCELERATION_MIN,
            MOTION_ACCELERATION_MAX,
            "Method acceleration",
        ),
    }
    parsed: dict[str, float] = {}
    for key, (value, minimum, maximum, label) in values.items():
        numeric = parse_finite_float(value, label)
        if numeric < minimum or numeric > maximum:
            raise ValueError(f"{label} must be between {minimum:g} and {maximum:g}.")
        parsed[key] = numeric
    return parsed


def parse_method_initialization(raw_initialization: object) -> TestInitialization:
    raw = raw_initialization if isinstance(raw_initialization, dict) else {}
    mode = str(raw.get("mode") or INITIALIZATION_MODE_NONE).strip().upper()
    if mode not in INITIALIZATION_MODES:
        raise ValueError("Initialization mode is not valid.")
    if mode == INITIALIZATION_MODE_NONE:
        return TestInitialization()

    preload_force_n = parse_finite_float(
        raw.get("preload_force_n", INITIALIZATION_PRELOAD_FORCE_DEFAULT_N),
        "Initialization preload force",
    )
    unload_force_n = parse_finite_float(
        raw.get("unload_force_n", INITIALIZATION_UNLOAD_FORCE_DEFAULT_N),
        "Initialization unload force",
    )
    rate_mm_s = parse_finite_float(
        raw.get("rate_mm_s", INITIALIZATION_RATE_DEFAULT_MM_S),
        "Initialization rate",
    )
    max_travel_mm = parse_finite_float(
        raw.get("max_travel_mm", INITIALIZATION_MAX_TRAVEL_DEFAULT_MM),
        "Initialization max travel",
    )
    if preload_force_n == 0.0:
        raise ValueError("Initialization preload force cannot be zero.")
    if rate_mm_s <= 0.0:
        raise ValueError("Initialization rate must be greater than zero.")
    if max_travel_mm <= 0.0:
        raise ValueError("Initialization max travel must be greater than zero.")

    return TestInitialization(
        mode=mode,
        preload_force_n=preload_force_n,
        unload_force_n=unload_force_n,
        rate_mm_s=rate_mm_s,
        max_travel_mm=max_travel_mm,
    )


def parse_test_method(payload: dict[str, object]) -> TestMethod:
    if not isinstance(payload, dict):
        raise ValueError("Method payload is not valid.")
    name = parse_method_name(payload.get("name"))
    return TestMethod(
        id=method_id_from_name(name),
        name=name,
        steps=parse_test_steps(payload),
        motion=parse_method_motion(payload.get("motion")),
        initialization=parse_method_initialization(payload.get("initialization")),
        schema_version=1,
        created_at=str(payload.get("created_at") or ""),
        updated_at=str(payload.get("updated_at") or ""),
    )


def method_to_public(method: TestMethod) -> dict[str, object]:
    stable = {
        "schema_version": method.schema_version,
        "id": method.id,
        "name": method.name,
        "steps": [asdict(step) for step in method.steps],
        "motion": dict(method.motion),
        "initialization": asdict(method.initialization),
    }
    return {
        **stable,
        "created_at": method.created_at,
        "updated_at": method.updated_at,
        "hash": method_hash(stable),
    }


def sample_set_to_public(sample_set: TestSampleSet) -> dict[str, object]:
    return {
        "schema_version": sample_set.schema_version,
        "id": sample_set.id,
        "name": sample_set.name,
        "sample_count": len(sample_set.samples),
        "method_snapshot": dict(sample_set.method_snapshot),
        "samples": [
            sample_record_to_public(record)
            for record in sample_set.samples
        ],
        "created_at": sample_set.created_at,
        "updated_at": sample_set.updated_at,
    }


def sample_set_summary(sample_set: TestSampleSet) -> dict[str, object]:
    return {
        "id": sample_set.id,
        "name": sample_set.name,
        "sample_count": len(sample_set.samples),
        "created_at": sample_set.created_at,
        "updated_at": sample_set.updated_at,
    }


def sample_record_to_public(record: TestSampleRecord) -> dict[str, object]:
    return asdict(record)


def copy_sample_record(record: TestSampleRecord) -> TestSampleRecord:
    return parse_sample_record(sample_record_to_public(record), record.index)


def parse_sample_set_method_snapshot(
    raw_snapshot: object,
    records: list[TestSampleRecord],
) -> dict[str, object]:
    raw = raw_snapshot if isinstance(raw_snapshot, dict) else {}
    if not raw:
        for record in reversed(records):
            if record.method_snapshot:
                return dict(record.method_snapshot)
        return {}

    steps = parse_test_steps(raw)
    name = parse_method_name(raw.get("name") or "Unsaved Method")
    raw_id = str(raw.get("id") or "").strip()
    method_id = parse_method_id(raw_id) if raw_id else method_id_from_name(name)
    snapshot = {
        "schema_version": 1,
        "id": method_id,
        "name": name,
        "steps": [asdict(step) for step in steps],
        "motion": parse_method_motion(raw.get("motion")),
        "initialization": asdict(parse_method_initialization(raw.get("initialization"))),
    }
    snapshot["hash"] = method_hash(snapshot)
    return snapshot


def method_hash(snapshot: dict[str, object]) -> str:
    stable = {
        key: value
        for key, value in snapshot.items()
        if key not in {"created_at", "updated_at", "hash"}
    }
    encoded = json.dumps(stable, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:12]


def parse_run_method_snapshot(
    body: dict[str, object],
    steps: list[TestStep],
) -> dict[str, object]:
    raw_snapshot = body.get("method_snapshot")
    raw = raw_snapshot if isinstance(raw_snapshot, dict) else {}
    raw_id = str(raw.get("id") or body.get("method_id") or "").strip()
    method_id = parse_method_id(raw_id) if raw_id else ""
    name = parse_method_name(raw.get("name") or body.get("method_name") or "Unsaved Method")
    snapshot = {
        "schema_version": 1,
        "id": method_id,
        "name": name,
        "steps": [asdict(step) for step in steps],
        "motion": parse_method_motion(raw.get("motion")),
        "initialization": asdict(parse_method_initialization(raw.get("initialization"))),
    }
    snapshot["hash"] = method_hash(snapshot)
    return snapshot


def freeze_method_snapshot(
    method_snapshot: dict[str, object] | None,
    steps: list[TestStep],
) -> dict[str, object]:
    if method_snapshot:
        return parse_run_method_snapshot({"method_snapshot": method_snapshot}, steps)
    return parse_run_method_snapshot({}, steps)


def parse_test_steps(body: dict[str, object]) -> list[TestStep]:
    raw_steps = body.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("Add at least one test step.")

    steps: list[TestStep] = []
    for index, raw_step in enumerate(raw_steps, start=1):
        if not isinstance(raw_step, dict):
            raise ValueError(f"Step {index} is not valid.")
        target_type = parse_test_type(
            raw_step.get("target_type"), TEST_TARGET_TYPES, f"Step {index} target type")
        target = parse_finite_float(raw_step.get("target_value"), f"Step {index} target")
        rate_type = parse_test_type(
            raw_step.get("rate_type"), TEST_RATE_TYPES, f"Step {index} rate type")
        rate = parse_finite_float(raw_step.get("rate_value_per_s"), f"Step {index} rate")
        hold = parse_finite_float(raw_step.get("hold_duration_s", 0), f"Step {index} hold duration")
        if rate <= 0.0:
            raise ValueError(f"Step {index} rate must be greater than zero.")
        if hold < 0.0:
            raise ValueError(f"Step {index} hold duration cannot be negative.")
        if hold > TEST_HOLD_DURATION_MAX_S:
            raise ValueError(f"Step {index} hold duration cannot exceed 24 hours.")
        steps.append(TestStep(
            target_type=target_type,
            target_value=target,
            rate_type=rate_type,
            rate_value_per_s=rate,
            hold_duration_s=hold,
        ))

    return steps


def parse_sample_metadata(body: dict[str, object], default_sample_id: str) -> TestSampleMetadata:
    raw_sample = body.get("sample")
    if raw_sample is None:
        return TestSampleMetadata(sample_id=default_sample_id)
    if not isinstance(raw_sample, dict):
        raise ValueError("Sample metadata is not valid.")

    sample_id = str(raw_sample.get("id") or "").strip() or default_sample_id
    notes = parse_sample_notes(raw_sample.get("notes"))
    if len(sample_id) > SAMPLE_ID_MAX_LENGTH:
        raise ValueError(
            f"Sample ID cannot exceed {SAMPLE_ID_MAX_LENGTH} characters.")
    return TestSampleMetadata(sample_id=sample_id, notes=notes)


def parse_sample_notes(value: object) -> str:
    notes = str(value or "").strip()
    if len(notes) > SAMPLE_NOTES_MAX_LENGTH:
        raise ValueError(
            f"Sample notes cannot exceed {SAMPLE_NOTES_MAX_LENGTH} characters.")
    return notes


def parse_sample_record(raw_record: object, fallback_index: int) -> TestSampleRecord:
    if not isinstance(raw_record, dict):
        raise ValueError("Sample record is not valid.")
    raw_samples = raw_record.get("samples", [])
    if not isinstance(raw_samples, list):
        raise ValueError("Sample telemetry must be a list.")
    samples: list[dict[str, object]] = []
    for raw_sample in raw_samples:
        if not isinstance(raw_sample, dict):
            raise ValueError("Sample telemetry rows must be objects.")
        samples.append(dict(raw_sample))

    sample_id = str(raw_record.get("sample_id") or f"Sample {fallback_index}").strip()
    if not sample_id:
        sample_id = f"Sample {fallback_index}"
    if len(sample_id) > SAMPLE_ID_MAX_LENGTH:
        raise ValueError(
            f"Sample ID cannot exceed {SAMPLE_ID_MAX_LENGTH} characters.")

    raw_method_snapshot = raw_record.get("method_snapshot")
    method_snapshot = (
        dict(raw_method_snapshot)
        if isinstance(raw_method_snapshot, dict)
        else {}
    )
    status = str(raw_record.get("status") or "COMPLETE").strip().upper() or "COMPLETE"
    return TestSampleRecord(
        index=parse_positive_int(
            raw_record.get("index", fallback_index), "Sample index"),
        run_id=parse_nonnegative_int(
            raw_record.get("run_id", 0), "Sample run ID"),
        sample_id=sample_id,
        notes=parse_sample_notes(raw_record.get("notes")),
        status=status,
        included=parse_boolean(
            raw_record.get("included", status == "COMPLETE"),
            "Sample included",
        ),
        started_at=parse_finite_float(
            raw_record.get("started_at", 0.0), "Sample started time"),
        finished_at=parse_finite_float(
            raw_record.get("finished_at", 0.0), "Sample finished time"),
        point_count=parse_nonnegative_int(
            raw_record.get("point_count", len(samples)), "Sample point count"),
        peak_force_n=parse_finite_float(
            raw_record.get("peak_force_n", 0.0), "Sample peak force"),
        peak_force_position_mm=parse_finite_float(
            raw_record.get("peak_force_position_mm", 0.0),
            "Sample peak force position",
        ),
        final_force_n=parse_finite_float(
            raw_record.get("final_force_n", 0.0), "Sample final force"),
        final_position_mm=parse_finite_float(
            raw_record.get("final_position_mm", 0.0), "Sample final position"),
        method_id=str(raw_record.get("method_id") or "").strip(),
        method_name=str(raw_record.get("method_name") or "").strip(),
        method_hash=str(raw_record.get("method_hash") or "").strip(),
        method_snapshot=method_snapshot,
        samples=samples,
    )


def parse_return_zero_request(body: dict[str, object]) -> ReturnZeroRequest:
    mode = str(body.get("mode") or "DISPLACEMENT").strip().upper()
    if mode not in RETURN_ZERO_MODES:
        raise ValueError("Return-to-zero mode must be LOAD or DISPLACEMENT.")
    default_rate = (
        RETURN_ZERO_LOAD_DEFAULT_RATE_N_S
        if mode == "LOAD"
        else RETURN_ZERO_DISPLACEMENT_DEFAULT_RATE_MM_S
    )
    rate = parse_finite_float(
        body.get("rate_value_per_s", default_rate),
        "Return-to-zero rate",
    )
    if rate <= 0.0:
        raise ValueError("Return-to-zero rate must be greater than zero.")
    return ReturnZeroRequest(mode=mode, rate_value_per_s=rate)


def parse_relative_move_offset(body: dict[str, object]) -> float:
    offset_mm = parse_finite_float(body.get("offset_mm"), "Relative move offset")
    if offset_mm not in RELATIVE_MOVE_AMOUNTS_MM:
        allowed = ", ".join(f"{amount:+g}" for amount in sorted(
            RELATIVE_MOVE_AMOUNTS_MM, reverse=True))
        raise ValueError(f"Relative move offset must be one of: {allowed} mm.")
    return offset_mm


def parse_calibration_points(body: dict[str, object]) -> list[CalibrationSample]:
    raw_points = body.get("points")
    if not isinstance(raw_points, list):
        raise ValueError("Calibration points must be a list.")

    points: list[CalibrationSample] = []
    for index, raw_point in enumerate(raw_points, start=1):
        if not isinstance(raw_point, dict):
            raise ValueError(f"Calibration point {index} is not valid.")
        reference_force_n = parse_finite_float(
            raw_point.get("reference_force_n"),
            f"Calibration point {index} reference force",
        )
        raw_adc_mean = parse_finite_float(
            raw_point.get("raw_adc_mean"),
            f"Calibration point {index} raw ADC mean",
        )
        points.append(CalibrationSample(
            reference_force_n=reference_force_n,
            raw_adc_mean=raw_adc_mean,
            raw_adc_stddev=parse_optional_calibration_float(
                raw_point, "raw_adc_stddev", 0.0),
            raw_adc_min=parse_optional_calibration_float(
                raw_point, "raw_adc_min", raw_adc_mean),
            raw_adc_max=parse_optional_calibration_float(
                raw_point, "raw_adc_max", raw_adc_mean),
            sample_count=parse_optional_calibration_int(
                raw_point, "sample_count", 0),
            duration_s=parse_optional_calibration_float(
                raw_point, "duration_s", 0.0),
            started_at=parse_optional_calibration_float(
                raw_point, "started_at", 0.0),
            finished_at=parse_optional_calibration_float(
                raw_point, "finished_at", 0.0),
        ))
    return points


def parse_optional_calibration_float(
    raw_point: dict[str, object],
    key: str,
    default: float,
) -> float:
    value = raw_point.get(key, default)
    return parse_finite_float(value, key)


def parse_optional_calibration_int(
    raw_point: dict[str, object],
    key: str,
    default: int,
) -> int:
    value = raw_point.get(key, default)
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be an integer.") from exc
    if parsed < 0:
        raise ValueError(f"{key} cannot be negative.")
    return parsed


def parse_nonnegative_int(value: object, label: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an integer.") from exc
    if parsed < 0:
        raise ValueError(f"{label} cannot be negative.")
    return parsed


def parse_positive_int(value: object, label: str) -> int:
    parsed = parse_nonnegative_int(value, label)
    if parsed <= 0:
        raise ValueError(f"{label} must be greater than zero.")
    return parsed


def parse_boolean(value: object, label: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    raise ValueError(f"{label} must be true or false.")


def parse_optional_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def safe_excel_sheet_name(sample_id: str, used_names: set[str]) -> str:
    base = re.sub(r"[:\\/?*\[\]]", "_", sample_id).strip().strip("'")
    if not base:
        base = f"Sample {len(used_names) + 1}"
    base = base[:31]
    candidate = base
    suffix_number = 2
    while candidate.lower() in used_names:
        suffix = f" {suffix_number}"
        candidate = f"{base[:31 - len(suffix)]}{suffix}"
        suffix_number += 1
    used_names.add(candidate.lower())
    return candidate


def workbook_formats(workbook: xlsxwriter.Workbook) -> dict[str, object]:
    return {
        "title": workbook.add_format({
            "bold": True,
            "font_size": 14,
        }),
        "section": workbook.add_format({
            "bold": True,
            "font_size": 11,
            "top": 1,
        }),
        "header": workbook.add_format({
            "bold": True,
            "bg_color": "#D9EAF7",
            "border": 1,
        }),
        "label": workbook.add_format({
            "bold": True,
            "bg_color": "#F3F6F8",
        }),
        "text": workbook.add_format({}),
        "text_wrap": workbook.add_format({
            "text_wrap": True,
            "valign": "top",
        }),
        "integer": workbook.add_format({
            "num_format": "0",
        }),
        "number_2": workbook.add_format({
            "num_format": "0.00",
        }),
        "number_3": workbook.add_format({
            "num_format": "0.000",
        }),
        "number_4": workbook.add_format({
            "num_format": "0.0000",
        }),
        "number_5": workbook.add_format({
            "num_format": "0.00000",
        }),
        "boolean": workbook.add_format({}),
    }


def write_summary_worksheet(
    workbook: xlsxwriter.Workbook,
    records: list[TestSampleRecord],
    formats: dict[str, object],
) -> None:
    worksheet = workbook.add_worksheet("Summary")
    worksheet.write(0, 0, "Tensile Sample Set Summary", formats["title"])
    worksheet.write(1, 0, "Generated At", formats["label"])
    worksheet.write(
        1,
        1,
        datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        formats["text"],
    )

    worksheet.write(3, 0, "Statistics (included complete samples)", formats["section"])
    stats_header = ["Metric", "Count", "Average", "Min", "Max", "Std Dev"]
    for column_index, heading in enumerate(stats_header):
        worksheet.write(4, column_index, heading, formats["header"])

    write_stat_row(
        worksheet,
        5,
        "Peak Force (N)",
        workbook_stat_values(records, "peak_force_n"),
        formats,
    )
    write_stat_row(
        worksheet,
        6,
        "Final Displacement (mm)",
        workbook_stat_values(records, "final_position_mm"),
        formats,
    )

    table_title_row = 8
    table_header_row = 9
    data_start_row = table_header_row + 1
    worksheet.write(table_title_row, 0, "Samples", formats["section"])
    for column_index, field in enumerate(SUMMARY_WORKBOOK_FIELDNAMES):
        worksheet.write(table_header_row, column_index, field, formats["header"])

    for row_offset, record in enumerate(records):
        values = summary_workbook_row(record)
        row_index = data_start_row + row_offset
        for column_index, field in enumerate(SUMMARY_WORKBOOK_FIELDNAMES):
            write_workbook_value(
                worksheet,
                row_index,
                column_index,
                values[field],
                summary_workbook_cell_format(field, formats),
            )

    if records:
        worksheet.autofilter(
            table_header_row,
            0,
            data_start_row + len(records) - 1,
            len(SUMMARY_WORKBOOK_FIELDNAMES) - 1,
        )
    else:
        worksheet.write(data_start_row, 0, "No samples recorded", formats["text"])
        worksheet.autofilter(
            table_header_row,
            0,
            table_header_row,
            len(SUMMARY_WORKBOOK_FIELDNAMES) - 1,
        )

    chart_records = sample_chart_records(records)
    chart_header_row = table_header_row
    chart_data_row = data_start_row
    chart_columns = (24, 25, 26)
    for column_index, heading in zip(
        chart_columns,
        ["chart_sample", "chart_peak_force_n", "chart_final_displacement_mm"],
    ):
        worksheet.write(chart_header_row, column_index, heading, formats["header"])
    for row_offset, record in enumerate(chart_records):
        row_index = chart_data_row + row_offset
        worksheet.write(row_index, chart_columns[0], record.sample_id, formats["text"])
        write_workbook_value(
            worksheet,
            row_index,
            chart_columns[1],
            record.peak_force_n,
            formats["number_4"],
        )
        write_workbook_value(
            worksheet,
            row_index,
            chart_columns[2],
            record.final_position_mm,
            formats["number_5"],
        )
    worksheet.set_column(chart_columns[0], chart_columns[-1], None, None, {"hidden": True})

    if chart_records:
        last_chart_row = chart_data_row + len(chart_records) - 1
        peak_chart = workbook.add_chart({"type": "column"})
        peak_chart.add_series({
            "name": "Peak Force (N)",
            "categories": ["Summary", chart_data_row, chart_columns[0], last_chart_row, chart_columns[0]],
            "values": ["Summary", chart_data_row, chart_columns[1], last_chart_row, chart_columns[1]],
            "fill": {"color": "#4472C4"},
        })
        peak_chart.set_title({"name": "Peak Force by Sample"})
        peak_chart.set_x_axis({"name": "Sample"})
        peak_chart.set_y_axis({"name": "Force (N)"})
        peak_chart.set_legend({"none": True})
        worksheet.insert_chart(1, 12, peak_chart, {"x_scale": 1.15, "y_scale": 1.0})

        displacement_chart = workbook.add_chart({"type": "line"})
        displacement_chart.add_series({
            "name": "Final Displacement (mm)",
            "categories": ["Summary", chart_data_row, chart_columns[0], last_chart_row, chart_columns[0]],
            "values": ["Summary", chart_data_row, chart_columns[2], last_chart_row, chart_columns[2]],
            "line": {"color": "#70AD47", "width": 2.25},
            "marker": {"type": "circle", "size": 5},
        })
        displacement_chart.set_title({"name": "Final Displacement by Sample"})
        displacement_chart.set_x_axis({"name": "Sample"})
        displacement_chart.set_y_axis({"name": "Displacement (mm)"})
        displacement_chart.set_legend({"none": True})
        worksheet.insert_chart(17, 12, displacement_chart, {"x_scale": 1.15, "y_scale": 1.0})
    else:
        worksheet.write(1, 12, "No samples to chart.", formats["text"])

    worksheet.freeze_panes(data_start_row, 0)
    worksheet.set_column(0, 0, 12)
    worksheet.set_column(1, 1, 16)
    worksheet.set_column(2, 2, 28)
    worksheet.set_column(3, 6, 14)
    worksheet.set_column(7, 10, 18)


def write_method_worksheet(
    workbook: xlsxwriter.Workbook,
    method_snapshot: dict[str, object],
    formats: dict[str, object],
) -> None:
    worksheet = workbook.add_worksheet("Method")
    worksheet.write(0, 0, "Test Method", formats["title"])
    worksheet.set_column(0, 0, 28)
    worksheet.set_column(1, 1, 24)
    worksheet.set_column(2, 5, 18)

    if not method_snapshot:
        worksheet.write(2, 0, "No method saved with this sample set", formats["text"])
        return

    method_hash_value = str(method_snapshot.get("hash") or method_hash(method_snapshot))
    metadata = [
        ("Method ID", method_snapshot.get("id") or ""),
        ("Method Name", method_snapshot.get("name") or ""),
        ("Method Hash", method_hash_value),
        ("Schema Version", method_snapshot.get("schema_version") or ""),
    ]
    for row_offset, (label, value) in enumerate(metadata, start=2):
        worksheet.write(row_offset, 0, label, formats["label"])
        write_workbook_value(worksheet, row_offset, 1, value, formats["text"])

    row_index = 8
    row_index = write_method_mapping_section(
        worksheet,
        row_index,
        "Motion Settings",
        method_snapshot.get("motion"),
        [
            "jog_speed_steps_s",
            "test_max_step_rate_steps_s",
            "acceleration_steps_s2",
        ],
        formats,
    )
    row_index = write_method_mapping_section(
        worksheet,
        row_index + 1,
        "Initialization",
        method_snapshot.get("initialization"),
        [
            "mode",
            "preload_force_n",
            "unload_force_n",
            "rate_mm_s",
            "max_travel_mm",
        ],
        formats,
    )

    steps = method_snapshot.get("steps")
    step_rows = steps if isinstance(steps, list) else []
    row_index += 2
    worksheet.write(row_index, 0, "Steps", formats["section"])
    row_index += 1
    step_fields = [
        "step",
        "target_type",
        "target_value",
        "rate_type",
        "rate_value_per_s",
        "hold_duration_s",
    ]
    for column_index, field in enumerate(step_fields):
        worksheet.write(row_index, column_index, field, formats["header"])

    for step_offset, raw_step in enumerate(step_rows, start=1):
        raw = raw_step if isinstance(raw_step, dict) else {}
        values = {
            "step": step_offset,
            "target_type": raw.get("target_type") or "",
            "target_value": raw.get("target_value") or "",
            "rate_type": raw.get("rate_type") or "",
            "rate_value_per_s": raw.get("rate_value_per_s") or "",
            "hold_duration_s": raw.get("hold_duration_s") or "",
        }
        for column_index, field in enumerate(step_fields):
            cell_format = formats["integer"] if field == "step" else workbook_value_format(
                values[field], formats)
            write_workbook_value(
                worksheet,
                row_index + step_offset,
                column_index,
                values[field],
                cell_format,
            )
    if step_rows:
        worksheet.autofilter(row_index, 0, row_index + len(step_rows), len(step_fields) - 1)
    else:
        worksheet.write(row_index + 1, 0, "No steps saved with this method", formats["text"])


def write_method_mapping_section(
    worksheet: xlsxwriter.worksheet.Worksheet,
    row_index: int,
    title: str,
    raw_mapping: object,
    keys: list[str],
    formats: dict[str, object],
) -> int:
    mapping = raw_mapping if isinstance(raw_mapping, dict) else {}
    worksheet.write(row_index, 0, title, formats["section"])
    row_index += 1
    worksheet.write(row_index, 0, "Setting", formats["header"])
    worksheet.write(row_index, 1, "Value", formats["header"])
    for key in keys:
        row_index += 1
        worksheet.write(row_index, 0, key, formats["label"])
        value = mapping.get(key, "")
        write_workbook_value(
            worksheet,
            row_index,
            1,
            value,
            workbook_value_format(value, formats),
        )
    return row_index


def write_sample_worksheet(
    worksheet: xlsxwriter.worksheet.Worksheet,
    record: TestSampleRecord,
    formats: dict[str, object],
) -> None:
    metadata_rows = [
        ("sample_index", record.index),
        ("sample_id", record.sample_id),
        ("sample_notes", record.notes),
        ("sample_status", record.status),
        ("sample_included", 1 if record.included else 0),
        ("method_id", record.method_id),
        ("method_name", record.method_name),
        ("method_hash", record.method_hash),
        ("method_snapshot_json", json.dumps(record.method_snapshot, sort_keys=True)),
    ]
    for row_index, (label, value) in enumerate(metadata_rows):
        worksheet.write(row_index, 0, label, formats["label"])
        value_format = formats["text_wrap"] if label == "method_snapshot_json" else workbook_value_format(
            value, formats)
        write_workbook_value(worksheet, row_index, 1, value, value_format)

    header_row = 10
    for column_index, field in enumerate(SAMPLE_WORKBOOK_FIELDNAMES):
        worksheet.write(header_row, column_index, field, formats["header"])
        worksheet.set_column(
            column_index,
            column_index,
            sample_workbook_column_width(field),
        )
    worksheet.set_column(1, 1, 22)

    for row_offset, sample in enumerate(record.samples, start=1):
        for column_index, field in enumerate(SAMPLE_WORKBOOK_FIELDNAMES):
            value = sample_workbook_value(field, sample.get(field, ""))
            write_workbook_value(
                worksheet,
                header_row + row_offset,
                column_index,
                value,
                sample_workbook_cell_format(field, formats),
            )

    last_row = header_row + max(1, len(record.samples))
    worksheet.autofilter(header_row, 0, last_row, len(SAMPLE_WORKBOOK_FIELDNAMES) - 1)
    worksheet.freeze_panes(header_row + 1, 0)


def write_plots_worksheet(
    workbook: xlsxwriter.Workbook,
    records: list[TestSampleRecord],
    formats: dict[str, object],
) -> None:
    worksheet = workbook.add_worksheet("Plots")
    worksheet.write(0, 0, "Force-Displacement Overlay", formats["title"])
    worksheet.write(
        1,
        0,
        f"Charts use up to {WORKBOOK_FORCE_DISPLACEMENT_MAX_POINTS:,} points per sample.",
        formats["text"],
    )

    chart = workbook.add_chart({"type": "scatter", "subtype": "straight"})
    series_count = 0
    column_index = 0
    for record in records:
        points = workbook_force_displacement_points(record.samples)
        sampled_points = sampled_workbook_points(
            points,
            WORKBOOK_FORCE_DISPLACEMENT_MAX_POINTS,
        )
        if not sampled_points:
            continue

        worksheet.write(3, column_index, f"{record.sample_id} position_mm", formats["header"])
        worksheet.write(3, column_index + 1, f"{record.sample_id} force_n", formats["header"])
        for row_offset, point in enumerate(sampled_points, start=4):
            worksheet.write_number(row_offset, column_index, point[0], formats["number_5"])
            worksheet.write_number(row_offset, column_index + 1, point[1], formats["number_4"])

        last_row = 3 + len(sampled_points)
        chart.add_series({
            "name": record.sample_id,
            "categories": ["Plots", 4, column_index, last_row, column_index],
            "values": ["Plots", 4, column_index + 1, last_row, column_index + 1],
            "marker": {"type": "none"},
        })
        worksheet.set_column(column_index, column_index + 1, 18)
        column_index += 2
        series_count += 1

    if series_count:
        chart.set_title({"name": "Force vs Displacement"})
        chart.set_x_axis({"name": "Displacement (mm)"})
        chart.set_y_axis({"name": "Force (N)"})
        worksheet.insert_chart(3, max(4, column_index + 1), chart, {
            "x_scale": 1.45,
            "y_scale": 1.3,
        })
        worksheet.freeze_panes(4, 0)
    else:
        worksheet.write(3, 0, "No sample telemetry to plot.", formats["text"])
        worksheet.set_column(0, 0, 34)


def write_stat_row(
    worksheet: xlsxwriter.worksheet.Worksheet,
    row_index: int,
    label: str,
    stats: dict[str, float | int],
    formats: dict[str, object],
) -> None:
    worksheet.write(row_index, 0, label, formats["label"])
    write_workbook_value(worksheet, row_index, 1, stats["count"], formats["integer"])
    for column_index, key in enumerate(["average", "min", "max", "std_dev"], start=2):
        write_workbook_value(worksheet, row_index, column_index, stats[key], formats["number_4"])


def summary_workbook_row(record: TestSampleRecord) -> dict[str, object]:
    return {
        "sample_index": record.index,
        "sample_id": record.sample_id,
        "notes": record.notes,
        "included": record.included,
        "status": record.status,
        "method_name": record.method_name,
        "point_count": record.point_count,
        "peak_force_n": record.peak_force_n,
        "peak_force_position_mm": record.peak_force_position_mm,
        "final_force_n": record.final_force_n,
        "final_position_mm": record.final_position_mm,
    }


def included_complete_records(records: list[TestSampleRecord]) -> list[TestSampleRecord]:
    return [
        record
        for record in records
        if record.included and record.status == "COMPLETE"
    ]


def sample_chart_records(records: list[TestSampleRecord]) -> list[TestSampleRecord]:
    return [
        record
        for record in records
        if all(
            isinstance(value, (int, float)) and math.isfinite(float(value))
            for value in (record.peak_force_n, record.final_position_mm)
        )
    ]


def workbook_stat_values(
    records: list[TestSampleRecord],
    field: str,
) -> dict[str, float | int]:
    values = [
        value
        for record in included_complete_records(records)
        if isinstance((value := getattr(record, field)), (int, float))
        and math.isfinite(float(value))
    ]
    if not values:
        return {
            "count": 0,
            "average": 0.0,
            "min": 0.0,
            "max": 0.0,
            "std_dev": 0.0,
        }
    average = sum(values) / len(values)
    return {
        "count": len(values),
        "average": average,
        "min": min(values),
        "max": max(values),
        "std_dev": sample_standard_deviation(values, average),
    }


def sample_standard_deviation(values: list[float], average: float) -> float:
    if len(values) < 2:
        return 0.0
    variance = sum((value - average) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(variance)


def workbook_force_displacement_points(
    samples: list[dict[str, object]],
) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for sample in samples:
        position = parse_optional_float(sample.get("position_mm"))
        force = parse_optional_float(sample.get("force_n"))
        if position is None or force is None:
            continue
        points.append((position, force))
    return points


def sampled_workbook_points(
    points: list[tuple[float, float]],
    max_points: int,
) -> list[tuple[float, float]]:
    if len(points) <= max_points:
        return points
    if max_points <= 1:
        return points[:1]
    last_index = len(points) - 1
    return [
        points[round(index * last_index / (max_points - 1))]
        for index in range(max_points)
    ]


def write_workbook_value(
    worksheet: xlsxwriter.worksheet.Worksheet,
    row_index: int,
    column_index: int,
    value: object,
    cell_format: object = None,
) -> None:
    if value is None or value == "":
        worksheet.write_blank(row_index, column_index, None, cell_format)
    elif isinstance(value, bool):
        worksheet.write_boolean(row_index, column_index, value, cell_format)
    elif isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            worksheet.write_blank(row_index, column_index, None, cell_format)
        else:
            worksheet.write_number(row_index, column_index, value, cell_format)
    else:
        worksheet.write(row_index, column_index, str(value), cell_format)


def workbook_value_format(value: object, formats: dict[str, object]) -> object:
    if isinstance(value, bool):
        return formats["boolean"]
    if isinstance(value, int):
        return formats["integer"]
    if isinstance(value, float):
        return formats["number_4"]
    return formats["text"]


def summary_workbook_cell_format(field: str, formats: dict[str, object]) -> object:
    if field not in SUMMARY_WORKBOOK_NUMERIC_FIELDS:
        return formats["boolean"] if field == "included" else formats["text"]
    if field in {"sample_index", "point_count"}:
        return formats["integer"]
    if field in {"peak_force_position_mm", "final_position_mm"}:
        return formats["number_5"]
    return formats["number_4"]


def sample_workbook_cell_format(field: str, formats: dict[str, object]) -> object:
    if field not in SAMPLE_WORKBOOK_NUMERIC_FIELDS:
        return formats["text"]
    if field in {"controller_time_ms", "run_id", "step_index"}:
        return formats["integer"]
    if field == "wall_time_s":
        return formats["number_3"]
    if field == "step_rate_steps_s":
        return formats["number_2"]
    if field in {"position_mm", "setpoint_displacement_mm"}:
        return formats["number_5"]
    return formats["number_4"]


def sample_workbook_column_width(field: str) -> int:
    widths = {
        "wall_time_s": 12,
        "controller_time_ms": 18,
        "run_id": 10,
        "frame_mode": 14,
        "step_index": 12,
        "phase": 16,
        "fault_reason": 16,
        "control_mode": 14,
        "setpoint_force_n": 18,
        "setpoint_displacement_mm": 24,
        "force_n": 14,
        "position_mm": 14,
        "step_rate_steps_s": 18,
    }
    return widths.get(field, 14)


def sample_workbook_value(field: str, value: object) -> object:
    if field not in SAMPLE_WORKBOOK_NUMERIC_FIELDS:
        return value
    if value is None or value == "":
        return ""
    parsed = parse_optional_float(value)
    if parsed is None:
        return value
    if field in {"controller_time_ms", "run_id", "step_index"} and parsed.is_integer():
        return int(parsed)
    return parsed


def parse_finite_float(value: object, label: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a number.") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{label} must be finite.")
    return parsed


def parse_test_type(value: object, allowed: set[str], label: str) -> str:
    parsed = str(value or "").strip().upper()
    if parsed not in allowed:
        choices = " or ".join(sorted(allowed))
        raise ValueError(f"{label} must be {choices}.")
    return parsed


def clamp_float(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))
