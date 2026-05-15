const $ = (id) => document.getElementById(id);
let motionControlsActive = false;
let motionLocalUntil = 0;
let lastMotionSent = "";
let serialAutoScroll = true;

function number(value, digits) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed.toFixed(digits) : "--";
}

function setActive(id, active) {
  $(id).classList.toggle("active", Boolean(active));
}

function updateMotionLabels() {
  $("speed-value").textContent = `${Number($("speed-slider").value).toFixed(0)} steps/s`;
  $("acceleration-value").textContent = `${Number($("acceleration-slider").value).toFixed(0)} steps/s^2`;
}

function syncMotionControls(data) {
  // Keep user drag/edit values local until the Arduino confirms or the holdoff expires.
  if (motionControlsActive || Date.now() < motionLocalUntil) {
    return;
  }
  const speed = Number(data.jog_speed_steps_s);
  const acceleration = Number(data.acceleration_steps_s2);
  if (Number.isFinite(speed)) {
    $("speed-slider").value = String(speed);
  }
  if (Number.isFinite(acceleration)) {
    $("acceleration-slider").value = String(acceleration);
  }
  updateMotionLabels();
}

function beginMotionEdit() {
  motionControlsActive = true;
  motionLocalUntil = Date.now() + 2000;
}

function finishMotionEdit() {
  motionControlsActive = false;
  motionLocalUntil = Date.now() + 2000;
  sendMotionUpdate();
}

function scheduleMotionUpdate() {
  motionLocalUntil = Date.now() + 2000;
  updateMotionLabels();
}

async function sendMotionUpdate() {
  const payload = {
    speed_steps_s: Number($("speed-slider").value),
    acceleration_steps_s2: Number($("acceleration-slider").value),
  };
  const serialized = JSON.stringify(payload);
  if (serialized === lastMotionSent) {
    return;
  }
  lastMotionSent = serialized;
  try {
    const response = await fetch("/api/motion", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!response.ok) {
      let detail = `HTTP ${response.status}`;
      try {
        const data = await response.json();
        detail = data.detail || detail;
      } catch (_) {
      }
      lastMotionSent = "";
      $("message").textContent = `Motion setting error: ${detail}`;
    }
  } catch (error) {
    lastMotionSent = "";
    $("message").textContent = `Motion setting error: ${error}`;
  }
}

function updateSerialLog(rawSerialLines) {
  const serialLog = $("serial-log");
  const rawSerial = (rawSerialLines || []).join("\n");
  if (serialLog.value === rawSerial) {
    return;
  }

  const wasAtBottom =
    serialLog.scrollHeight - serialLog.scrollTop - serialLog.clientHeight < 12;
  serialLog.value = rawSerial;
  if (serialAutoScroll || wasAtBottom) {
    serialLog.scrollTop = serialLog.scrollHeight;
  }
}

function updateConnection(connected) {
  const connection = $("connection");
  connection.textContent = connected ? "Connected" : "Disconnected";
  connection.classList.toggle("connected", Boolean(connected));
  connection.classList.toggle("disconnected", !connected);
}

function updatePage(data) {
  $("force").textContent = number(data.force_n, 2);
  $("state").textContent = data.state || "--";
  $("raw").textContent = String(data.raw_adc ?? "--");
  $("position").textContent = `${number(data.position_mm, 3)} mm`;
  $("step-rate").textContent = `${number(data.step_rate_steps_s, 0)} steps/s`;
  $("message").textContent = data.last_message || "";
  syncMotionControls(data);
  updateSerialLog(data.raw_serial);
  updateConnection(data.connected);
  setActive("button-up", data.button_up);
  setActive("button-down", data.button_down);
}

async function refresh() {
  try {
    // Poll the Python snapshot instead of reading serial directly in the browser.
    const response = await fetch("/api/snapshot", { cache: "no-store" });
    const data = await response.json();
    updatePage(data);
  } catch (error) {
    updateConnection(false);
    $("message").textContent = `Web app error: ${error}`;
  }
}

function watchMotionSlider(slider) {
  slider.addEventListener("pointerdown", beginMotionEdit);
  slider.addEventListener("focus", beginMotionEdit);
  slider.addEventListener("input", scheduleMotionUpdate);
  slider.addEventListener("change", finishMotionEdit);
  slider.addEventListener("pointerup", finishMotionEdit);
  slider.addEventListener("blur", finishMotionEdit);
}

for (const slider of [$("speed-slider"), $("acceleration-slider")]) {
  watchMotionSlider(slider);
}

$("serial-log").addEventListener("scroll", () => {
  const serialLog = $("serial-log");
  serialAutoScroll =
    serialLog.scrollHeight - serialLog.scrollTop - serialLog.clientHeight < 12;
});

updateMotionLabels();
refresh();
window.setInterval(refresh, 250);
