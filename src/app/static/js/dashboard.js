(function () {
  const stateEl = document.getElementById("machine-state");
  const forceEl = document.getElementById("force-value");
  const targetEl = document.getElementById("target-value");
  const stepRateEl = document.getElementById("step-rate-value");
  const displacementEl = document.getElementById("displacement-value");
  const messageEl = document.getElementById("controller-message");
  const plotEl = document.getElementById("force-plot");
  const armForm = document.getElementById("arm-form");
  const feedbackEl = document.getElementById("run-form-feedback");
  const runActionSection = document.getElementById("run-action-section");
  const stateActionsEl = document.getElementById("state-actions");
  const zeroButton = document.getElementById("zero-load-button");
  const armButton = document.getElementById("arm-run-button");
  const maxPoints = window.TENSILE_DASHBOARD?.telemetryWindowPoints || 600;
  const workflowFields = armForm ? Array.from(armForm.querySelectorAll("input, select, textarea")) : [];
  const actionSets = {
    ARMED: [
      { label: "Start", url: "/api/machine/start", tone: "primary" },
      { label: "Cancel", url: "/api/machine/cancel-arm" }
    ],
    RUNNING: [
      { label: "Pause", url: "/api/machine/pause" },
      { label: "Abort", url: "/api/machine/abort", tone: "danger" }
    ],
    RETURNING_TO_ZERO: [
      { label: "Pause", url: "/api/machine/pause" },
      { label: "Abort", url: "/api/machine/abort", tone: "danger" }
    ],
    PAUSED: [
      { label: "Resume", url: "/api/machine/resume", tone: "primary" },
      { label: "Return to Zero", url: "/api/machine/return-zero" },
      { label: "Abort", url: "/api/machine/abort", tone: "danger" }
    ],
    ABORTED: [
      { label: "Acknowledge / Reset", url: "/api/machine/reset-fault" }
    ],
    ESTOPPED: [
      { label: "Acknowledge / Reset", url: "/api/machine/reset-fault" }
    ],
    FAULT: [
      { label: "Acknowledge / Reset", url: "/api/machine/reset-fault" }
    ]
  };

  const traceActual = {
    x: [],
    y: [],
    type: "scatter",
    mode: "lines",
    name: "Actual Force",
    line: { color: "#146c94", width: 2 }
  };
  const traceTarget = {
    x: [],
    y: [],
    type: "scatter",
    mode: "lines",
    name: "Target Force",
    line: { color: "#b83a36", width: 2, dash: "dot" }
  };

  Plotly.newPlot(
    plotEl,
    [traceActual, traceTarget],
    {
      margin: { t: 24, r: 18, b: 42, l: 58 },
      paper_bgcolor: "#ffffff",
      plot_bgcolor: "#ffffff",
      xaxis: { title: "Controller time (s)", gridcolor: "#e2e8ee" },
      yaxis: { title: "Force (N)", gridcolor: "#e2e8ee" },
      legend: { orientation: "h", y: 1.12 }
    },
    { responsive: true, displayModeBar: false }
  );

  function setFeedback(message, tone) {
    if (!feedbackEl) {
      return;
    }
    feedbackEl.innerHTML = `<div class="notice ${tone}">${message}</div>`;
  }

  async function postJson(url, payload) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: payload ? JSON.stringify(payload) : "{}"
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(body.detail || "Request failed.");
    }
    return body;
  }

  async function postCommand(url) {
    try {
      const result = await postJson(url);
      setFeedback(result.message || "Command sent.", "success");
    } catch (error) {
      setFeedback(error.message, "warning");
    }
  }

  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-action]");
    if (!button) {
      return;
    }
    postCommand(button.dataset.action);
  });

  armForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(armForm);
    try {
      const result = await postJson("/api/run/arm", {
        method_id: Number(data.get("method_id")),
        sample_name: String(data.get("sample_name") || ""),
        notes: String(data.get("notes") || "")
      });
      setFeedback(result.message, "success");
    } catch (error) {
      setFeedback(error.message, "warning");
    }
  });

  function updateSnapshot(snapshot) {
    if (stateEl) stateEl.textContent = snapshot.state;
    if (forceEl) forceEl.textContent = Number(snapshot.force_n || 0).toFixed(2);
    if (targetEl) targetEl.textContent = Number(snapshot.target_force_n || 0).toFixed(2);
    if (stepRateEl) stepRateEl.textContent = Number(snapshot.step_rate_steps_s || 0).toFixed(1);
    if (displacementEl) displacementEl.textContent = Number(snapshot.estimated_crosshead_mm || 0).toFixed(3);
    if (messageEl) messageEl.textContent = snapshot.last_message || "";
    renderActions(snapshot.state);
    syncWorkflowAvailability(snapshot.state);
  }

  function renderActions(state) {
    if (!stateActionsEl || !runActionSection) {
      return;
    }
    const actions = actionSets[state] || [];
    runActionSection.hidden = actions.length === 0;
    stateActionsEl.innerHTML = actions
      .map((action) => {
        const tone = action.tone ? ` ${action.tone}` : "";
        return `<button type="button" class="${tone.trim()}" data-action="${action.url}">${action.label}</button>`;
      })
      .join("");
  }

  function syncWorkflowAvailability(state) {
    const idle = state === "IDLE";
    workflowFields.forEach((field) => {
      field.disabled = !idle;
    });
    if (zeroButton) {
      zeroButton.disabled = !idle;
    }
    if (armButton) {
      armButton.disabled = !idle;
    }
  }

  function appendTelemetry(point) {
    const seconds = Number(point.time_ms || 0) / 1000;
    Plotly.extendTraces(
      plotEl,
      {
        x: [[seconds], [seconds]],
        y: [[Number(point.force_n || 0)], [Number(point.target_force_n || 0)]]
      },
      [0, 1],
      maxPoints
    );
  }

  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${scheme}://${window.location.host}/api/ws/telemetry`);
  socket.addEventListener("open", () => socket.send("dashboard-ready"));
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.type === "snapshot") {
      updateSnapshot(message.payload);
    }
    if (message.type === "telemetry") {
      appendTelemetry(message.payload);
    }
  });
})();
