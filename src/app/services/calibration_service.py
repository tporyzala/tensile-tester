from __future__ import annotations

from sqlalchemy.orm import Session

from app.database.models import Calibration


def get_calibration(db: Session) -> Calibration:
    calibration = db.get(Calibration, 1)
    if calibration is None:
        calibration = Calibration(id=1)
        db.add(calibration)
        db.commit()
        db.refresh(calibration)
    return calibration


def update_calibration(db: Session, slope: float, intercept: float) -> Calibration:
    calibration = get_calibration(db)
    calibration.slope = slope
    calibration.intercept = intercept
    db.add(calibration)
    db.commit()
    db.refresh(calibration)
    return calibration

