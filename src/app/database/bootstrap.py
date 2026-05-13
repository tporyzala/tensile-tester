from __future__ import annotations

from sqlalchemy.orm import Session

from app.database.models import AdminSettings, Calibration


def ensure_singletons(db: Session) -> None:
    if db.get(AdminSettings, 1) is None:
        db.add(AdminSettings(id=1))
    if db.get(Calibration, 1) is None:
        db.add(Calibration(id=1))
    db.commit()

