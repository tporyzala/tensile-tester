import unittest
from unittest.mock import AsyncMock, patch

from app.main import (
    AppConfig,
    DISPLACEMENT_ZERO_COMMAND_ATTEMPTS,
    DisplacementZeroCommandError,
    SerialMonitor,
    TareCommandError,
)


class SetupCommandRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_motion_success_returns_confirmed_values_without_retry(self):
        monitor = SerialMonitor(AppConfig())
        confirmed = (1200.0, 1500.0, 3000.0)
        monitor._send_motion_attempt = AsyncMock(return_value=confirmed)

        result = await monitor._send_motion_with_retries(*confirmed)

        self.assertEqual(result, confirmed)
        monitor._send_motion_attempt.assert_awaited_once_with(*confirmed)

    async def test_tare_timeout_logs_retry_then_succeeds(self):
        monitor = SerialMonitor(AppConfig())
        monitor._send_tare_attempt = AsyncMock(
            side_effect=[TimeoutError(), None],
        )

        with patch("app.main.asyncio.sleep", new_callable=AsyncMock) as sleep:
            await monitor._send_tare_with_retries()

        self.assertEqual(monitor._send_tare_attempt.await_count, 2)
        sleep.assert_awaited_once()
        retry_message = (
            "Arduino did not acknowledge ZERO_LOAD before the timeout. "
            "Retrying ZERO_LOAD (2/3)."
        )
        self.assertEqual(monitor.snapshot.last_message, retry_message)
        self.assertTrue(monitor._serial_log[-1].endswith(f"SYS {retry_message}"))

    async def test_displacement_zero_exhaustion_raises_command_error(self):
        monitor = SerialMonitor(AppConfig())
        last_error = DisplacementZeroCommandError("Arduino rejected zero.")
        monitor._send_displacement_zero_attempt = AsyncMock(
            side_effect=[
                DisplacementZeroCommandError("First rejection."),
                TimeoutError(),
                last_error,
            ],
        )

        with patch("app.main.asyncio.sleep", new_callable=AsyncMock) as sleep:
            with self.assertRaises(DisplacementZeroCommandError) as raised:
                await monitor._send_displacement_zero_with_retries()

        final_message = (
            "Arduino did not confirm ZERO_DISPLACEMENT after "
            f"{DISPLACEMENT_ZERO_COMMAND_ATTEMPTS} attempts."
        )
        self.assertEqual(monitor._send_displacement_zero_attempt.await_count, 3)
        self.assertEqual(sleep.await_count, 2)
        self.assertEqual(str(raised.exception), final_message)
        self.assertIs(raised.exception.__cause__, last_error)
        self.assertEqual(monitor.snapshot.last_message, final_message)
        self.assertTrue(monitor._serial_log[-1].endswith(f"SYS {final_message}"))

    async def test_unexpected_error_propagates_without_retry(self):
        monitor = SerialMonitor(AppConfig())
        unexpected = RuntimeError("Unexpected failure.")
        monitor._send_tare_attempt = AsyncMock(side_effect=unexpected)

        with patch("app.main.asyncio.sleep", new_callable=AsyncMock) as sleep:
            with self.assertRaises(RuntimeError) as raised:
                await monitor._send_tare_with_retries()

        self.assertIs(raised.exception, unexpected)
        monitor._send_tare_attempt.assert_awaited_once()
        sleep.assert_not_awaited()
        self.assertNotIsInstance(raised.exception, TareCommandError)


if __name__ == "__main__":
    unittest.main()
