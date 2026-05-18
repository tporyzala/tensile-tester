const $ = (id) => document.getElementById(id);
let motionControlsActive = false;
let motionLocalUntil = 0;
let lastMotionSent = "";
let serialAutoScroll = true;
let tareInFlight = false;
let lastConnected = false;
let messageHoldUntil = 0;

function number(value, digits) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed.toFixed(digits) : "--";
}

function setActive(id, active) {
  $(id).classList.toggle("active", Boolean(active));
}

function setMessage(text, holdMs = 0) {
  $("message").textContent = text || "";
  messageHoldUntil = holdMs > 0 ? Date.now() + holdMs : 0;
}

function updateMotionLabels() {
  $("speed-value").textContent = `${Number($("speed-slider").value).toFixed(0)} steps/s`;
  $("acceleration-value").textContent = `${Number($("acceleration-slider").value).toFixed(0)} steps/s^2`;
}

function updateTareButton() {
  const button = $("tare-button");
  button.disabled = tareInFlight;
  button.textContent = tareInFlight ? "Taring" : "Tare";
  button.classList.toggle("disconnected", !lastConnected);
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
      setMessage(`Motion setting error: ${detail}`, 5000);
    }
  } catch (error) {
    lastMotionSent = "";
    setMessage(`Motion setting error: ${error}`, 5000);
  }
}

async function tareLoad() {
  if (tareInFlight) {
    return;
  }

  tareInFlight = true;
  updateTareButton();
  setMessage(lastConnected
    ? "Taring load reading."
    : "Trying tare; Arduino is not connected in the latest snapshot.");
  try {
    const response = await fetch("/api/tare", { method: "POST" });
    if (!response.ok) {
      let detail = `HTTP ${response.status}`;
      try {
        const data = await response.json();
        detail = data.detail || detail;
      } catch (_) {
      }
      setMessage(`Tare error: ${detail}`, 5000);
      return;
    }
    const data = await response.json();
    updatePage(data);
  } catch (error) {
    setMessage(`Tare error: ${error}`, 5000);
  } finally {
    tareInFlight = false;
    updateTareButton();
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
  lastConnected = Boolean(connected);
  const connection = $("connection");
  connection.textContent = lastConnected ? "Connected" : "Disconnected";
  connection.classList.toggle("connected", lastConnected);
  connection.classList.toggle("disconnected", !lastConnected);
  updateTareButton();
}

function updatePage(data) {
  $("force").textContent = number(data.force_n, 2);
  $("state").textContent = data.state || "--";
  $("raw").textContent = String(data.raw_adc ?? "--");
  $("position").textContent = `${number(data.position_mm, 3)} mm`;
  $("step-rate").textContent = `${number(data.step_rate_steps_s, 0)} steps/s`;
  if (data.tare_confirmed) {
    setMessage(data.last_message || "Load reading tared.", 3000);
  } else if (!tareInFlight && Date.now() >= messageHoldUntil) {
    setMessage(data.last_message || "");
  }
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
    setMessage(`Web app error: ${error}`, 5000);
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

$("tare-button").addEventListener("click", tareLoad);

updateMotionLabels();
updateTareButton();
refresh();
window.setInterval(refresh, 250);
