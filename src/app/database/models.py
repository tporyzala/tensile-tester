from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.session import Base


def utcnow() -> datetime:
    return datetime.utcnow()


class AdminSettings(Base):
    __tablename__ = "admin_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    p_gain: Mapped[float] = mapped_column(Float, default=0.8)
    i_gain: Mapped[float] = mapped_column(Float, default=0.04)
    d_gain: Mapped[float] = mapped_column(Float, default=0.01)
    deadband_n: Mapped[float] = mapped_column(Float, default=1.0)
    overload_threshold_n: Mapped[float] = mapped_column(Float, default=1000.0)
    microstepping: Mapped[int] = mapped_column(Integer, default=4)
    jog_speed_steps_s: Mapped[float] = mapped_column(Float, default=500.0)
    max_step_rate_steps_s: Mapped[float] = mapped_column(Float, default=2200.0)
    max_acceleration_steps_s2: Mapped[float] = mapped_column(Float, default=4000.0)
    return_to_zero_rate_n_s: Mapped[float] = mapped_column(Float, default=50.0)
    invert_motor_direction: Mapped[bool] = mapped_column(Boolean, default=False)
    invert_load_cell_sign: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Calibration(Base):
    __tablename__ = "calibrations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    slope: Mapped[float] = mapped_column(Float, default=0.001)
    intercept: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class TestMethod(Base):
    __tablename__ = "test_methods"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(160), unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    steps: Mapped[list["TestStep"]] = relationship(
        back_populates="method",
        cascade="all, delete-orphan",
        order_by="TestStep.position",
    )
    runs: Mapped[list["Run"]] = relationship(back_populates="method")


class TestStep(Base):
    __tablename__ = "test_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    method_id: Mapped[int] = mapped_column(ForeignKey("test_methods.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer)
    step_type: Mapped[str] = mapped_column(String(32))
    target_force_n: Mapped[float] = mapped_column(Float)
    rate_n_per_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    timeout_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)

    method: Mapped[TestMethod] = relationship(back_populates="steps")


class SampleMetadata(Base):
    __tablename__ = "sample_metadata"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sample_name: Mapped[str] = mapped_column(String(160))
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    runs: Mapped[list["Run"]] = relationship(back_populates="sample")


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    method_id: Mapped[int] = mapped_column(ForeignKey("test_methods.id"))
    sample_id: Mapped[int] = mapped_column(ForeignKey("sample_metadata.id"))
    run_name: Mapped[str] = mapped_column(String(255), unique=True)
    status: Mapped[str] = mapped_column(String(32), default="ARMED")
    tare_offset_n: Mapped[float] = mapped_column(Float, default=0.0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completion_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    method: Mapped[TestMethod] = relationship(back_populates="runs")
    sample: Mapped[SampleMetadata] = relationship(back_populates="runs")
    telemetry: Mapped[list["TelemetryPoint"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="TelemetryPoint.seq",
    )
    faults: Mapped[list["FaultLog"]] = relationship(back_populates="run")


class TelemetryPoint(Base):
    __tablename__ = "telemetry_points"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer)
    machine_time_ms: Mapped[int] = mapped_column(Integer)
    machine_state: Mapped[str] = mapped_column(String(40))
    raw_adc: Mapped[int] = mapped_column(Integer)
    force_n: Mapped[float] = mapped_column(Float)
    target_force_n: Mapped[float] = mapped_column(Float)
    step_rate_steps_s: Mapped[float] = mapped_column(Float)
    estimated_crosshead_mm: Mapped[float] = mapped_column(Float)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    run: Mapped[Run] = relationship(back_populates="telemetry")


class FaultLog(Base):
    __tablename__ = "fault_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"), nullable=True)
    state: Mapped[str] = mapped_column(String(40))
    code: Mapped[str] = mapped_column(String(80))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    run: Mapped[Run | None] = relationship(back_populates="faults")

