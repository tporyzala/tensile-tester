import asyncio
import math
import unittest

from app.calibration import CalibrationSample, fit_load_cell_calibration
from app.main import (
    AppConfig,
    CalibrationCommandError,
    SerialMonitor,
    TestRunState,
    parse_calibration_points,
)


def point(force, raw):
    return CalibrationSample(
        reference_force_n=force,
        raw_adc_mean=raw,
    )


def ready_monitor():
    monitor = SerialMonitor(AppConfig())
    monitor.snapshot.connected = True
    monitor.snapshot.frame_mode = "SETUP"
    monitor.snapshot.test_phase = "NONE"
    monitor.snapshot.fault_reason = "NONE"
    monitor.snapshot.step_rate_steps_s = 0.0
    monitor._test_state = TestRunState(status="IDLE")
    return monitor


class CalibrationFitTests(unittest.TestCase):
    def test_exact_linear_data_produces_constants(self):
        fit = fit_load_cell_calibration([
            point(0.0, 1000.0),
            point(100.0, 2000.0),
            point(200.0, 3000.0),
        ])

        self.assertAlmostEqual(fit["slope_n_per_count"], 0.1)
        self.assertAlmostEqual(fit["intercept_n"], -100.0)
        self.assertEqual(fit["zero_raw_adc_mean"], 1000.0)
        self.assertAlmostEqual(fit["rms_error_n"], 0.0)
        self.assertAlmostEqual(fit["max_abs_error_n"], 0.0)
        self.assertIn("CalibrationSlopeNPerCount", fit["constants_block"])
        self.assertIn("InvertSign = false", fit["constants_block"])

    def test_negative_raw_count_direction_produces_negative_slope(self):
        fit = fit_load_cell_calibration([
            point(0.0, 1000.0),
            point(100.0, 500.0),
        ])

        self.assertAlmostEqual(fit["slope_n_per_count"], -0.2)
        self.assertAlmostEqual(fit["intercept_n"], 200.0)

    def test_residual_statistics_are_reported(self):
        fit = fit_load_cell_calibration([
            point(0.0, 1000.0),
            point(100.0, 2000.0),
            point(220.0, 3000.0),
        ])

        self.assertAlmostEqual(fit["slope_n_per_count"], 0.108)
        self.assertAlmostEqual(fit["residuals"][1]["residual_force_n"], 8.0)
        self.assertAlmostEqual(fit["residuals"][2]["residual_force_n"], -4.0)
        self.assertAlmostEqual(fit["rms_error_n"], math.sqrt(80.0 / 3.0))
        self.assertAlmostEqual(fit["max_abs_error_n"], 8.0)
        self.assertAlmostEqual(fit["max_percent_span_error"], 8.0 / 220.0 * 100.0)

    def test_fit_rejects_missing_zero(self):
        with self.assertRaises(ValueError):
            fit_load_cell_calibration([
                point(50.0, 1500.0),
                point(100.0, 2000.0),
            ])

    def test_fit_rejects_duplicate_raw_adc_means(self):
        with self.assertRaises(ValueError):
            fit_load_cell_calibration([
                point(0.0, 1000.0),
                point(100.0, 1000.0),
            ])

    def test_fit_rejects_too_few_points(self):
        with self.assertRaises(ValueError):
            fit_load_cell_calibration([point(0.0, 1000.0)])

    def test_parse_rejects_non_finite_values(self):
        with self.assertRaises(ValueError):
            parse_calibration_points({
                "points": [
                    {"reference_force_n": 0.0, "raw_adc_mean": 1000.0},
                    {"reference_force_n": float("nan"), "raw_adc_mean": 2000.0},
                ],
            })

class CalibrationSamplingTests(unittest.IsolatedAsyncioTestCase):
    async def test_sampling_averages_only_fresh_telemetry(self):
        monitor = ready_monitor()

        async def feed():
            await asyncio.sleep(0.005)
            monitor.snapshot.raw_adc = 100
            monitor.snapshot.telemetry_seq = 1
            await asyncio.sleep(0.01)
            monitor.snapshot.raw_adc = 999
            await asyncio.sleep(0.01)
            monitor.snapshot.raw_adc = 102
            monitor.snapshot.telemetry_seq = 2

        feed_task = asyncio.create_task(feed())
        sample = await monitor.sample_calibration_adc(
            0.0,
            duration_s=0.08,
            minimum_sample_count=2,
            poll_s=0.001,
        )
        await feed_task

        self.assertEqual(sample.sample_count, 2)
        self.assertAlmostEqual(sample.raw_adc_mean, 101.0)
        self.assertEqual(sample.raw_adc_min, 100)
        self.assertEqual(sample.raw_adc_max, 102)

    async def test_sampling_rejects_disconnected_machine(self):
        monitor = ready_monitor()
        monitor.snapshot.connected = False

        with self.assertRaises(CalibrationCommandError):
            await monitor.sample_calibration_adc(
                0.0,
                duration_s=0.001,
                minimum_sample_count=1,
                poll_s=0.001,
            )

    async def test_sampling_rejects_active_machine(self):
        monitor = ready_monitor()
        monitor._test_state = TestRunState(status="RUNNING")

        with self.assertRaises(CalibrationCommandError):
            await monitor.sample_calibration_adc(
                0.0,
                duration_s=0.001,
                minimum_sample_count=1,
                poll_s=0.001,
            )

    async def test_sampling_rejects_moving_machine(self):
        monitor = ready_monitor()
        monitor.snapshot.step_rate_steps_s = 1.0

        with self.assertRaises(CalibrationCommandError):
            await monitor.sample_calibration_adc(
                0.0,
                duration_s=0.001,
                minimum_sample_count=1,
                poll_s=0.001,
            )

    async def test_sampling_rejects_insufficient_fresh_samples(self):
        monitor = ready_monitor()

        with self.assertRaises(CalibrationCommandError):
            await monitor.sample_calibration_adc(
                0.0,
                duration_s=0.003,
                minimum_sample_count=1,
                poll_s=0.001,
            )

if __name__ == "__main__":
    unittest.main()
