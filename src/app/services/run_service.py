from __future__ import annotations

import csv
import io
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.database.models import Run, SampleMetadata, TelemetryPoint
from app.services.method_service import get_method


def build_run_name(method_name: str, sample_name: str) -> str:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_method = "_".join(method_name.split())
    safe_sample = "_".join(sample_name.split())
    return f"{safe_method}_{safe_sample}_{timestamp}"


def arm_run(db: Session, method_id: int, sample_name: str, notes: str | None) -> Run:
    method = get_method(db, method_id)
    if method is None:
        raise ValueError("Method not found.")
    if not method.steps:
        raise ValueError("Method must contain at least one step before arming.")

    sample = SampleMetadata(sample_name=sample_name.strip(), notes=notes.strip() if notes else None)
    db.add(sample)
    db.flush()

    run = Run(
        method_id=method.id,
        sample_id=sample.id,
        run_name=build_run_name(method.name, sample.sample_name),
        status="ARMED",
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def mark_run_started(db: Session, run_id: int) -> None:
    run = db.get(Run, run_id)
    if run is None:
        return
    run.status = "RUNNING"
    run.started_at = run.started_at or datetime.utcnow()
    db.add(run)
    db.commit()


def mark_run_terminal(db: Session, run_id: int, status: str, detail: str | None = None) -> None:
    run = db.get(Run, run_id)
    if run is None:
        return
    run.status = status
    run.completed_at = datetime.utcnow()
    run.completion_detail = detail
    db.add(run)
    db.commit()


def append_telemetry(
    db: Session,
    run_id: int,
    seq: int,
    machine_time_ms: int,
    machine_state: str,
    raw_adc: int,
    force_n: float,
    target_force_n: float,
    step_rate_steps_s: float,
    estimated_crosshead_mm: float,
) -> None:
    point = TelemetryPoint(
        run_id=run_id,
        seq=seq,
        machine_time_ms=machine_time_ms,
        machine_state=machine_state,
        raw_adc=raw_adc,
        force_n=force_n,
        target_force_n=target_force_n,
        step_rate_steps_s=step_rate_steps_s,
        estimated_crosshead_mm=estimated_crosshead_mm,
    )
    db.add(point)
    db.commit()


def list_runs(db: Session) -> list[Run]:
    statement = (
        select(Run)
        .options(selectinload(Run.method), selectinload(Run.sample))
        .order_by(Run.created_at.desc())
    )
    return list(db.scalars(statement).all())


def get_run(db: Session, run_id: int) -> Run | None:
    statement = (
        select(Run)
        .options(
            selectinload(Run.method),
            selectinload(Run.sample),
            selectinload(Run.telemetry),
        )
        .where(Run.id == run_id)
    )
    return db.scalar(statement)


def export_run_csv(db: Session, run_id: int) -> tuple[str, str]:
    run = get_run(db, run_id)
    if run is None:
        raise ValueError("Run not found.")

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "seq",
            "machine_time_ms",
            "machine_state",
            "raw_adc",
            "force_n",
            "target_force_n",
            "step_rate_steps_s",
            "estimated_crosshead_mm",
            "captured_at_utc",
        ]
    )
    for point in run.telemetry:
        writer.writerow(
            [
                point.seq,
                point.machine_time_ms,
                point.machine_state,
                point.raw_adc,
                f"{point.force_n:.6f}",
                f"{point.target_force_n:.6f}",
                f"{point.step_rate_steps_s:.6f}",
                f"{point.estimated_crosshead_mm:.6f}",
                point.captured_at.isoformat(),
            ]
        )
    return f"{run.run_name}.csv", output.getvalue()

