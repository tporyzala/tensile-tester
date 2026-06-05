const $ = (id) => document.getElementById(id);

let steps = [
  { target_type: "FORCE", target_value: 100, rate_type: "FORCE", rate_value_per_s: 10, hold_duration_s: 5 },
];
let serialAutoScroll = true;
let commandInFlight = false;
let tareInFlight = false;
let displacementZeroInFlight = false;
let motionInFlight = false;
let motionControlsActive = false;
let motionLocalUntil = 0;
let lastMotionSent = "";
let lastConnected = false;
let lastStatus = "IDLE";
let sampleSetSignature = "";
let overlayEnabled = false;
let overlayRefreshInFlight = false;
let plotCursor = 0;
let plotResetId = null;
let plotRefreshInFlight = false;
let plotGeneration = 0;
let lastSubmittedSampleId = "";
let lastMachine = {};
let calibrationPoints = [];
let calibrationFit = null;
let calibrationSampleInFlight = false;
let calibrationSampleStartedAt = 0;
let calibrationFitInFlight = false;
const CALIBRATION_SAMPLE_DURATION_MS = 12000;
const STEPS_PER_MM = 7681.2;
const calibrationChartConfig = {
  responsive: true,
  displaylogo: false,
  scrollZoom: true,
  modeBarButtonsToRemove: ["select2d", "lasso2d"],
};
const tabTitles = {
  run: "Automated Test",
  calibration: "Calibration",
  setup: "Setup",
};
const returnZeroRates = {
  LOAD: { value: 10, label: "Rate (N/s)", step: "0.1", max: "" },
  DISPLACEMENT: { value: 0.1562, label: "Rate (mm/s)", step: "0.0001", max: "0.1562" },
};
let lastDisplacementReturnDefault = returnZeroRates.DISPLACEMENT.value;
const liveCharts = new LiveForceCharts(
  "force-time-chart",
  "force-displacement-chart",
);
window.addEventListener("resize", () => {
  const chart = $("calibration-fit-chart");
  if (window.Plotly && chart?.dataset.plotlyReady === "1") {
    window.Plotly.Plots.resize(chart);
  }
});

function number(value, digits) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed.toFixed(digits) : "--";
}

function optionalNumber(value, digits, suffix = "") {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? `${parsed.toFixed(digits)}${suffix}` : "--";
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function setMessage(text) {
  $("message").textContent = text || "";
}

async function requestJson(url, options = {}) {
  const response = await fetch(url, options);
  let data = {};
  try {
    data = await response.json();
  } catch (_) {
  }
  return { response, data };
}

function switchTab(tabName) {
  $("page-title").textContent = tabTitles[tabName] || "Tensile Tester";
  for (const button of document.querySelectorAll(".tab-button")) {
    const active = button.dataset.tab === tabName;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
  }

  for (const panel of document.querySelectorAll(".tab-panel")) {
    const active = panel.id === `${tabName}-panel`;
    panel.classList.toggle("active", active);
    panel.hidden = !active;
  }

  syncLiveChartVisibility();
  if (tabName === "calibration") {
    window.requestAnimationFrame(() => renderCalibrationChart());
  }
}

function syncLiveChartVisibility() {
  liveCharts.setVisible(!document.hidden && !$("run-panel").hidden);
}

function renderSteps() {
  const body = $("step-body");
  body.innerHTML = steps.map((step, index) => `
    <tr>
      <td>${index + 1}</td>
      <td>
        <select class="table-input" data-index="${index}" data-field="target_type">
          <option value="FORCE" ${step.target_type === "FORCE" ? "selected" : ""}>Force (N)</option>
          <option value="DISPLACEMENT" ${step.target_type === "DISPLACEMENT" ? "selected" : ""}>Displacement (mm)</option>
        </select>
      </td>
      <td><input class="table-input" data-index="${index}" data-field="target_value" type="number" step="0.001" value="${step.target_value}"></td>
      <td>
        <select class="table-input" data-index="${index}" data-field="rate_type">
          <option value="FORCE" ${step.rate_type === "FORCE" ? "selected" : ""}>Force (N/s)</option>
          <option value="DISPLACEMENT" ${step.rate_type === "DISPLACEMENT" ? "selected" : ""}>Displacement (mm/s)</option>
        </select>
      </td>
      <td><input class="table-input" data-index="${index}" data-field="rate_value_per_s" type="number" min="0.0001" step="0.001" value="${step.rate_value_per_s}"></td>
      <td><input class="table-input" data-index="${index}" data-field="hold_duration_s" type="number" min="0" step="0.1" value="${step.hold_duration_s}"></td>
      <td><button class="mini-button danger" data-action="remove" data-index="${index}" type="button">Delete</button></td>
    </tr>
  `).join("");
}

function readStepsFromTable() {
  for (const input of document.querySelectorAll("[data-field]")) {
    const index = Number(input.dataset.index);
    const field = input.dataset.field;
    steps[index][field] = field.endsWith("_type") ? input.value : Number(input.value);
  }
}

function addStep() {
  readStepsFromTable();
  const last = steps[steps.length - 1] || {
    target_type: "FORCE",
    target_value: 100,
    rate_type: "FORCE",
    rate_value_per_s: 10,
    hold_duration_s: 5,
  };
  steps.push({ ...last });
  renderSteps();
}

function removeStep(index) {
  readStepsFromTable();
  if (steps.length === 1) {
    setMessage("At least one step is required.");
    return;
  }
  steps.splice(index, 1);
  renderSteps();
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
  const isConnected = Boolean(connected);
  lastConnected = isConnected;
  const connection = $("connection");
  connection.textContent = isConnected ? "Connected" : "Disconnected";
  connection.classList.toggle("connected", isConnected);
  connection.classList.toggle("disconnected", !isConnected);
}

function updateButtons(status) {
  const active = ["STARTING", "RUNNING", "PAUSED", "WAITING_NEXT"].includes(status);
  const blocked = active || status === "FAULT";
  const readyForSetupActions = ["IDLE", "COMPLETE"].includes(status);
  const zeroingInFlight = tareInFlight || displacementZeroInFlight;
  const calibrationBusy = calibrationSampleInFlight;
  const setupControlsDisabled =
    commandInFlight || zeroingInFlight || motionInFlight || calibrationBusy ||
    !readyForSetupActions || !lastConnected;
  const samples = getCurrentSamples();
  $("start-button").disabled =
    commandInFlight || motionInFlight || zeroingInFlight ||
    calibrationBusy || !readyForSetupActions;
  $("tare-button").disabled = commandInFlight || motionInFlight || zeroingInFlight || calibrationBusy || !readyForSetupActions;
  $("tare-button").textContent = tareInFlight ? "Taring" : "Tare";
  $("zero-displacement-button").disabled = commandInFlight || motionInFlight || zeroingInFlight || calibrationBusy || !readyForSetupActions;
  $("zero-displacement-button").textContent = displacementZeroInFlight ? "Zeroing" : "Zero Displacement";
  $("return-zero-button").disabled =
    commandInFlight || motionInFlight || zeroingInFlight ||
    calibrationBusy || !readyForSetupActions;
  for (const button of document.querySelectorAll(".relative-move-button")) {
    button.disabled =
      commandInFlight || motionInFlight || zeroingInFlight ||
      calibrationBusy || !readyForSetupActions || !lastConnected;
  }
  $("pause-button").disabled = commandInFlight || !["RUNNING", "PAUSED", "WAITING_NEXT"].includes(status);
  $("pause-button").textContent = status === "PAUSED" ? "Resume" : "Pause";
  $("stop-button").disabled = commandInFlight || !(active || status === "FAULT");
  $("add-step-button").disabled = commandInFlight;
  $("clear-samples-button").disabled = commandInFlight || blocked || samples.length === 0;
  $("sample-id").disabled = commandInFlight || motionInFlight || zeroingInFlight || calibrationBusy || !readyForSetupActions;
  $("sample-notes").disabled = commandInFlight || motionInFlight || zeroingInFlight || calibrationBusy || !readyForSetupActions;
  for (const slider of motionSliders()) {
    slider.disabled = setupControlsDisabled;
  }
  updateCalibrationButtons(status);
}

function getCurrentSamples() {
  const rows = $("sample-body").dataset.sampleCount;
  const count = Number(rows);
  return Number.isFinite(count) && count > 0 ? new Array(count) : [];
}

function motionSliders() {
  return [
    $("jog-speed-slider"),
    $("test-speed-slider"),
    $("acceleration-slider"),
  ];
}

function updateMotionLabels() {
  $("jog-speed-value").textContent = `${Number($("jog-speed-slider").value).toFixed(0)} steps/s`;
  $("test-speed-value").textContent = `${Number($("test-speed-slider").value).toFixed(0)} steps/s`;
  $("acceleration-value").textContent = `${Number($("acceleration-slider").value).toFixed(0)} steps/s^2`;
  updateDisplacementReturnLimit(Number($("test-speed-slider").value));
}

function updateDisplacementReturnLimit(testMaxStepRateStepsS) {
  const maxRate = Math.max(0.0001, testMaxStepRateStepsS / STEPS_PER_MM);
  const formattedRate = maxRate.toFixed(4);
  const previousDefault = lastDisplacementReturnDefault;
  lastDisplacementReturnDefault = Number(formattedRate);
  returnZeroRates.DISPLACEMENT.value = lastDisplacementReturnDefault;
  returnZeroRates.DISPLACEMENT.max = formattedRate;
  if ($("return-zero-mode").value !== "DISPLACEMENT") {
    return;
  }

  const input = $("return-zero-rate");
  const current = Number(input.value);
  input.max = formattedRate;
  if (
    !Number.isFinite(current) ||
    Math.abs(current - previousDefault) < 0.0001 ||
    current > lastDisplacementReturnDefault
  ) {
    input.value = formattedRate;
  }
}

function syncMotionControls(machine) {
  if (motionControlsActive || Date.now() < motionLocalUntil) {
    return;
  }

  const jogSpeed = Number(machine.jog_speed_steps_s);
  const testMaxSpeed = Number(machine.test_max_step_rate_steps_s);
  const acceleration = Number(machine.acceleration_steps_s2);
  if (Number.isFinite(jogSpeed)) {
    $("jog-speed-slider").value = String(jogSpeed);
  }
  if (Number.isFinite(testMaxSpeed)) {
    $("test-speed-slider").value = String(testMaxSpeed);
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

function scheduleMotionUpdate() {
  motionLocalUntil = Date.now() + 2000;
  updateMotionLabels();
}

function finishMotionEdit() {
  motionControlsActive = false;
  motionLocalUntil = Date.now() + 2000;
  sendMotionUpdate();
}

async function sendMotionUpdate() {
  const payload = {
    speed_steps_s: Number($("jog-speed-slider").value),
    test_max_step_rate_steps_s: Number($("test-speed-slider").value),
    acceleration_steps_s2: Number($("acceleration-slider").value),
  };
  const serialized = JSON.stringify(payload);
  if (serialized === lastMotionSent || motionInFlight) {
    return;
  }

  lastMotionSent = serialized;
  motionInFlight = true;
  updateButtons(lastStatus);
  setMessage("Sending motion settings.");
  try {
    const { response, data } = await requestJson("/api/motion", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: serialized,
    });
    if (!response.ok) {
      lastMotionSent = "";
      setMessage(data.detail || `HTTP ${response.status}`);
      return;
    }
    setMessage(data.last_message || "Motion settings applied.");
    await refresh();
  } catch (error) {
    lastMotionSent = "";
    setMessage(`Motion setting error: ${error}`);
  } finally {
    motionInFlight = false;
    updateButtons(lastStatus);
  }
}

function updatePage(data) {
  const run = data.run || {};
  const machine = data.machine || {};
  lastStatus = run.status || "IDLE";
  lastMachine = machine;

  $("frame-mode").textContent = machine.frame_mode || "--";
  $("force").textContent = `${number(machine.force_n, 2)} N`;
  $("control-mode").textContent = machine.test_control_mode || "--";
  if (machine.test_control_mode === "FORCE") {
    $("command-value").textContent = `${number(machine.test_setpoint_force_n, 2)} N`;
  } else if (machine.test_control_mode === "DISPLACEMENT") {
    $("command-value").textContent = `${number(machine.test_setpoint_displacement_mm, 3)} mm`;
  } else {
    $("command-value").textContent = "--";
  }
  $("phase").textContent = run.phase || machine.test_phase || "--";
  $("step").textContent = `${run.step_index || 0} / ${run.step_count || steps.length}`;
  $("position").textContent = `${number(machine.position_mm, 3)} mm`;
  $("step-rate").textContent = `${number(machine.step_rate_steps_s, 0)} steps/s`;
  updateSampleSet(data.sample_set || {});
  const message = lastStatus === "IDLE"
    ? machine.last_message || run.message
    : run.message || machine.last_message;
  setMessage(message || "");
  updateSerialLog(machine.raw_serial);
  updateConnection(machine.connected);
  updateCalibrationStatus(machine, run);
  syncMotionControls(machine);
  updateButtons(lastStatus);
}

function updateSampleSet(sampleSet) {
  updateSampleDefaults(sampleSet);
  renderSampleTable(sampleSet.samples || []);
  const signature = buildSampleSetSignature(sampleSet.samples || []);
  if (signature !== sampleSetSignature) {
    sampleSetSignature = signature;
    if (overlayEnabled) {
      refreshOverlay();
    }
  }
}

function updateSampleDefaults(sampleSet) {
  const nextSampleId = sampleSet.next_sample_id || "Sample 1";
  const sampleInput = $("sample-id");
  const notesInput = $("sample-notes");
  const activeSample = sampleSet.active_sample;
  const canReplace =
    !sampleInput.value ||
    sampleInput.value === sampleInput.dataset.defaultValue ||
    (!activeSample && lastSubmittedSampleId && sampleInput.value === lastSubmittedSampleId);
  if (canReplace) {
    sampleInput.value = nextSampleId;
    sampleInput.dataset.defaultValue = nextSampleId;
    if (!activeSample && lastSubmittedSampleId) {
      notesInput.value = "";
      lastSubmittedSampleId = "";
    }
  }
}

function renderSampleTable(samples) {
  const body = $("sample-body");
  body.dataset.sampleCount = String(samples.length);
  if (samples.length === 0) {
    body.innerHTML = `
      <tr>
        <td class="empty-table-cell" colspan="8">No samples recorded</td>
      </tr>
    `;
    return;
  }
  body.innerHTML = samples.map((sample) => `
    <tr>
      <td>${sample.index}</td>
      <td>${escapeHtml(sample.sample_id)}</td>
      <td>${escapeHtml(sample.status)}</td>
      <td>${sample.point_count || 0}</td>
      <td>${optionalNumber(sample.peak_force_n, 2, " N")}</td>
      <td>${optionalNumber(sample.final_position_mm, 3, " mm")}</td>
      <td>
        <input class="include-checkbox" data-sample-index="${sample.index}" type="checkbox" ${sample.included ? "checked" : ""}>
      </td>
      <td>${escapeHtml(sample.notes)}</td>
    </tr>
  `).join("");
}

function buildSampleSetSignature(samples) {
  return samples
    .map((sample) => `${sample.index}:${sample.included}:${sample.status}:${sample.point_count}`)
    .join("|");
}

function setCalibrationMessage(text) {
  $("calibration-message").textContent = text || "";
}

function hasCalibrationZero() {
  return calibrationPoints.some(
    (point) => Math.abs(Number(point.reference_force_n)) < 1e-9,
  );
}

function calibrationReadyForCapture(status = lastStatus) {
  const frameMode = String(lastMachine.frame_mode || "");
  const testPhase = String(lastMachine.test_phase || "NONE");
  const faultReason = String(lastMachine.fault_reason || "NONE");
  const stepRate = Math.abs(Number(lastMachine.step_rate_steps_s || 0));
  return (
    lastConnected &&
    ["IDLE", "COMPLETE"].includes(status) &&
    testPhase === "NONE" &&
    frameMode !== "FAULT" &&
    faultReason === "NONE" &&
    stepRate <= 0.5 &&
    !commandInFlight &&
    !motionInFlight &&
    !tareInFlight &&
    !displacementZeroInFlight
  );
}

function calibrationCanFit() {
  const zeroPoint = calibrationPoints.find(
    (point) => Math.abs(Number(point.reference_force_n)) < 1e-9,
  );
  if (!zeroPoint) {
    return false;
  }
  const zeroRaw = Number(zeroPoint.raw_adc_mean);
  return calibrationPoints.some((point) => (
    Math.abs(Number(point.reference_force_n)) >= 1e-9 &&
    Math.abs(Number(point.raw_adc_mean) - zeroRaw) > 1e-9
  ));
}

function updateCalibrationButtons(status = lastStatus) {
  const ready = calibrationReadyForCapture(status);
  const zeroCaptured = hasCalibrationZero();
  const referenceForce = Number($("calibration-reference-force").value);
  const validLoadForce =
    Number.isFinite(referenceForce) && Math.abs(referenceForce) >= 1e-9;
  const elapsed = Date.now() - calibrationSampleStartedAt;
  const remainingS = Math.max(
    0,
    Math.ceil((CALIBRATION_SAMPLE_DURATION_MS - elapsed) / 1000),
  );
  $("capture-zero-button").disabled = calibrationSampleInFlight || !ready || zeroCaptured;
  $("capture-zero-button").textContent = calibrationSampleInFlight
    ? `Capturing ${remainingS}s`
    : "Capture Zero";
  $("capture-load-button").disabled =
    calibrationSampleInFlight || !ready || !zeroCaptured || !validLoadForce;
  $("capture-load-button").textContent = calibrationSampleInFlight
    ? `Capturing ${remainingS}s`
    : "Capture Load Point";
  $("calibration-reference-force").disabled =
    calibrationSampleInFlight || !ready || !zeroCaptured;
  $("fit-calibration-button").disabled =
    calibrationSampleInFlight || calibrationFitInFlight || !calibrationCanFit();
  $("fit-calibration-button").textContent = calibrationFitInFlight
    ? "Fitting"
    : "Fit Linear Calibration";
  $("copy-calibration-button").disabled = !calibrationFit;
  $("download-calibration-json-button").disabled = calibrationPoints.length === 0;
  $("download-calibration-csv-button").disabled = calibrationPoints.length === 0;
  $("reset-calibration-button").disabled =
    calibrationSampleInFlight || calibrationPoints.length === 0;
}

function updateCalibrationStatus(machine, run) {
  $("calibration-frame-mode").textContent = machine.frame_mode || "--";
  $("calibration-phase").textContent = run.phase || machine.test_phase || "--";
  $("calibration-raw-adc").textContent = Number.isFinite(Number(machine.raw_adc))
    ? String(machine.raw_adc)
    : "--";
  $("calibration-step-rate").textContent = `${number(machine.step_rate_steps_s, 0)} steps/s`;
}

function renderCalibrationPoints() {
  const body = $("calibration-point-body");
  if (calibrationPoints.length === 0) {
    body.innerHTML = `
      <tr>
        <td class="empty-table-cell" colspan="8">No calibration points captured</td>
      </tr>
    `;
    renderCalibrationFit();
    renderCalibrationChart();
    updateCalibrationButtons(lastStatus);
    return;
  }

  const residuals = calibrationFit?.residuals || [];
  body.innerHTML = calibrationPoints.map((point, index) => {
    const residual = residuals[index]?.residual_force_n;
    return `
      <tr>
        <td>${index + 1}</td>
        <td>${optionalNumber(point.reference_force_n, 3, " N")}</td>
        <td>${optionalNumber(point.raw_adc_mean, 2)}</td>
        <td>${optionalNumber(point.raw_adc_stddev, 2)}</td>
        <td>${point.sample_count || 0}</td>
        <td>${optionalNumber(point.raw_adc_min, 0)} / ${optionalNumber(point.raw_adc_max, 0)}</td>
        <td>${optionalNumber(residual, 4, " N")}</td>
        <td><button class="mini-button danger" data-calibration-action="delete" data-index="${index}" type="button">Delete</button></td>
      </tr>
    `;
  }).join("");
  renderCalibrationFit();
  renderCalibrationChart();
  updateCalibrationButtons(lastStatus);
}

function renderCalibrationFit() {
  if (!calibrationFit) {
    $("calibration-slope").textContent = "--";
    $("calibration-intercept").textContent = "--";
    $("calibration-rms-error").textContent = "--";
    $("calibration-max-error").textContent = "--";
    $("calibration-constants").textContent = "--";
    return;
  }

  $("calibration-slope").textContent =
    `${Number(calibrationFit.slope_n_per_count).toPrecision(8)} N/count`;
  $("calibration-intercept").textContent = `${number(calibrationFit.intercept_n, 4)} N`;
  $("calibration-rms-error").textContent = `${number(calibrationFit.rms_error_n, 4)} N`;
  $("calibration-max-error").textContent =
    `${number(calibrationFit.max_abs_error_n, 4)} N (${number(calibrationFit.max_percent_span_error, 3)}%)`;
  $("calibration-constants").textContent = calibrationFit.constants_block || "--";
}

function renderCalibrationChart() {
  const chart = $("calibration-fit-chart");
  const summary = $("calibration-chart-summary");
  if (!chart) {
    return;
  }
  if (!window.Plotly) {
    chart.textContent = "Plotly failed to load";
    return;
  }

  const pointTrace = {
    type: "scatter",
    mode: "markers",
    name: "Measured points",
    x: calibrationPoints.map((point) => point.raw_adc_mean),
    y: calibrationPoints.map((point) => point.reference_force_n),
    customdata: calibrationPoints.map((point, index) => [
      index + 1,
      point.raw_adc_stddev,
      point.sample_count,
    ]),
    marker: {
      color: "#176a8f",
      line: { color: "#0d4f70", width: 1 },
      size: 9,
    },
    hovertemplate:
      "Point %{customdata[0]}<br>Mean raw ADC %{x:.2f}<br>Reference %{y:.4f} N<br>Std dev %{customdata[1]:.2f}<br>Samples %{customdata[2]}<extra></extra>",
  };
  const traces = [pointTrace];
  const fitTrace = buildCalibrationFitLine();
  if (fitTrace) {
    traces.push(fitTrace);
  }

  const emptyText = calibrationPoints.length === 0
    ? "No calibration points"
    : "";
  const layout = calibrationChartLayout(emptyText);
  layout.showlegend = calibrationPoints.length > 0;
  const plot = chart.dataset.plotlyReady === "1"
    ? window.Plotly.react(chart, traces, layout, calibrationChartConfig)
    : window.Plotly.newPlot(chart, traces, layout, calibrationChartConfig);
  chart.dataset.plotlyReady = "1";
  plot.catch(() => {
    chart.dataset.plotlyReady = "0";
  });

  if (summary) {
    const pointLabel = calibrationPoints.length === 1 ? "point" : "points";
    summary.textContent = calibrationFit
      ? `${calibrationPoints.length} ${pointLabel}, fit ready`
      : `${calibrationPoints.length} ${pointLabel}`;
  }
}

function buildCalibrationFitLine() {
  if (!calibrationFit || calibrationPoints.length < 2) {
    return null;
  }
  const rawValues = calibrationPoints
    .map((point) => Number(point.raw_adc_mean))
    .filter(Number.isFinite);
  if (rawValues.length < 2) {
    return null;
  }
  const minimum = Math.min(...rawValues);
  const maximum = Math.max(...rawValues);
  const span = maximum - minimum;
  if (span <= 0) {
    return null;
  }
  const padding = span * 0.06;
  const x0 = minimum - padding;
  const x1 = maximum + padding;
  const slope = Number(calibrationFit.slope_n_per_count);
  const intercept = Number(calibrationFit.intercept_n);
  if (!Number.isFinite(slope) || !Number.isFinite(intercept)) {
    return null;
  }
  return {
    type: "scatter",
    mode: "lines",
    name: "Linear fit",
    x: [x0, x1],
    y: [
      (slope * x0) + intercept,
      (slope * x1) + intercept,
    ],
    line: { color: "#b45f06", width: 2 },
    hovertemplate:
      "Fit<br>Raw ADC %{x:.2f}<br>Force %{y:.4f} N<extra></extra>",
  };
}

function calibrationChartLayout(emptyText) {
  const layout = {
    autosize: true,
    showlegend: true,
    margin: { l: 62, r: 20, t: 22, b: 52 },
    paper_bgcolor: "#ffffff",
    plot_bgcolor: "#ffffff",
    font: {
      family: "Arial, Helvetica, sans-serif",
      size: 12,
      color: "#172026",
    },
    hovermode: "closest",
    legend: {
      orientation: "h",
      x: 0,
      xanchor: "left",
      y: 1.12,
      yanchor: "bottom",
      font: { size: 12 },
      itemsizing: "constant",
    },
    xaxis: calibrationAxisLayout("Mean raw ADC"),
    yaxis: calibrationAxisLayout("Reference force (N)"),
    uirevision: "calibration-fit",
  };

  if (emptyText) {
    layout.annotations = [{
      text: emptyText,
      x: 0.5,
      y: 0.5,
      xref: "paper",
      yref: "paper",
      showarrow: false,
      font: { color: "#69757d", size: 13 },
    }];
  }

  return layout;
}

function calibrationAxisLayout(title) {
  return {
    title: { text: title, standoff: 8 },
    showline: true,
    linecolor: "#d9e0e4",
    linewidth: 1,
    mirror: true,
    gridcolor: "#eef2f4",
    zeroline: false,
    automargin: true,
  };
}

async function refreshCalibrationFit({ silent = false } = {}) {
  if (!calibrationCanFit()) {
    calibrationFit = null;
    renderCalibrationPoints();
    return false;
  }

  calibrationFitInFlight = true;
  updateCalibrationButtons(lastStatus);
  try {
    const { response, data } = await requestJson("/api/calibration/fit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ points: calibrationPoints }),
    });
    if (!response.ok) {
      if (!silent) {
        setCalibrationMessage(data.detail || `HTTP ${response.status}`);
      }
      return false;
    }
    calibrationFit = data;
    renderCalibrationPoints();
    if (!silent) {
      setCalibrationMessage("Linear calibration fit complete.");
    }
    return true;
  } catch (error) {
    if (!silent) {
      setCalibrationMessage(`Calibration fit error: ${error}`);
    }
    return false;
  } finally {
    calibrationFitInFlight = false;
    updateCalibrationButtons(lastStatus);
  }
}

async function captureCalibrationPoint(referenceForceN) {
  calibrationSampleInFlight = true;
  calibrationSampleStartedAt = Date.now();
  updateButtons(lastStatus);
  setCalibrationMessage("Capturing 12 second ADC average.");
  try {
    const { response, data } = await requestJson("/api/calibration/sample", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reference_force_n: referenceForceN }),
    });
    if (!response.ok) {
      setCalibrationMessage(data.detail || `HTTP ${response.status}`);
      return;
    }
    calibrationPoints.push(data);
    calibrationFit = null;
    renderCalibrationPoints();
    await refreshCalibrationFit({ silent: true });
    setCalibrationMessage(`Captured ${number(referenceForceN, 3)} N calibration point.`);
  } catch (error) {
    setCalibrationMessage(`Calibration sample error: ${error}`);
  } finally {
    calibrationSampleInFlight = false;
    updateButtons(lastStatus);
  }
}

function captureCalibrationZero() {
  if (hasCalibrationZero()) {
    setCalibrationMessage("Zero point already captured.");
    return;
  }
  captureCalibrationPoint(0);
}

function captureCalibrationLoad() {
  const referenceForceN = Number($("calibration-reference-force").value);
  if (!Number.isFinite(referenceForceN) || Math.abs(referenceForceN) < 1e-9) {
    setCalibrationMessage("Enter a non-zero reference force in N.");
    return;
  }
  captureCalibrationPoint(referenceForceN);
}

async function fitCalibration() {
  const fitUpdated = await refreshCalibrationFit({ silent: false });
  if (!fitUpdated && !calibrationCanFit()) {
    setCalibrationMessage("Capture zero and at least one distinct load point first.");
  }
}

function deleteCalibrationPoint(index) {
  calibrationPoints.splice(index, 1);
  calibrationFit = null;
  renderCalibrationPoints();
  refreshCalibrationFit({ silent: true });
  setCalibrationMessage(
    calibrationPoints.length === 0
      ? "No calibration points captured."
      : "Calibration point deleted.",
  );
}

function resetCalibrationRun() {
  calibrationPoints = [];
  calibrationFit = null;
  renderCalibrationPoints();
  setCalibrationMessage("No calibration points captured.");
}

async function copyCalibrationConstants() {
  if (!calibrationFit?.constants_block) {
    return;
  }
  try {
    await navigator.clipboard.writeText(calibrationFit.constants_block);
    setCalibrationMessage("Calibration constants copied.");
  } catch (error) {
    setCalibrationMessage(`Copy failed: ${error}`);
  }
}

function calibrationExportPayload() {
  return {
    exported_at: new Date().toISOString(),
    averaging_duration_s: CALIBRATION_SAMPLE_DURATION_MS / 1000,
    points: calibrationPoints,
    fit: calibrationFit,
  };
}

function downloadBlob(filename, mimeType, contents) {
  const blob = new Blob([contents], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function downloadCalibrationJson() {
  if (calibrationPoints.length === 0) {
    return;
  }
  downloadBlob(
    "load-cell-calibration.json",
    "application/json",
    `${JSON.stringify(calibrationExportPayload(), null, 2)}\n`,
  );
}

function csvValue(value) {
  const text = String(value ?? "");
  return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

function downloadCalibrationCsv() {
  if (calibrationPoints.length === 0) {
    return;
  }
  const residuals = calibrationFit?.residuals || [];
  const rows = [[
    "index",
    "reference_force_n",
    "raw_adc_mean",
    "raw_adc_stddev",
    "raw_adc_min",
    "raw_adc_max",
    "sample_count",
    "duration_s",
    "predicted_force_n",
    "residual_force_n",
  ]];
  calibrationPoints.forEach((point, index) => {
    const residual = residuals[index] || {};
    rows.push([
      index + 1,
      point.reference_force_n,
      point.raw_adc_mean,
      point.raw_adc_stddev,
      point.raw_adc_min,
      point.raw_adc_max,
      point.sample_count,
      point.duration_s,
      residual.predicted_force_n ?? "",
      residual.residual_force_n ?? "",
    ]);
  });
  const csv = rows.map((row) => row.map(csvValue).join(",")).join("\n");
  downloadBlob("load-cell-calibration.csv", "text/csv", `${csv}\n`);
}

async function refreshOverlay() {
  if (!overlayEnabled || overlayRefreshInFlight) {
    return;
  }
  overlayRefreshInFlight = true;
  try {
    const { response, data } = await requestJson(
      "/api/test/samples/overlay",
      { cache: "no-store" },
    );
    if (response.ok) {
      liveCharts.setOverlaySeries(data.series || []);
    }
  } catch (_) {
  } finally {
    overlayRefreshInFlight = false;
  }
}

async function refreshPlotData() {
  if (plotRefreshInFlight) {
    return;
  }
  plotRefreshInFlight = true;
  const generation = plotGeneration;
  try {
    const { response, data } = await requestJson(
      `/api/test/plots?after=${plotCursor}`,
      { cache: "no-store" },
    );
    if (!response.ok || generation !== plotGeneration) {
      return;
    }

    if (plotResetId !== data.reset_id) {
      plotResetId = data.reset_id;
      plotCursor = 0;
      liveCharts.setLivePoints([]);
    }

    const points = Array.isArray(data.points) ? data.points : [];
    if (points.length > 0) {
      liveCharts.appendPoints(points);
      const lastIndex = Number(points[points.length - 1].index);
      if (Number.isFinite(lastIndex)) {
        plotCursor = lastIndex;
      }
    }
  } catch (_) {
  } finally {
    plotRefreshInFlight = false;
  }
}

async function clearPlots() {
  plotGeneration += 1;
  liveCharts.reset();
  plotCursor = 0;
  try {
    const { response, data } = await requestJson(
      "/api/test/plots/clear",
      { method: "POST" },
    );
    if (response.ok) {
      plotResetId = data.reset_id;
    } else {
      setMessage(data.detail || `HTTP ${response.status}`);
    }
  } catch (error) {
    setMessage(`Plot clear error: ${error}`);
  }
}

async function postJson(url, body = {}) {
  commandInFlight = true;
  updateButtons(lastStatus);
  try {
    const { response, data } = await requestJson(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      setMessage(data.detail || `HTTP ${response.status}`);
      return;
    }
    updatePage(data);
  } catch (error) {
    setMessage(`Web app error: ${error}`);
  } finally {
    commandInFlight = false;
    updateButtons(lastStatus);
  }
}

async function startTest() {
  readStepsFromTable();
  const sampleId = $("sample-id").value.trim();
  lastSubmittedSampleId = sampleId;
  await postJson("/api/test/start", {
    steps,
    sample: {
      id: sampleId,
      notes: $("sample-notes").value.trim(),
    },
  });
}

async function returnToZero() {
  const mode = $("return-zero-mode").value;
  await postJson("/api/test/return-zero", {
    mode,
    rate_value_per_s: Number($("return-zero-rate").value),
  });
}

async function moveRelative(offsetMm) {
  await postJson("/api/test/move-relative", { offset_mm: offsetMm });
}

async function pauseOrResumeTest() {
  const url = lastStatus === "PAUSED" ? "/api/test/resume" : "/api/test/pause";
  await postJson(url);
}

async function tareLoad() {
  if (tareInFlight || displacementZeroInFlight) {
    return;
  }

  tareInFlight = true;
  updateButtons(lastStatus);
  setMessage("Collecting tare readings for 5 seconds.");
  try {
    const { response, data } = await requestJson(
      "/api/tare",
      { method: "POST" },
    );
    if (!response.ok) {
      setMessage(data.detail || `HTTP ${response.status}`);
      return;
    }
    setMessage(data.last_message || "Load reading tared.");
    await refresh();
  } catch (error) {
    setMessage(`Tare error: ${error}`);
  } finally {
    tareInFlight = false;
    updateButtons(lastStatus);
  }
}

async function zeroDisplacement() {
  if (tareInFlight || displacementZeroInFlight) {
    return;
  }

  displacementZeroInFlight = true;
  updateButtons(lastStatus);
  setMessage("Zeroing displacement at the current position.");
  try {
    const { response, data } = await requestJson(
      "/api/zero-displacement",
      { method: "POST" },
    );
    if (!response.ok) {
      setMessage(data.detail || `HTTP ${response.status}`);
      return;
    }
    setMessage(data.last_message || "Displacement zeroed.");
    await refresh();
  } catch (error) {
    setMessage(`Displacement zero error: ${error}`);
  } finally {
    displacementZeroInFlight = false;
    updateButtons(lastStatus);
  }
}

async function refresh() {
  try {
    const { data } = await requestJson(
      "/api/test/state",
      { cache: "no-store" },
    );
    updatePage(data);
    refreshPlotData();
  } catch (error) {
    updateConnection(false);
    setMessage(`Web app error: ${error}`);
  }
}

function updateReturnZeroRateControl() {
  const mode = $("return-zero-mode").value;
  const config = returnZeroRates[mode];
  $("return-zero-rate-label").textContent = config.label;
  $("return-zero-rate").step = config.step;
  if (config.max) {
    $("return-zero-rate").max = config.max;
  } else {
    $("return-zero-rate").removeAttribute("max");
  }
  $("return-zero-rate").value = config.value;
}

$("step-body").addEventListener("input", (event) => {
  const input = event.target;
  if (!input.dataset.field) {
    return;
  }
  const index = Number(input.dataset.index);
  steps[index][input.dataset.field] = input.dataset.field.endsWith("_type")
    ? input.value
    : Number(input.value);
});

$("step-body").addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button) {
    return;
  }
  const index = Number(button.dataset.index);
  const action = button.dataset.action;
  if (action === "remove") {
    removeStep(index);
  }
});

$("sample-body").addEventListener("change", (event) => {
  const checkbox = event.target.closest(".include-checkbox");
  if (!checkbox) {
    return;
  }
  postJson("/api/test/samples/include", {
    index: Number(checkbox.dataset.sampleIndex),
    included: checkbox.checked,
  });
});

$("overlay-toggle").addEventListener("change", () => {
  overlayEnabled = $("overlay-toggle").checked;
  if (overlayEnabled) {
    refreshOverlay();
  } else {
    liveCharts.setOverlaySeries([]);
  }
});

$("return-zero-mode").addEventListener("change", updateReturnZeroRateControl);
$("add-step-button").addEventListener("click", addStep);
$("start-button").addEventListener("click", startTest);
$("return-zero-button").addEventListener("click", returnToZero);
for (const button of document.querySelectorAll(".relative-move-button")) {
  button.addEventListener("click", () => moveRelative(Number(button.dataset.offsetMm)));
}
$("tare-button").addEventListener("click", tareLoad);
$("zero-displacement-button").addEventListener("click", zeroDisplacement);
$("clear-plots-button").addEventListener("click", clearPlots);
$("pause-button").addEventListener("click", pauseOrResumeTest);
$("stop-button").addEventListener("click", () => postJson("/api/test/stop"));
$("clear-samples-button").addEventListener("click", () => postJson("/api/test/samples/clear"));
$("capture-zero-button").addEventListener("click", captureCalibrationZero);
$("capture-load-button").addEventListener("click", captureCalibrationLoad);
$("fit-calibration-button").addEventListener("click", fitCalibration);
$("copy-calibration-button").addEventListener("click", copyCalibrationConstants);
$("download-calibration-json-button").addEventListener("click", downloadCalibrationJson);
$("download-calibration-csv-button").addEventListener("click", downloadCalibrationCsv);
$("reset-calibration-button").addEventListener("click", resetCalibrationRun);
$("calibration-reference-force").addEventListener("input", () => updateCalibrationButtons(lastStatus));

$("calibration-point-body").addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button || button.dataset.calibrationAction !== "delete") {
    return;
  }
  deleteCalibrationPoint(Number(button.dataset.index));
});

$("serial-log").addEventListener("scroll", () => {
  const serialLog = $("serial-log");
  serialAutoScroll =
    serialLog.scrollHeight - serialLog.scrollTop - serialLog.clientHeight < 12;
});

for (const slider of motionSliders()) {
  slider.addEventListener("pointerdown", beginMotionEdit);
  slider.addEventListener("focus", beginMotionEdit);
  slider.addEventListener("input", scheduleMotionUpdate);
  slider.addEventListener("change", finishMotionEdit);
  slider.addEventListener("pointerup", finishMotionEdit);
  slider.addEventListener("blur", finishMotionEdit);
}

for (const button of document.querySelectorAll(".tab-button")) {
  button.addEventListener("click", () => switchTab(button.dataset.tab));
}
document.addEventListener("visibilitychange", syncLiveChartVisibility);

renderSteps();
renderCalibrationPoints();
updateMotionLabels();
updateReturnZeroRateControl();
syncLiveChartVisibility();
refresh();
window.setInterval(refresh, 250);
