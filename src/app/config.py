from __future__ import annotations

import os
import platform
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parent
SRC_ROOT = APP_ROOT.parent
DEFAULT_DB_PATH = SRC_ROOT / "tensile_tester.sqlite3"
DEFAULT_SERIAL_PORT = "COM3" if platform.system().lower().startswith("win") else "/dev/ttyACM0"


class AppConfig:
    database_url = os.getenv(
        "TENSILE_DATABASE_URL",
        f"sqlite:///{DEFAULT_DB_PATH.as_posix()}",
    )
    machine_transport = os.getenv("TENSILE_MACHINE_TRANSPORT", "simulated").lower()
    serial_port = os.getenv("TENSILE_SERIAL_PORT", DEFAULT_SERIAL_PORT)
    serial_baudrate = int(os.getenv("TENSILE_SERIAL_BAUDRATE", "115200"))
    telemetry_window_points = int(os.getenv("TENSILE_TELEMETRY_WINDOW_POINTS", "600"))


config = AppConfig()
