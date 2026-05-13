from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass

import serial
from serial.tools import list_ports


@dataclass(slots=True)
class ProbeResult:
    saw_ack_ping: bool = False
    status_line: str | None = None
    raw_lines: list[str] | None = None


def available_ports() -> list[str]:
    return [port.device for port in list_ports.comports()]


def probe_serial_link(port: str, baudrate: int, timeout_s: float) -> ProbeResult:
    deadline = time.monotonic() + timeout_s
    result = ProbeResult(raw_lines=[])

    with serial.Serial(port, baudrate=baudrate, timeout=0.25) as connection:
        # Uno-class boards commonly reset when the USB serial connection opens.
        time.sleep(2.0)
        connection.reset_input_buffer()
        connection.write(b"PING\n")
        connection.write(b"GET_STATUS\n")
        connection.flush()

        while time.monotonic() < deadline:
            raw_line = connection.readline()
            if not raw_line:
                continue
            line = raw_line.decode("ascii", errors="ignore").strip()
            if not line:
                continue
            result.raw_lines.append(line)
            if line == "ACK,PING":
                result.saw_ack_ping = True
            if line.startswith("STATUS,"):
                result.status_line = line
            if result.saw_ack_ping and result.status_line is not None:
                break

    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check USB serial communication with the tensile tester Arduino.")
    parser.add_argument("--port", help="Serial device path, for example /dev/ttyACM0 or COM3.")
    parser.add_argument("--baudrate", type=int, default=115200, help="Serial baud rate. Default: 115200.")
    parser.add_argument("--timeout", type=float, default=6.0, help="Probe timeout in seconds. Default: 6.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ports = available_ports()
    port = args.port or (ports[0] if len(ports) == 1 else None)

    if port is None:
        print("No unique serial port was selected.")
        print("Detected ports:", ", ".join(ports) if ports else "none")
        print("Run again with --port /dev/ttyACM0, --port /dev/ttyUSB0, or the COM port in use.")
        return 2

    print(f"Probing Arduino serial link on {port} at {args.baudrate} baud...")
    try:
        result = probe_serial_link(port, args.baudrate, args.timeout)
    except serial.SerialException as exc:
        print(f"Serial open/read failed: {exc}")
        return 3

    if result.saw_ack_ping and result.status_line is not None:
        print("Serial handshake passed.")
        print(f"Controller status: {result.status_line}")
        return 0

    print("Serial handshake did not complete.")
    if result.raw_lines:
        print("Lines received:")
        for line in result.raw_lines:
            print(f"  {line}")
    else:
        print("No controller lines were received.")
    print("Expected at least ACK,PING and STATUS,... from the Arduino firmware.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
