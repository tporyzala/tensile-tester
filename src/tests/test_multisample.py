import asyncio
from io import BytesIO
import zipfile
import unittest
from unittest.mock import AsyncMock, Mock

from app.main import (
    AppConfig,
    RETURN_ZERO_DISPLACEMENT_DEFAULT_RATE_MM_S,
    ReturnZeroRequest,
    RUN_KIND_RELATIVE_MOVE,
    RUN_KIND_SPECIMEN,
    SerialMonitor,
    STEPS_PER_MM,
    TEST_SPEED_DEFAULT,
    TestRunState,
    TestSampleMetadata,
    TestSampleRecord,
    TestStep,
    parse_machine_payload,
    parse_relative_move_offset,
    parse_return_zero_request,
    parse_sample_metadata,
)


def step(target=100.0):
    return TestStep(
        target_type="FORCE",
        target_value=target,
        rate_type="FORCE",
        rate_value_per_s=10.0,
        hold_duration_s=5.0,
    )


def telemetry(force, position):
    return {
        "wall_time_s": "1.000",
        "controller_time_ms": 100,
        "run_id": 7,
        "frame_mode": "TESTING",
        "step_index": 1,
        "phase": "RAMPING",
        "fault_reason": "NONE",
        "control_mode": "FORCE",
        "setpoint_force_n": "10.0000",
        "setpoint_displacement_mm": "0.00000",
        "force_n": f"{force:.4f}",
        "position_mm": f"{position:.5f}",
        "step_rate_steps_s": "100.00",
    }


def telemetry_line(seq, controller_ms, force, position):
    fields = [
        "TEL",
        seq,
        controller_ms,
        "TESTING",
        "RAMPING",
        "NONE",
        "123",
        force,
        "100.00",
        position,
        "0",
        "0",
        "0",
        "4000.00",
        "10000.00",
        str(TEST_SPEED_DEFAULT),
        "7",
        "1",
        "1",
        "FORCE",
        "10.0000",
        "0.00000",
        controller_ms - 1000,
    ]
    return ",".join(str(field) for field in fields)


class MultiSampleTests(unittest.TestCase):
    def test_machine_payload_accepts_old_and_new_motion_fields(self):
        old_payload = [
            "SETUP", "NONE", "NONE", "12", "1.5", "0.0", "0.01",
            "0", "0", "0", "4000.0", "10000.0", "0", "0", "0",
            "NONE", "0.0", "0.0", "0",
        ]
        parsed_old = parse_machine_payload(old_payload)
        self.assertIsNone(parsed_old.test_max_step_rate_steps_s)

        new_payload = [
            "SETUP", "NONE", "NONE", "12", "1.5", "0.0", "0.01",
            "0", "0", "0", "4000.0", "10000.0", str(TEST_SPEED_DEFAULT),
            "0", "0", "0", "NONE", "0.0", "0.0", "0",
        ]
        parsed_new = parse_machine_payload(new_payload)
        self.assertEqual(parsed_new.test_max_step_rate_steps_s, TEST_SPEED_DEFAULT)

    def test_sample_metadata_defaults_and_limits(self):
        parsed = parse_sample_metadata({}, "Sample 3")
        self.assertEqual(parsed.sample_id, "Sample 3")
        self.assertEqual(parsed.notes, "")

        parsed = parse_sample_metadata(
            {"sample": {"id": " A-1 ", "notes": " first coupon "}},
            "Sample 1",
        )
        self.assertEqual(parsed.sample_id, "A-1")
        self.assertEqual(parsed.notes, "first coupon")

        with self.assertRaises(ValueError):
            parse_sample_metadata({"sample": {"id": "x" * 65}}, "Sample 1")

    def test_finalize_active_sample_summary_and_exports(self):
        monitor = SerialMonitor(AppConfig())
        monitor._test_run_kind = RUN_KIND_SPECIMEN
        monitor._active_sample = TestSampleMetadata("A-1", "valid")
        monitor._test_state = TestRunState(run_id=7, started_at=10.0)
        monitor._test_samples = [
            telemetry(2.0, 0.1),
            telemetry(-5.0, 0.4),
            telemetry(3.0, 0.6),
        ]

        monitor._finalize_active_sample("COMPLETE")

        self.assertEqual(len(monitor._sample_records), 1)
        record = monitor._sample_records[0]
        self.assertTrue(record.included)
        self.assertEqual(record.point_count, 3)
        self.assertEqual(record.peak_force_n, -5.0)
        self.assertEqual(record.peak_force_position_mm, 0.4)
        self.assertEqual(record.final_force_n, 3.0)
        self.assertEqual(record.final_position_mm, 0.6)
        workbook = monitor.sample_set_workbook()
        with zipfile.ZipFile(BytesIO(workbook)) as archive:
            workbook_xml = archive.read("xl/workbook.xml").decode("utf-8")
            shared_strings = archive.read("xl/sharedStrings.xml").decode("utf-8")
        self.assertIn('name="A-1"', workbook_xml)
        self.assertIn("sample_id", shared_strings)
        self.assertIn("A-1", shared_strings)

    def test_plot_data_retains_all_periodic_telemetry_until_clear(self):
        monitor = SerialMonitor(AppConfig())
        monitor._apply_line(telemetry_line(1, 1000, 1.0, 0.1))
        monitor._apply_line(telemetry_line(2, 1100, 2.0, 0.2))

        data = monitor.public_plot_data()
        self.assertEqual(len(data["points"]), 2)
        self.assertEqual(data["points"][0]["timeS"], 0.0)
        self.assertAlmostEqual(data["points"][1]["timeS"], 0.1)
        self.assertEqual(data["points"][1]["forceN"], 2.0)

        incremental = monitor.public_plot_data(after_index=1)
        self.assertEqual(len(incremental["points"]), 1)
        self.assertEqual(incremental["points"][0]["index"], 2)

        cleared = monitor.clear_plot_data()
        self.assertEqual(cleared["reset_id"], 1)
        self.assertEqual(cleared["points"], [])

        monitor._apply_line(telemetry_line(3, 1200, 3.0, 0.3))
        refreshed_after_clear = monitor.public_plot_data(after_index=2)
        self.assertEqual(len(refreshed_after_clear["points"]), 1)
        self.assertEqual(refreshed_after_clear["points"][0]["index"], 1)

    def test_sample_overlay_keeps_all_points(self):
        monitor = SerialMonitor(AppConfig())
        samples = [telemetry(float(index), index / 10) for index in range(405)]
        monitor._sample_records = [
            TestSampleRecord(
                index=1,
                run_id=7,
                sample_id="A-1",
                notes="",
                status="COMPLETE",
                included=True,
                started_at=1.0,
                finished_at=2.0,
                point_count=len(samples),
                peak_force_n=404.0,
                peak_force_position_mm=40.4,
                final_force_n=404.0,
                final_position_mm=40.4,
                samples=samples,
            )
        ]

        overlay = monitor.sample_overlay()
        self.assertEqual(len(overlay["series"]), 1)
        self.assertEqual(len(overlay["series"][0]["points"]), 405)

    def test_stop_retains_partial_sample_and_returns_to_idle(self):
        monitor = SerialMonitor(AppConfig())
        monitor._test_run_kind = RUN_KIND_SPECIMEN
        monitor._active_sample = TestSampleMetadata("A-2")
        monitor._test_state = TestRunState(
            run_id=8,
            status="RUNNING",
            phase="RAMPING",
            started_at=10.0,
        )
        monitor.snapshot.test_run_id = 8
        monitor.snapshot.test_phase = "RAMPING"
        monitor._test_samples = [telemetry(1.0, 0.2)]

        monitor._mark_test_stopped()

        self.assertEqual(monitor._sample_records[0].status, "STOPPED")
        self.assertFalse(monitor._sample_records[0].included)
        self.assertEqual(monitor._test_state.status, "IDLE")
        self.assertEqual(monitor._test_state.phase, "NONE")
        self.assertEqual(monitor.snapshot.test_run_id, 0)
        self.assertEqual(monitor.snapshot.test_phase, "NONE")

    def test_return_zero_request_defaults_and_steps(self):
        request = parse_return_zero_request({"mode": "displacement"})
        self.assertEqual(request.mode, "DISPLACEMENT")
        self.assertEqual(
            request.rate_value_per_s,
            RETURN_ZERO_DISPLACEMENT_DEFAULT_RATE_MM_S,
        )

        monitor = SerialMonitor(AppConfig())
        load_step = monitor._return_zero_step(ReturnZeroRequest("LOAD", 10.0))
        self.assertEqual(load_step.target_type, "FORCE")
        self.assertEqual(load_step.target_value, 0.0)
        self.assertGreater(load_step.hold_duration_s, 0.0)

        displacement_step = monitor._return_zero_step(
            ReturnZeroRequest("DISPLACEMENT", 0.02)
        )
        self.assertEqual(displacement_step.target_type, "DISPLACEMENT")
        self.assertEqual(displacement_step.hold_duration_s, 0.0)

    def test_relative_move_offset_and_step(self):
        self.assertEqual(parse_relative_move_offset({"offset_mm": 100}), 100.0)
        self.assertEqual(parse_relative_move_offset({"offset_mm": -1}), -1.0)
        with self.assertRaises(ValueError):
            parse_relative_move_offset({"offset_mm": 5})

        monitor = SerialMonitor(AppConfig())
        monitor.snapshot.position_mm = 12.5
        monitor.snapshot.test_max_step_rate_steps_s = TEST_SPEED_DEFAULT
        relative_step = monitor._relative_move_step(-10.0)
        self.assertEqual(relative_step.target_type, "DISPLACEMENT")
        self.assertEqual(relative_step.target_value, 2.5)
        self.assertEqual(relative_step.rate_type, "DISPLACEMENT")
        self.assertEqual(
            relative_step.rate_value_per_s,
            TEST_SPEED_DEFAULT / STEPS_PER_MM,
        )
        self.assertEqual(relative_step.hold_duration_s, 0.0)

    def test_relative_move_completion_and_stop_messages(self):
        monitor = SerialMonitor(AppConfig())
        monitor._test_run_kind = RUN_KIND_RELATIVE_MOVE
        monitor._test_state = TestRunState(run_id=9, status="RUNNING")
        monitor._mark_test_complete()
        self.assertEqual(monitor._test_state.message, "Relative move complete.")

        monitor._test_run_kind = RUN_KIND_RELATIVE_MOVE
        monitor._test_state = TestRunState(run_id=10, status="RUNNING")
        monitor._mark_test_stopped()
        self.assertEqual(
            monitor._test_state.message,
            "Relative move stopped; controller returned to idle.",
        )

    def test_relative_move_starts_one_displacement_step(self):
        monitor = SerialMonitor(AppConfig())
        monitor._serial = object()
        monitor.snapshot.position_mm = 2.5
        monitor.snapshot.test_max_step_rate_steps_s = TEST_SPEED_DEFAULT
        monitor._send_test_command_with_retries = AsyncMock()
        monitor._send_test_step = AsyncMock()
        monitor._ensure_test_heartbeat = Mock()

        asyncio.run(monitor.move_relative(10.0))

        self.assertEqual(monitor._test_run_kind, RUN_KIND_RELATIVE_MOVE)
        self.assertEqual(monitor._test_state.status, "RUNNING")
        self.assertEqual(monitor._test_state.message, "Moving load head +10 mm.")
        self.assertEqual(monitor._test_steps[0].target_value, 12.5)
        monitor._send_test_command_with_retries.assert_awaited_once_with(
            "START_TEST,1,1",
            "START_TEST",
            1,
        )
        monitor._send_test_step.assert_awaited_once_with(1)
        monitor._ensure_test_heartbeat.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()
