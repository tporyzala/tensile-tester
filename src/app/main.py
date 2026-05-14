from __future__ import annotations

import asyncio
import os
import platform
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass

import serial
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse


DEFAULT_SERIAL_PORT = "COM3" if platform.system().lower().startswith("win") else "/dev/ttyACM0"


@dataclass(slots=True)
class AppConfig:
    serial_port: str = os.getenv("TENSILE_SERIAL_PORT", DEFAULT_SERIAL_PORT)
    serial_baudrate: int = int(os.getenv("TENSILE_SERIAL_BAUDRATE", "115200"))
    reconnect_delay_s: float = float(os.getenv("TENSILE_SERIAL_RECONNECT_S", "2.0"))


@dataclass(slots=True)
class MachineSnapshot:
    connected: bool = False
    state: str = "DISCONNECTED"
    raw_adc: int = 0
    force_n: float = 0.0
    step_rate_steps_s: float = 0.0
    position_mm: float = 0.0
    button_up: bool = False
    button_down: bool = False
    telemetry_seq: int = 0
    controller_time_ms: int = 0
    updated_at: float = 0.0
    last_message: str = "Waiting for Arduino."


class SerialMonitor:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.snapshot = MachineSnapshot()
        self._serial_log: deque[str] = deque(maxlen=80)
        self._serial: serial.Serial | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()

    async def start(self) -> None:
        self._stopped.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopped.set()
        if self._task is not None:
            await self._task
            self._task = None
        await self._close_serial()

    def public_snapshot(self) -> dict[str, object]:
        data = asdict(self.snapshot)
        data["raw_serial"] = list(self._serial_log)
        return data

    async def send(self, line: str) -> None:
        if self._serial is None:
            raise RuntimeError("Serial transport is not connected.")
        payload = f"{line}\n".encode("ascii", errors="ignore")
        connection = self._serial

        def write_and_flush() -> None:
            connection.write(payload)
            connection.flush()

        self._log_serial("TX", line)
        await asyncio.to_thread(write_and_flush)

    async def _run(self) -> None:
        while not self._stopped.is_set():
            if self._serial is None:
                await self._connect_or_wait()
                continue

            try:
                raw_line = await asyncio.to_thread(self._serial.readline)
            except Exception as exc:
                await self._mark_disconnected(exc)
                continue

            if not raw_line:
                continue

            line = raw_line.decode("ascii", errors="ignore").strip()
            if line:
                self._log_serial("RX", line)
                self._apply_line(line)

    async def _connect_or_wait(self) -> None:
        try:
            self._serial = await asyncio.to_thread(
                serial.Serial,
                self.config.serial_port,
                self.config.serial_baudrate,
                timeout=0.25,
            )
            self.snapshot.connected = True
            self.snapshot.state = "CONNECTED"
            self.snapshot.updated_at = time.time()
            self.snapshot.last_message = f"Arduino connected on {self.config.serial_port}."
            self._log_serial("SYS", self.snapshot.last_message)
            await self.send("GET_STATUS")
        except Exception as exc:
            self.snapshot.connected = False
            self.snapshot.state = "DISCONNECTED"
            self.snapshot.updated_at = time.time()
            self.snapshot.last_message = f"Arduino not detected on {self.config.serial_port}: {exc}"
            self._log_serial("SYS", self.snapshot.last_message)
            await asyncio.sleep(max(self.config.reconnect_delay_s, 0.25))

    async def _mark_disconnected(self, exc: Exception) -> None:
        await self._close_serial()
        self.snapshot.connected = False
        self.snapshot.state = "DISCONNECTED"
        self.snapshot.step_rate_steps_s = 0.0
        self.snapshot.button_up = False
        self.snapshot.button_down = False
        self.snapshot.updated_at = time.time()
        self.snapshot.last_message = f"Serial link lost: {exc}"
        self._log_serial("SYS", self.snapshot.last_message)

    async def _close_serial(self) -> None:
        if self._serial is None:
            return
        connection = self._serial
        self._serial = None
        await asyncio.to_thread(connection.close)

    def _apply_line(self, line: str) -> None:
        parts = [part.strip() for part in line.split(",")]
        kind = parts[0].upper() if parts else ""
        try:
            if kind == "TEL" and len(parts) >= 10:
                self.snapshot.telemetry_seq = int(parts[1])
                self.snapshot.controller_time_ms = int(parts[2])
                self._apply_payload(parts[3:10])
            elif kind == "STATUS" and len(parts) >= 8:
                self._apply_payload(parts[1:8])
            else:
                self.snapshot.last_message = line
                self.snapshot.updated_at = time.time()
        except ValueError:
            self.snapshot.last_message = f"Could not parse serial line: {line}"
            self.snapshot.updated_at = time.time()

    def _apply_payload(self, payload: list[str]) -> None:
        self.snapshot.connected = True
        self.snapshot.state = payload[0]
        self.snapshot.raw_adc = int(float(payload[1]))
        self.snapshot.force_n = float(payload[2])
        self.snapshot.step_rate_steps_s = float(payload[3])
        self.snapshot.position_mm = float(payload[4])
        self.snapshot.button_up = payload[5] == "1"
        self.snapshot.button_down = payload[6] == "1"
        self.snapshot.updated_at = time.time()
        self.snapshot.last_message = "Telemetry received."

    def _log_serial(self, direction: str, line: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self._serial_log.append(f"{timestamp} {direction} {line}")


config = AppConfig()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.monitor = SerialMonitor(config)
    await app.state.monitor.start()
    try:
        yield
    finally:
        await app.state.monitor.stop()


app = FastAPI(title="Tensile Tester", lifespan=lifespan)


@app.get("/", include_in_schema=False)
async def index() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML)


@app.get("/api/snapshot")
async def snapshot(request: Request) -> JSONResponse:
    return JSONResponse(request.app.state.monitor.public_snapshot())


@app.get("/health")
async def health(request: Request) -> dict[str, object]:
    data = request.app.state.monitor.public_snapshot()
    return {"ok": True, "arduino_connected": data["connected"], "state": data["state"]}


INDEX_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Tensile Tester</title>
  <style>
    :root {
      color-scheme: light;
      font-family: Arial, Helvetica, sans-serif;
      background: #f5f7f8;
      color: #172026;
    }

    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
    }

    main {
      width: min(900px, 100%);
    }

    .topbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 14px;
    }

    h1 {
      margin: 0;
      font-size: 24px;
      font-weight: 700;
      letter-spacing: 0;
    }

    .chip {
      min-width: 118px;
      border: 1px solid #b8c1c7;
      border-radius: 999px;
      padding: 7px 12px;
      text-align: center;
      font-size: 14px;
      font-weight: 700;
      background: #ffffff;
      color: #4d5961;
    }

    .chip.connected {
      border-color: #1b8f61;
      color: #116b49;
      background: #e9f7f0;
    }

    .chip.disconnected {
      border-color: #c4473d;
      color: #9b2d26;
      background: #fff0ee;
    }

    .panel {
      border: 1px solid #d8dee2;
      border-radius: 8px;
      background: #ffffff;
      box-shadow: 0 10px 30px rgba(23, 32, 38, 0.08);
      padding: 28px;
    }

    .force {
      display: grid;
      grid-template-columns: 1fr;
      gap: 4px;
      padding-bottom: 24px;
      border-bottom: 1px solid #e4e8eb;
    }

    .label {
      color: #5d6870;
      font-size: 14px;
      font-weight: 700;
      text-transform: uppercase;
    }

    .force-value {
      font-size: clamp(56px, 13vw, 116px);
      line-height: 0.95;
      font-weight: 800;
      letter-spacing: 0;
      color: #10181d;
      overflow-wrap: anywhere;
    }

    .unit {
      color: #5d6870;
      font-size: 28px;
      font-weight: 700;
    }

    .grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 14px;
      margin-top: 22px;
    }

    .metric {
      border: 1px solid #e1e6e9;
      border-radius: 8px;
      padding: 14px;
      min-height: 86px;
      background: #fbfcfc;
    }

    .metric-value {
      margin-top: 10px;
      font-size: 22px;
      font-weight: 800;
      color: #172026;
      overflow-wrap: anywhere;
    }

    .buttons {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      margin-top: 22px;
    }

    .button-state {
      border: 1px solid #cad2d7;
      border-radius: 8px;
      padding: 12px;
      text-align: center;
      font-weight: 800;
      background: #f7f9fa;
      color: #536069;
    }

    .button-state.active {
      border-color: #176a8f;
      background: #e8f5fb;
      color: #105373;
    }

    .message {
      margin-top: 18px;
      color: #65717a;
      font-size: 13px;
      overflow-wrap: anywhere;
    }

    .serial-panel {
      margin-top: 18px;
      border-top: 1px solid #e4e8eb;
      padding-top: 18px;
    }

    .serial-log {
      width: 100%;
      height: 180px;
      margin-top: 10px;
      border: 1px solid #cbd3d8;
      border-radius: 8px;
      padding: 12px;
      resize: vertical;
      background: #10181d;
      color: #e8eef2;
      font-family: Consolas, "Courier New", monospace;
      font-size: 12px;
      line-height: 1.45;
      white-space: pre;
    }

    @media (max-width: 720px) {
      body {
        align-items: flex-start;
        padding: 16px;
      }

      .topbar {
        align-items: flex-start;
        flex-direction: column;
      }

      .panel {
        padding: 20px;
      }

      .grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
    }
  </style>
</head>
<body>
  <main>
    <div class="topbar">
      <h1>Tensile Tester</h1>
      <div id="connection" class="chip disconnected">Disconnected</div>
    </div>
    <section class="panel" aria-label="Live machine telemetry">
      <div class="force">
        <div class="label">Force</div>
        <div>
          <span id="force" class="force-value">--</span>
          <span class="unit">N</span>
        </div>
      </div>
      <div class="grid">
        <div class="metric">
          <div class="label">State</div>
          <div id="state" class="metric-value">--</div>
        </div>
        <div class="metric">
          <div class="label">Raw ADC</div>
          <div id="raw" class="metric-value">--</div>
        </div>
        <div class="metric">
          <div class="label">Position</div>
          <div id="position" class="metric-value">--</div>
        </div>
        <div class="metric">
          <div class="label">Step Rate</div>
          <div id="step-rate" class="metric-value">--</div>
        </div>
      </div>
      <div class="buttons">
        <div id="button-up" class="button-state">Button 1 Up</div>
        <div id="button-down" class="button-state">Button 2 Down</div>
      </div>
      <div id="message" class="message">Waiting for telemetry.</div>
      <div class="serial-panel">
        <div class="label">Raw Serial</div>
        <textarea id="serial-log" class="serial-log" readonly spellcheck="false"></textarea>
      </div>
    </section>
  </main>
  <script>
    const $ = (id) => document.getElementById(id);

    function number(value, digits) {
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed.toFixed(digits) : "--";
    }

    function setActive(id, active) {
      $(id).classList.toggle("active", Boolean(active));
    }

    async function refresh() {
      try {
        const response = await fetch("/api/snapshot", { cache: "no-store" });
        const data = await response.json();

        $("force").textContent = number(data.force_n, 2);
        $("state").textContent = data.state || "--";
        $("raw").textContent = String(data.raw_adc ?? "--");
        $("position").textContent = `${number(data.position_mm, 3)} mm`;
        $("step-rate").textContent = `${number(data.step_rate_steps_s, 0)} steps/s`;
        $("message").textContent = data.last_message || "";

        const serialLog = $("serial-log");
        const rawSerial = (data.raw_serial || []).join("\\n");
        if (serialLog.value !== rawSerial) {
          serialLog.value = rawSerial;
          serialLog.scrollTop = serialLog.scrollHeight;
        }

        const connection = $("connection");
        connection.textContent = data.connected ? "Connected" : "Disconnected";
        connection.classList.toggle("connected", Boolean(data.connected));
        connection.classList.toggle("disconnected", !data.connected);

        setActive("button-up", data.button_up);
        setActive("button-down", data.button_down);
      } catch (error) {
        $("connection").textContent = "Disconnected";
        $("connection").classList.remove("connected");
        $("connection").classList.add("disconnected");
        $("message").textContent = `Web app error: ${error}`;
      }
    }

    refresh();
    window.setInterval(refresh, 250);
  </script>
</body>
</html>
"""
