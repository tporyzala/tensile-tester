from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class TelemetryFrame:
    seq: int
    time_ms: int
    state: str
    raw_adc: int
    force_n: float
    target_force_n: float
    step_rate_steps_s: float
    estimated_mm: float


@dataclass(slots=True)
class EventFrame:
    name: str
    details: tuple[str, ...]


@dataclass(slots=True)
class AckFrame:
    command: str


@dataclass(slots=True)
class ErrorFrame:
    code: str
    details: tuple[str, ...]


@dataclass(slots=True)
class StatusFrame:
    state: str
    configured: bool
    force_n: float
    target_force_n: float
    step_rate_steps_s: float
    estimated_mm: float


ProtocolFrame = TelemetryFrame | EventFrame | AckFrame | ErrorFrame | StatusFrame


def parse_line(line: str) -> ProtocolFrame | None:
    parts = [part.strip() for part in line.strip().split(",")]
    if not parts or not parts[0]:
        return None

    kind = parts[0].upper()
    if kind == "TEL" and len(parts) >= 9:
        return TelemetryFrame(
            seq=int(parts[1]),
            time_ms=int(parts[2]),
            state=parts[3],
            raw_adc=int(float(parts[4])),
            force_n=float(parts[5]),
            target_force_n=float(parts[6]),
            step_rate_steps_s=float(parts[7]),
            estimated_mm=float(parts[8]),
        )
    if kind == "EVENT" and len(parts) >= 2:
        return EventFrame(name=parts[1], details=tuple(parts[2:]))
    if kind == "ACK" and len(parts) >= 2:
        return AckFrame(command=parts[1])
    if kind == "ERR" and len(parts) >= 2:
        return ErrorFrame(code=parts[1], details=tuple(parts[2:]))
    if kind == "STATUS" and len(parts) >= 7:
        return StatusFrame(
            state=parts[1],
            configured=parts[2] == "1",
            force_n=float(parts[3]),
            target_force_n=float(parts[4]),
            step_rate_steps_s=float(parts[5]),
            estimated_mm=float(parts[6]),
        )
    return None

