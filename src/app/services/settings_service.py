from __future__ import annotations

from sqlalchemy.orm import Session

from app.database.models import AdminSettings


SETTING_FIELDS = {
    "p_gain": float,
    "i_gain": float,
    "d_gain": float,
    "deadband_n": float,
    "overload_threshold_n": float,
    "microstepping": int,
    "jog_speed_steps_s": float,
    "max_step_rate_steps_s": float,
    "max_acceleration_steps_s2": float,
    "return_to_zero_rate_n_s": float,
    "invert_motor_direction": bool,
    "invert_load_cell_sign": bool,
}


def get_settings(db: Session) -> AdminSettings:
    settings = db.get(AdminSettings, 1)
    if settings is None:
        settings = AdminSettings(id=1)
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings


def update_settings(db: Session, values: dict[str, object]) -> AdminSettings:
    settings = get_settings(db)
    for field, caster in SETTING_FIELDS.items():
        if field not in values:
            continue
        raw_value = values[field]
        if caster is bool:
            parsed_value = bool(raw_value)
        else:
            parsed_value = caster(raw_value)
        setattr(settings, field, parsed_value)
    db.add(settings)
    db.commit()
    db.refresh(settings)
    return settings


def settings_to_protocol(settings: AdminSettings, calibration_slope: float, calibration_intercept: float) -> str:
    return ",".join(
        [
            "LOAD_CONFIG",
            f"{settings.p_gain:.6f}",
            f"{settings.i_gain:.6f}",
            f"{settings.d_gain:.6f}",
            f"{settings.deadband_n:.6f}",
            f"{settings.max_step_rate_steps_s:.6f}",
            f"{settings.max_acceleration_steps_s2:.6f}",
            f"{settings.jog_speed_steps_s:.6f}",
            f"{settings.return_to_zero_rate_n_s:.6f}",
            f"{settings.overload_threshold_n:.6f}",
            str(settings.microstepping),
            "1" if settings.invert_motor_direction else "0",
            "1" if settings.invert_load_cell_sign else "0",
            f"{calibration_slope:.9f}",
            f"{calibration_intercept:.9f}",
        ]
    )

