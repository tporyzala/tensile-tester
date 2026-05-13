from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import select

SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from app.database.bootstrap import ensure_singletons
from app.database.models import Base
from app.database.models import TestMethod
from app.database.session import SessionLocal, engine
from app.services.method_service import add_step, create_method


def main() -> None:
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        ensure_singletons(db)
        method = get_method_by_name(db, "Compression Ramp Hold Demo")
        if method is not None:
            print("Demo method already exists.")
            return

        method = create_method(
            db,
            "Compression Ramp Hold Demo",
            "Example method: ramp to -250 N, hold briefly, and return automatically.",
        )
        add_step(db, method.id, "RAMP_TO_LOAD", -250.0, 50.0, 20.0, None)
        add_step(db, method.id, "HOLD_LOAD", -250.0, None, None, 5.0)
        print(f"Created demo method {method.name}.")


def get_method_by_name(db, name: str) -> TestMethod | None:
    return db.scalar(select(TestMethod).where(TestMethod.name == name))


if __name__ == "__main__":
    main()
