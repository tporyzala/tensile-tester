from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.database.models import TestMethod, TestStep


VALID_STEP_TYPES = {"RAMP_TO_LOAD", "HOLD_LOAD"}


def list_methods(db: Session) -> list[TestMethod]:
    statement = select(TestMethod).options(selectinload(TestMethod.steps)).order_by(TestMethod.name.asc())
    return list(db.scalars(statement).all())


def get_method(db: Session, method_id: int) -> TestMethod | None:
    statement = (
        select(TestMethod)
        .options(selectinload(TestMethod.steps))
        .where(TestMethod.id == method_id)
    )
    return db.scalar(statement)


def create_method(db: Session, name: str, description: str | None) -> TestMethod:
    normalized_name = name.strip()
    if not normalized_name:
        raise ValueError("Method name is required.")
    method = TestMethod(name=normalized_name, description=description.strip() if description else None)
    db.add(method)
    db.commit()
    db.refresh(method)
    return method


def add_step(
    db: Session,
    method_id: int,
    step_type: str,
    target_force_n: float,
    rate_n_per_s: float | None,
    timeout_s: float | None,
    duration_s: float | None,
) -> TestStep:
    normalized_type = step_type.upper()
    if normalized_type not in VALID_STEP_TYPES:
        raise ValueError("Unsupported step type.")
    if normalized_type == "RAMP_TO_LOAD":
        if rate_n_per_s is None or rate_n_per_s <= 0:
            raise ValueError("Ramp steps require a positive rate.")
        if timeout_s is None or timeout_s <= 0:
            raise ValueError("Ramp steps require a positive timeout.")
    if normalized_type == "HOLD_LOAD":
        if duration_s is None or duration_s <= 0:
            raise ValueError("Hold steps require a positive duration.")

    method = get_method(db, method_id)
    if method is None:
        raise ValueError("Method not found.")

    last_position = db.scalar(select(func.max(TestStep.position)).where(TestStep.method_id == method_id))
    position = int(last_position or 0) + 1
    step = TestStep(
        method_id=method_id,
        position=position,
        step_type=normalized_type,
        target_force_n=target_force_n,
        rate_n_per_s=rate_n_per_s if normalized_type == "RAMP_TO_LOAD" else None,
        timeout_s=timeout_s if normalized_type == "RAMP_TO_LOAD" else None,
        duration_s=duration_s if normalized_type == "HOLD_LOAD" else None,
    )
    db.add(step)
    db.commit()
    db.refresh(step)
    return step
