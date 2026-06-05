from __future__ import annotations

import math
from dataclasses import dataclass


CALIBRATION_ZERO_FORCE_TOLERANCE_N = 1.0e-9


@dataclass(slots=True)
class CalibrationSample:
    reference_force_n: float
    raw_adc_mean: float
    raw_adc_stddev: float = 0.0
    raw_adc_min: float = 0.0
    raw_adc_max: float = 0.0
    sample_count: int = 0
    duration_s: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0


def format_cpp_float(value: float) -> str:
    if abs(value) < 1.0e-12:
        value = 0.0
    text = f"{value:.9g}"
    if "e" not in text.lower() and "." not in text:
        text = f"{text}.0"
    return f"{text}f"


def calibration_constants_block(slope: float, intercept: float) -> str:
    return "\n".join([
        f"constexpr float CalibrationSlopeNPerCount = {format_cpp_float(slope)};",
        f"constexpr float CalibrationInterceptN = {format_cpp_float(intercept)};",
        "constexpr bool InvertSign = false;",
    ])


def fit_load_cell_calibration(points: list[CalibrationSample]) -> dict[str, object]:
    if len(points) < 2:
        raise ValueError("Calibration fit requires a zero point and at least one load point.")

    zero_point = next(
        (
            point for point in points
            if abs(point.reference_force_n) <= CALIBRATION_ZERO_FORCE_TOLERANCE_N
        ),
        None,
    )
    if zero_point is None:
        raise ValueError("Calibration fit requires a zero-force point.")

    loaded_points = [
        point for point in points
        if abs(point.reference_force_n) > CALIBRATION_ZERO_FORCE_TOLERANCE_N
    ]
    if not loaded_points:
        raise ValueError("Calibration fit requires at least one non-zero load point.")

    zero_raw_adc = zero_point.raw_adc_mean
    denominator = 0.0
    numerator = 0.0
    for point in points:
        delta_adc = point.raw_adc_mean - zero_raw_adc
        denominator += delta_adc * delta_adc
        numerator += delta_adc * point.reference_force_n

    if denominator <= 0.0:
        raise ValueError("Calibration load points must have raw ADC values distinct from zero.")

    slope = numerator / denominator
    if not math.isfinite(slope):
        raise ValueError("Calibration slope is not finite.")
    intercept = -slope * zero_raw_adc

    residuals: list[dict[str, float | int]] = []
    squared_error_sum = 0.0
    max_abs_error = 0.0
    for index, point in enumerate(points, start=1):
        predicted_force = slope * (point.raw_adc_mean - zero_raw_adc)
        residual = predicted_force - point.reference_force_n
        squared_error_sum += residual * residual
        max_abs_error = max(max_abs_error, abs(residual))
        residuals.append({
            "index": index,
            "reference_force_n": point.reference_force_n,
            "raw_adc_mean": point.raw_adc_mean,
            "predicted_force_n": predicted_force,
            "residual_force_n": residual,
        })

    rms_error = math.sqrt(squared_error_sum / len(points))
    reference_forces = [point.reference_force_n for point in points]
    force_span = max(reference_forces) - min(reference_forces)
    max_percent_span_error = (
        (max_abs_error / force_span) * 100.0 if force_span > 0.0 else 0.0
    )

    return {
        "slope_n_per_count": slope,
        "intercept_n": intercept,
        "zero_raw_adc_mean": zero_raw_adc,
        "invert_sign": False,
        "rms_error_n": rms_error,
        "max_abs_error_n": max_abs_error,
        "force_span_n": force_span,
        "max_percent_span_error": max_percent_span_error,
        "residuals": residuals,
        "constants_block": calibration_constants_block(slope, intercept),
    }
