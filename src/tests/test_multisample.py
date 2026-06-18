import asyncio
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import xml.etree.ElementTree as ET
import zipfile
import unittest
from unittest.mock import AsyncMock, Mock

from app.main import (
    AppConfig,
    INITIALIZATION_MODE_NONE,
    INITIALIZATION_MODE_PRELOAD_UNLOAD_ZERO,
    RETURN_ZERO_DISPLACEMENT_DEFAULT_RATE_MM_S,
    ReturnZeroRequest,
    RUN_KIND_INITIALIZATION,
    RUN_KIND_RELATIVE_MOVE,
    RUN_KIND_SPECIMEN,
    SerialMonitor,
    STEPS_PER_MM,
    TEST_SPEED_DEFAULT,
    TestCommandError,
    TestMethodStore,
    TestSampleSetStore,
    TestRunState,
    TestSampleMetadata,
    TestSampleRecord,
    TestStep,
    method_hash,
    parse_machine_payload,
    parse_relative_move_offset,
    parse_return_zero_request,
    parse_run_method_snapshot,
    parse_sample_metadata,
    parse_test_method,
)


def step(target=100.0):
    return TestStep(
        target_type="FORCE",
        target_value=target,
        rate_type="FORCE",
        rate_value_per_s=10.0,
        hold_duration_s=5.0,
    )


def step_payload(target=100.0):
    return {
        "target_type": "FORCE",
        "target_value": target,
        "rate_type": "FORCE",
        "rate_value_per_s": 10.0,
        "hold_duration_s": 5.0,
    }


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


def telemetry_line(
    seq,
    controller_ms,
    force,
    position,
    run_id=7,
    step_index=1,
    step_count=1,
    phase="RAMPING",
):
    fields = [
        "TEL",
        seq,
        controller_ms,
        "TESTING",
        phase,
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
        run_id,
        step_index,
        step_count,
        "FORCE",
        "10.0000",
        "0.00000",
        controller_ms - 1000,
    ]
    return ",".join(str(field) for field in fields)


WORKBOOK_NAMESPACE = {
    "sheet": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}


def workbook_sheet_names(archive):
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    return [
        sheet.attrib["name"]
        for sheet in workbook.findall(".//sheet:sheet", WORKBOOK_NAMESPACE)
    ]


def workbook_sheet_xml(archive, sheet_name):
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    rel_targets = {
        rel.attrib["Id"]: rel.attrib["Target"]
        for rel in rels.findall(".//rel:Relationship", WORKBOOK_NAMESPACE)
    }
    for sheet in workbook.findall(".//sheet:sheet", WORKBOOK_NAMESPACE):
        if sheet.attrib["name"] == sheet_name:
            rel_id = sheet.attrib[
                "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
            ]
            target = rel_targets[rel_id].lstrip("/")
            path = target if target.startswith("xl/") else f"xl/{target}"
            return archive.read(path)
    raise AssertionError(f"Workbook sheet {sheet_name!r} was not found.")


def worksheet_cells(worksheet_xml):
    worksheet = ET.fromstring(worksheet_xml)
    return {
        cell.attrib["r"]: cell
        for cell in worksheet.findall(".//sheet:c", WORKBOOK_NAMESPACE)
    }


def worksheet_number(cells, cell_ref):
    value = cells[cell_ref].find("sheet:v", WORKBOOK_NAMESPACE)
    if value is None:
        raise AssertionError(f"Cell {cell_ref} does not contain a value.")
    return float(value.text)


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

        parsed = parse_sample_metadata(
            {"sample": {"notes": "second coupon"}},
            "Sample 2",
        )
        self.assertEqual(parsed.sample_id, "Sample 2")
        self.assertEqual(parsed.notes, "second coupon")

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
        monitor._active_method_snapshot = parse_run_method_snapshot(
            {
                "method_snapshot": {
                    "id": "tpu-pull-v1",
                    "name": "TPU Pull v1",
                    "motion": {},
                    "initialization": {
                        "mode": INITIALIZATION_MODE_PRELOAD_UNLOAD_ZERO,
                        "preload_force_n": -10.0,
                        "unload_force_n": -0.5,
                        "rate_mm_s": 0.02,
                        "max_travel_mm": 2.0,
                    },
                },
            },
            [step(50.0)],
        )

        monitor._finalize_active_sample("COMPLETE")

        self.assertEqual(len(monitor._sample_records), 1)
        record = monitor._sample_records[0]
        self.assertTrue(record.included)
        self.assertEqual(record.point_count, 3)
        self.assertEqual(record.peak_force_n, -5.0)
        self.assertEqual(record.peak_force_position_mm, 0.4)
        self.assertEqual(record.final_force_n, 3.0)
        self.assertEqual(record.final_position_mm, 0.6)
        self.assertEqual(record.method_id, "tpu-pull-v1")
        self.assertEqual(record.method_name, "TPU Pull v1")
        self.assertEqual(record.method_hash, method_hash(record.method_snapshot))
        monitor._sample_records.extend([
            TestSampleRecord(
                index=2,
                run_id=8,
                sample_id="A-2",
                notes="excluded coupon",
                status="COMPLETE",
                included=False,
                started_at=20.0,
                finished_at=21.0,
                point_count=1,
                peak_force_n=99.0,
                peak_force_position_mm=9.9,
                final_force_n=90.0,
                final_position_mm=9.0,
                samples=[telemetry(99.0, 9.9)],
            ),
            TestSampleRecord(
                index=3,
                run_id=9,
                sample_id="A-3",
                notes="stopped coupon",
                status="STOPPED",
                included=True,
                started_at=30.0,
                finished_at=31.0,
                point_count=1,
                peak_force_n=75.0,
                peak_force_position_mm=7.5,
                final_force_n=70.0,
                final_position_mm=7.0,
                samples=[telemetry(75.0, 7.5)],
            ),
            TestSampleRecord(
                index=4,
                run_id=10,
                sample_id="A-4",
                notes="faulted coupon",
                status="FAULTED",
                included=False,
                started_at=40.0,
                finished_at=41.0,
                point_count=1,
                peak_force_n=55.0,
                peak_force_position_mm=5.5,
                final_force_n=50.0,
                final_position_mm=5.0,
                samples=[telemetry(55.0, 5.5)],
            ),
        ])
        workbook = monitor.sample_set_workbook()
        with zipfile.ZipFile(BytesIO(workbook)) as archive:
            sheet_names = workbook_sheet_names(archive)
            shared_strings = archive.read("xl/sharedStrings.xml").decode("utf-8")
            summary_xml = workbook_sheet_xml(archive, "Summary")
            method_xml = workbook_sheet_xml(archive, "Method")
            plots_xml = workbook_sheet_xml(archive, "Plots")
            worksheet_xml = workbook_sheet_xml(archive, "A-1")
            chart_files = [
                name
                for name in archive.namelist()
                if name.startswith("xl/charts/chart")
            ]
        self.assertEqual(
            sheet_names,
            ["Summary", "Method", "A-1", "A-2", "A-3", "A-4", "Plots"],
        )
        self.assertIn("sample_id", shared_strings)
        self.assertIn("A-1", shared_strings)
        self.assertIn("A-2", shared_strings)
        self.assertIn("A-3", shared_strings)
        self.assertIn("A-4", shared_strings)
        self.assertIn("STOPPED", shared_strings)
        self.assertIn("FAULTED", shared_strings)
        self.assertIn("method_name", shared_strings)
        self.assertIn("TPU Pull v1", shared_strings)
        self.assertIn("unload_force_n", shared_strings)
        self.assertIn("-0.5", method_xml.decode("utf-8"))
        self.assertGreaterEqual(len(chart_files), 3)

        summary_cells = worksheet_cells(summary_xml)
        self.assertEqual(worksheet_number(summary_cells, "B6"), 1.0)
        self.assertEqual(worksheet_number(summary_cells, "C6"), -5.0)
        self.assertEqual(worksheet_number(summary_cells, "B7"), 1.0)
        self.assertEqual(worksheet_number(summary_cells, "C7"), 0.6)
        self.assertEqual(worksheet_number(summary_cells, "Z12"), 99.0)
        self.assertEqual(worksheet_number(summary_cells, "Z13"), 75.0)
        self.assertEqual(worksheet_number(summary_cells, "Z14"), 55.0)
        self.assertEqual(worksheet_number(summary_cells, "AA14"), 5.0)
        for cell_ref in ["A11", "G11", "H11", "I11", "J11", "K11"]:
            with self.subTest(cell_ref=cell_ref):
                self.assertNotEqual(summary_cells[cell_ref].attrib.get("t"), "s")
                self.assertIsNotNone(
                    summary_cells[cell_ref].find("sheet:v", WORKBOOK_NAMESPACE))

        plots_cells = worksheet_cells(plots_xml)
        self.assertEqual(worksheet_number(plots_cells, "C5"), 9.9)
        self.assertEqual(worksheet_number(plots_cells, "D5"), 99.0)
        self.assertEqual(worksheet_number(plots_cells, "E5"), 7.5)
        self.assertEqual(worksheet_number(plots_cells, "F5"), 75.0)
        self.assertEqual(worksheet_number(plots_cells, "G5"), 5.5)
        self.assertEqual(worksheet_number(plots_cells, "H5"), 55.0)

        cells = worksheet_cells(worksheet_xml)
        for cell_ref in ["A12", "B12", "C12", "E12", "I12", "J12", "K12", "L12", "M12"]:
            with self.subTest(cell_ref=cell_ref):
                self.assertNotEqual(cells[cell_ref].attrib.get("t"), "s")
                self.assertIsNotNone(
                    cells[cell_ref].find("sheet:v", WORKBOOK_NAMESPACE))
        for cell_ref in ["D12", "F12", "G12", "H12"]:
            with self.subTest(cell_ref=cell_ref):
                self.assertEqual(cells[cell_ref].attrib.get("t"), "s")

        monitor.set_sample_notes(1, " revised coupon ")
        self.assertEqual(record.notes, "revised coupon")
        workbook = monitor.sample_set_workbook()
        with zipfile.ZipFile(BytesIO(workbook)) as archive:
            shared_strings = archive.read("xl/sharedStrings.xml").decode("utf-8")
        self.assertIn("revised coupon", shared_strings)
        self.assertNotIn(">valid<", shared_strings)

        with self.assertRaises(ValueError):
            monitor.set_sample_notes(1, "x" * 201)
        with self.assertRaises(TestCommandError):
            monitor.set_sample_notes(99, "missing")

    def test_empty_sample_set_workbook_has_summary(self):
        monitor = SerialMonitor(AppConfig())

        workbook = monitor.sample_set_workbook()

        with zipfile.ZipFile(BytesIO(workbook)) as archive:
            sheet_names = workbook_sheet_names(archive)
            shared_strings = archive.read("xl/sharedStrings.xml").decode("utf-8")
            summary_xml = workbook_sheet_xml(archive, "Summary")
            method_xml = workbook_sheet_xml(archive, "Method")

        self.assertEqual(sheet_names, ["Summary", "Method", "Samples", "Plots"])
        self.assertIn("No samples recorded", shared_strings)
        self.assertIn("No method saved with this sample set", shared_strings)
        summary_cells = worksheet_cells(summary_xml)
        self.assertEqual(worksheet_number(summary_cells, "B6"), 0.0)
        self.assertEqual(worksheet_number(summary_cells, "B7"), 0.0)
        self.assertIn(
            "No method saved with this sample set",
            method_xml.decode("utf-8") + shared_strings,
        )

    def test_method_store_saves_lists_loads_and_overwrites_methods(self):
        with TemporaryDirectory() as directory:
            store = TestMethodStore(Path(directory))
            saved = store.save_method({
                "name": "TPU Pull v1",
                "steps": [step_payload(50.0)],
                "motion": {
                    "jog_speed_steps_s": 500.0,
                    "test_max_step_rate_steps_s": 1200.0,
                    "acceleration_steps_s2": 5000.0,
                },
                "initialization": {
                    "mode": INITIALIZATION_MODE_PRELOAD_UNLOAD_ZERO,
                    "preload_force_n": -10.0,
                    "unload_force_n": -0.5,
                    "rate_mm_s": 0.02,
                    "max_travel_mm": 2.0,
                },
            })

            self.assertEqual(saved["id"], "tpu-pull-v1")
            self.assertEqual(saved["name"], "TPU Pull v1")
            self.assertEqual(saved["steps"][0]["target_value"], 50.0)
            self.assertEqual(saved["motion"]["jog_speed_steps_s"], 500.0)
            self.assertEqual(
                saved["initialization"]["mode"],
                INITIALIZATION_MODE_PRELOAD_UNLOAD_ZERO,
            )
            self.assertEqual(saved["initialization"]["preload_force_n"], -10.0)
            self.assertEqual(saved["initialization"]["unload_force_n"], -0.5)

            listed = store.list_methods()
            self.assertEqual([method["id"] for method in listed], ["tpu-pull-v1"])
            self.assertEqual(listed[0]["step_count"], 1)

            loaded = store.get_method("tpu-pull-v1")
            self.assertEqual(loaded["name"], "TPU Pull v1")
            self.assertEqual(loaded["hash"], saved["hash"])
            self.assertEqual(loaded["initialization"]["rate_mm_s"], 0.02)
            self.assertEqual(loaded["initialization"]["unload_force_n"], -0.5)

            updated = store.save_method({
                "name": "TPU Pull v1",
                "steps": [step_payload(75.0)],
                "motion": {
                    "jog_speed_steps_s": 500.0,
                    "test_max_step_rate_steps_s": 1200.0,
                    "acceleration_steps_s2": 5000.0,
                },
            })

            self.assertEqual(len(store.list_methods()), 1)
            self.assertEqual(updated["steps"][0]["target_value"], 75.0)
            self.assertNotEqual(updated["hash"], saved["hash"])

    def test_sample_set_store_saves_lists_loads_and_reopens_sets(self):
        samples = [telemetry(1.0, 0.1), telemetry(2.0, 0.2)]
        record = TestSampleRecord(
            index=1,
            run_id=7,
            sample_id="Sample 1",
            notes="first coupon",
            status="COMPLETE",
            included=True,
            started_at=10.0,
            finished_at=12.0,
            point_count=len(samples),
            peak_force_n=2.0,
            peak_force_position_mm=0.2,
            final_force_n=2.0,
            final_position_mm=0.2,
            method_id="tpu-pull-v1",
            method_name="TPU Pull v1",
            method_hash="abc123",
            method_snapshot={"name": "TPU Pull v1"},
            samples=samples,
        )

        with TemporaryDirectory() as directory:
            store = TestSampleSetStore(Path(directory))
            saved = store.save_sample_set(
                " TPU Set 1 ",
                [record],
                {
                    "id": "tpu-pull-v1",
                    "name": "TPU Pull v1",
                    "steps": [step_payload(75.0)],
                    "motion": {
                        "jog_speed_steps_s": 500.0,
                        "test_max_step_rate_steps_s": 1200.0,
                        "acceleration_steps_s2": 5000.0,
                    },
                    "initialization": {},
                },
            )

            self.assertEqual(saved.id, "tpu-set-1")
            self.assertEqual(saved.name, "TPU Set 1")
            self.assertEqual(saved.samples[0].notes, "first coupon")
            self.assertEqual(saved.samples[0].samples[1]["force_n"], "2.0000")
            self.assertEqual(saved.method_snapshot["name"], "TPU Pull v1")
            self.assertEqual(saved.method_snapshot["steps"][0]["target_value"], 75.0)

            listed = store.list_sample_sets()
            self.assertEqual([sample_set["id"] for sample_set in listed], ["tpu-set-1"])
            self.assertEqual(listed[0]["sample_count"], 1)

            loaded = store.get_sample_set("tpu-set-1")
            self.assertEqual(loaded.name, "TPU Set 1")
            self.assertEqual(loaded.samples[0].sample_id, "Sample 1")
            self.assertEqual(loaded.method_snapshot["steps"][0]["target_value"], 75.0)

            monitor = SerialMonitor(AppConfig())
            monitor.replace_sample_set(loaded)
            public_set = monitor.public_sample_set()
            self.assertEqual(public_set["next_sample_id"], "Sample 2")
            self.assertEqual(public_set["samples"][0]["notes"], "first coupon")
            overlay = monitor.sample_overlay()
            self.assertEqual(len(overlay["series"][0]["points"]), 2)

            with self.assertRaises(ValueError):
                store.save_sample_set("Empty", [])

    def test_method_payload_validation(self):
        parsed = parse_test_method({
            "name": "  Compression  ",
            "steps": [step_payload(-25.0)],
            "motion": {},
        })
        self.assertEqual(parsed.name, "Compression")
        self.assertEqual(parsed.steps[0].target_value, -25.0)
        self.assertEqual(parsed.initialization.mode, INITIALIZATION_MODE_NONE)

        parsed_disabled_initialization = parse_test_method({
            "name": "No Init",
            "steps": [step_payload()],
            "motion": {},
            "initialization": {
                "mode": INITIALIZATION_MODE_NONE,
                "preload_force_n": 0,
                "unload_force_n": 99,
                "rate_mm_s": 0,
                "max_travel_mm": 0,
            },
        })
        self.assertEqual(
            parsed_disabled_initialization.initialization.preload_force_n,
            10.0,
        )
        self.assertEqual(
            parsed_disabled_initialization.initialization.unload_force_n,
            0.0,
        )

        with self.assertRaises(ValueError):
            parse_test_method({
                "name": "",
                "steps": [step_payload()],
                "motion": {},
            })

        with self.assertRaises(ValueError):
            parse_test_method({
                "name": "Bad Motion",
                "steps": [step_payload()],
                "motion": {"jog_speed_steps_s": -1},
            })

        invalid_initializations = [
            {"mode": "BAD_MODE"},
            {"mode": INITIALIZATION_MODE_PRELOAD_UNLOAD_ZERO, "preload_force_n": 0},
            {"mode": INITIALIZATION_MODE_PRELOAD_UNLOAD_ZERO, "unload_force_n": "bad"},
            {"mode": INITIALIZATION_MODE_PRELOAD_UNLOAD_ZERO, "rate_mm_s": 0},
            {"mode": INITIALIZATION_MODE_PRELOAD_UNLOAD_ZERO, "max_travel_mm": 0},
        ]
        for initialization in invalid_initializations:
            with self.subTest(initialization=initialization):
                with self.assertRaises(ValueError):
                    parse_test_method({
                        "name": "Bad Initialization",
                        "steps": [step_payload()],
                        "motion": {},
                        "initialization": initialization,
                    })

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

    def test_initialization_runs_before_specimen_start(self):
        async def run_test():
            monitor = SerialMonitor(AppConfig())
            monitor._serial = object()
            monitor.snapshot.position_mm = 1.25
            monitor._plot_points = [{"index": 1}]
            monitor._plot_point_index = 1
            monitor._send_test_command_with_retries = AsyncMock()
            monitor._send_displacement_zero_with_retries = AsyncMock()
            monitor._ensure_test_heartbeat = Mock()

            task = asyncio.create_task(monitor.start_test(
                [step(100.0)],
                TestSampleMetadata("A-1"),
                {
                    "name": "Initialized Pull",
                    "steps": [step_payload(100.0)],
                    "motion": {},
                    "initialization": {
                        "mode": INITIALIZATION_MODE_PRELOAD_UNLOAD_ZERO,
                        "preload_force_n": 10.0,
                        "unload_force_n": 0.5,
                        "rate_mm_s": 0.02,
                        "max_travel_mm": 2.0,
                    },
                },
            ))
            await asyncio.sleep(0)

            self.assertEqual(monitor._test_run_kind, RUN_KIND_INITIALIZATION)
            self.assertEqual(len(monitor._test_steps), 2)
            self.assertEqual(monitor._test_steps[0].target_value, 10.0)
            self.assertEqual(monitor._test_steps[0].rate_type, "DISPLACEMENT")
            self.assertEqual(monitor._test_steps[1].target_value, 0.5)
            initialization_run_id = monitor._test_state.run_id

            monitor._apply_event(["EVT", "STEP_COMPLETE", str(initialization_run_id), "1"])
            await asyncio.sleep(0)
            monitor._apply_event(["EVT", "TEST_COMPLETE", str(initialization_run_id)])
            await task

            monitor._send_displacement_zero_with_retries.assert_awaited_once()
            self.assertEqual(monitor._plot_points, [])
            self.assertEqual(monitor._plot_point_index, 0)
            self.assertEqual(monitor._plot_reset_id, 1)
            self.assertEqual(monitor._test_run_kind, RUN_KIND_SPECIMEN)
            self.assertEqual(monitor._active_sample.sample_id, "A-1")
            self.assertEqual(
                monitor._active_method_snapshot["initialization"]["mode"],
                INITIALIZATION_MODE_PRELOAD_UNLOAD_ZERO,
            )
            self.assertEqual(monitor._sample_records, [])
            command_names = [
                call.args[1]
                for call in monitor._send_test_command_with_retries.await_args_list
            ]
            self.assertEqual(
                command_names,
                ["START_TEST", "TEST_STEP", "TEST_STEP", "START_TEST", "TEST_STEP"],
            )

        asyncio.run(run_test())

    def test_initialization_max_travel_abort_prevents_specimen_start(self):
        async def run_test():
            monitor = SerialMonitor(AppConfig())
            monitor._serial = object()
            monitor.snapshot.position_mm = 0.0
            monitor._send_test_command_with_retries = AsyncMock()
            monitor._send_displacement_zero_with_retries = AsyncMock()
            monitor._ensure_test_heartbeat = Mock()

            task = asyncio.create_task(monitor.start_test(
                [step(100.0)],
                TestSampleMetadata("A-1"),
                {
                    "name": "Initialized Pull",
                    "steps": [step_payload(100.0)],
                    "motion": {},
                    "initialization": {
                        "mode": INITIALIZATION_MODE_PRELOAD_UNLOAD_ZERO,
                        "preload_force_n": 10.0,
                        "rate_mm_s": 0.02,
                        "max_travel_mm": 0.5,
                    },
                },
            ))
            await asyncio.sleep(0)

            initialization_run_id = monitor._test_state.run_id
            monitor._apply_line(telemetry_line(
                1,
                1000,
                1.0,
                0.75,
                run_id=initialization_run_id,
                step_count=2,
            ))
            await asyncio.sleep(0)
            monitor._apply_event(["EVT", "TEST_STOPPED", str(initialization_run_id)])

            with self.assertRaises(TestCommandError) as raised:
                await task

            self.assertIn("exceeded max travel", str(raised.exception))
            monitor._send_displacement_zero_with_retries.assert_not_awaited()
            self.assertEqual(monitor._sample_records, [])
            self.assertEqual(monitor._test_run_kind, "NONE")
            command_names = [
                call.args[1]
                for call in monitor._send_test_command_with_retries.await_args_list
            ]
            self.assertEqual(command_names[-1], "STOP_TEST")
            self.assertNotIn("START_TEST", command_names[3:])

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
