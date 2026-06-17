const PLOT_TRACE_TYPE = "scattergl";
const LIVE_DISPLAY_MAX_POINTS = 5000;
const OVERLAY_DISPLAY_MAX_POINTS_PER_SAMPLE = 2000;

class LiveForceCharts {
  constructor(timeChartId, displacementChartId) {
    this.timeChart = document.getElementById(timeChartId);
    this.displacementChart = document.getElementById(displacementChartId);
    this.livePoints = [];
    this.overlaySeries = [];
    this.pendingPoints = [];
    this.updateQueued = false;
    this.fullRenderNeeded = true;
    this.fullRenderInFlight = false;
    this.incrementalUpdateInFlight = false;
    this.visible = !document.hidden;
    this.hasCommandedForce = false;
    this.showCommandedForce = true;
    this.config = {
      responsive: true,
      displaylogo: false,
      scrollZoom: true,
      modeBarButtonsToRemove: ["select2d", "lasso2d"],
    };

    window.addEventListener("resize", () => this.resize());
    this.scheduleUpdate();
  }

  appendPoints(points) {
    const displayPoints = normalizePlotPoints(recentItems(points, LIVE_DISPLAY_MAX_POINTS));
    if (displayPoints.length === 0) {
      return;
    }
    const hadLivePoints = this.livePoints.length > 0;
    this.livePoints.push(...displayPoints);
    trimToRecent(this.livePoints, LIVE_DISPLAY_MAX_POINTS);
    this.pendingPoints.push(...displayPoints);
    trimToRecent(this.pendingPoints, LIVE_DISPLAY_MAX_POINTS);
    if (!hadLivePoints) {
      this.fullRenderNeeded = true;
    }
    if (!this.hasCommandedForce && hasCommandedForce(displayPoints)) {
      this.hasCommandedForce = true;
      this.fullRenderNeeded = true;
    }
    this.scheduleUpdate();
  }

  setLivePoints(points) {
    this.livePoints = normalizePlotPoints(recentItems(points, LIVE_DISPLAY_MAX_POINTS));
    this.pendingPoints = [];
    this.hasCommandedForce = hasCommandedForce(this.livePoints);
    this.fullRenderNeeded = true;
    this.scheduleUpdate();
  }

  reset() {
    this.livePoints = [];
    this.pendingPoints = [];
    this.hasCommandedForce = false;
    this.fullRenderNeeded = true;
    this.scheduleUpdate();
  }

  setOverlaySeries(series) {
    this.overlaySeries = Array.isArray(series)
      ? series.map(normalizeOverlaySeries).filter((item) => item.points.length > 0)
      : [];
    this.fullRenderNeeded = true;
    this.scheduleUpdate();
  }

  setVisible(visible) {
    const nextVisible = Boolean(visible);
    if (this.visible === nextVisible) {
      return;
    }
    this.visible = nextVisible;
    if (this.visible) {
      this.fullRenderNeeded = true;
      this.draw();
    }
  }

  setCommandedForceVisible(visible) {
    const nextVisible = Boolean(visible);
    if (this.showCommandedForce === nextVisible) {
      return;
    }
    this.showCommandedForce = nextVisible;
    this.fullRenderNeeded = true;
    this.scheduleUpdate();
  }

  draw() {
    this.resize();
    this.fullRenderNeeded = true;
    this.scheduleUpdate();
  }

  resize() {
    if (!this.visible || !window.Plotly) {
      return;
    }
    for (const chart of [this.timeChart, this.displacementChart]) {
      if (chart && chart.dataset.plotlyReady === "1") {
        window.Plotly.Plots.resize(chart);
      }
    }
  }

  scheduleUpdate() {
    if (!this.visible || this.updateQueued) {
      return;
    }
    this.updateQueued = true;
    window.requestAnimationFrame(() => {
      this.updateQueued = false;
      if (this.fullRenderNeeded || !this.chartsReady()) {
        this.renderNow();
      } else {
        this.extendPendingPoints();
      }
    });
  }

  renderNow() {
    if (!this.visible) {
      return;
    }
    if (!window.Plotly) {
      this.showMissingPlotly();
      return;
    }
    if (this.fullRenderInFlight || this.incrementalUpdateInFlight) {
      this.fullRenderNeeded = true;
      return;
    }

    this.fullRenderInFlight = true;
    this.fullRenderNeeded = false;
    this.pendingPoints = [];
    Promise.all([
      this.renderPlot(
        this.timeChart,
        this.timeTraces(),
        "Time (s)",
        "Force (N)",
        "Waiting for telemetry",
      ),
      this.renderPlot(
        this.displacementChart,
        this.displacementTraces(),
        "Displacement (mm)",
        "Force (N)",
        "Waiting for telemetry",
      ),
    ]).finally(() => {
      this.fullRenderInFlight = false;
      if (this.fullRenderNeeded || this.pendingPoints.length > 0) {
        this.scheduleUpdate();
      }
    });
  }

  renderPlot(chart, traces, xTitle, yTitle, emptyText) {
    if (!chart) {
      return Promise.resolve();
    }
    const hasData = traces.some((trace) => Array.isArray(trace.x) && trace.x.length > 0);
    const layout = plotLayout(xTitle, yTitle, hasData ? "" : emptyText);
    const plot = chart.dataset.plotlyReady === "1"
      ? window.Plotly.react(chart, traces, layout, this.config)
      : window.Plotly.newPlot(chart, traces, layout, this.config);
    return Promise.resolve(plot).then(() => {
      chart.dataset.plotlyReady = "1";
    }).catch(() => {
      chart.dataset.plotlyReady = "0";
    });
  }

  chartsReady() {
    return [this.timeChart, this.displacementChart]
      .every((chart) => chart && chart.dataset.plotlyReady === "1");
  }

  extendPendingPoints() {
    if (!this.visible || this.pendingPoints.length === 0) {
      return;
    }
    if (this.fullRenderInFlight || this.incrementalUpdateInFlight) {
      return;
    }
    if (!this.chartsReady()) {
      this.fullRenderNeeded = true;
      this.scheduleUpdate();
      return;
    }

    const points = this.pendingPoints.splice(0);
    const times = points.map((point) => point.timeS);
    const forces = points.map((point) => point.forceN);
    const positions = points.map((point) => point.positionMm);
    const customData = points.map(pointCustomData);

    this.incrementalUpdateInFlight = true;
    let updates;
    try {
      const timeTraceUpdate = {
        x: [times],
        y: [forces],
        customdata: [customData],
      };
      const timeTraceIndexes = [0];
      if (this.hasCommandedForce && this.showCommandedForce) {
        timeTraceUpdate.x.push(times);
        timeTraceUpdate.y.push(points.map((point) => point.commandedForceN));
        timeTraceUpdate.customdata.push(points.map(() => null));
        timeTraceIndexes.push(1);
      }

      updates = [
        window.Plotly.extendTraces(
          this.timeChart,
          timeTraceUpdate,
          timeTraceIndexes,
          LIVE_DISPLAY_MAX_POINTS,
        ),
        window.Plotly.extendTraces(
          this.displacementChart,
          {
            x: [positions],
            y: [forces],
            customdata: [customData],
          },
          [0],
          LIVE_DISPLAY_MAX_POINTS,
        ),
      ];
    } catch (_) {
      this.fullRenderNeeded = true;
      this.incrementalUpdateInFlight = false;
      this.scheduleUpdate();
      return;
    }

    Promise.all(updates).catch(() => {
      this.fullRenderNeeded = true;
    }).finally(() => {
      this.incrementalUpdateInFlight = false;
      if (this.fullRenderNeeded || this.pendingPoints.length > 0) {
        this.scheduleUpdate();
      }
    });
  }

  timeTraces() {
    const measured = {
      type: PLOT_TRACE_TYPE,
      mode: "lines",
      name: "Measured",
      showlegend: true,
      x: this.livePoints.map((point) => point.timeS),
      y: this.livePoints.map((point) => point.forceN),
      customdata: this.livePoints.map(pointCustomData),
      line: { color: "#176a8f", width: 2 },
      hovertemplate:
        "Time %{x:.3f} s<br>Force %{y:.4f} N<br>Phase %{customdata[0]}<br>Step %{customdata[1]}<br>Rate %{customdata[3]:.2f} steps/s<extra></extra>",
    };

    if (!this.hasCommandedForce || !this.showCommandedForce) {
      return [measured];
    }

    return [measured, {
      type: PLOT_TRACE_TYPE,
      mode: "lines",
      name: "Commanded",
      showlegend: true,
      x: this.livePoints.map((point) => point.timeS),
      y: this.livePoints.map((point) => point.commandedForceN),
      customdata: this.livePoints.map(() => null),
      connectgaps: false,
      line: { color: "#9a6a13", width: 2, dash: "dash" },
      hovertemplate: "Time %{x:.3f} s<br>Commanded %{y:.4f} N<extra></extra>",
    }];
  }

  displacementTraces() {
    const traces = [
      {
        type: PLOT_TRACE_TYPE,
        mode: "lines",
        name: "Measured",
        showlegend: true,
        x: this.livePoints.map((point) => point.positionMm),
        y: this.livePoints.map((point) => point.forceN),
        customdata: this.livePoints.map(pointCustomData),
        line: { color: "#176a8f", width: 2 },
        hovertemplate:
          "Displacement %{x:.5f} mm<br>Force %{y:.4f} N<br>Phase %{customdata[0]}<br>Step %{customdata[1]}<extra></extra>",
      },
    ];

    const colors = ["#6f7d87", "#7b5e9a", "#4f7f5a", "#b05d45", "#566bb0"];
    for (let index = 0; index < this.overlaySeries.length; index += 1) {
      const series = this.overlaySeries[index];
      traces.push({
        type: PLOT_TRACE_TYPE,
        mode: "lines",
        name: index === 0 ? "Samples" : series.sampleId || `Sample ${series.index || index + 1}`,
        legendgroup: "samples",
        showlegend: index === 0,
        x: series.points.map((point) => point.positionMm),
        y: series.points.map((point) => point.forceN),
        line: { color: colors[index % colors.length], width: 1.5 },
        opacity: 0.65,
        hovertemplate:
          `${escapeTemplate(series.sampleId || "Sample")}<br>Displacement %{x:.5f} mm<br>Force %{y:.4f} N<extra></extra>`,
      });
    }
    return traces;
  }

  showMissingPlotly() {
    for (const chart of [this.timeChart, this.displacementChart]) {
      if (!chart) {
        continue;
      }
      chart.textContent = "Plotly failed to load";
    }
  }
}

function plotLayout(xTitle, yTitle, emptyText) {
  const layout = {
    autosize: true,
    showlegend: true,
    margin: { l: 58, r: 18, t: 30, b: 48 },
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
      y: 1.14,
      yanchor: "bottom",
      font: { size: 12 },
      itemsizing: "constant",
    },
    xaxis: axisLayout(xTitle),
    yaxis: axisLayout(yTitle),
    uirevision: "operator-view",
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

function axisLayout(title) {
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

function normalizePlotPoints(points) {
  return Array.isArray(points)
    ? points.map(normalizePlotPoint).filter((point) => point !== null)
    : [];
}

function normalizePlotPoint(point) {
  if (!point) {
    return null;
  }
  const timeS = finiteOrNull(point.timeS);
  const forceN = finiteOrNull(point.forceN);
  const positionMm = finiteOrNull(point.positionMm);
  if (timeS === null || forceN === null || positionMm === null) {
    return null;
  }
  return {
    index: Number(point.index) || 0,
    timeS,
    forceN,
    positionMm,
    stepRateStepsS: finiteOrNull(point.stepRateStepsS),
    commandedForceN: finiteOrNull(point.commandedForceN),
    controlMode: point.controlMode || "NONE",
    testPhase: point.testPhase || "NONE",
    stepIndex: Number(point.stepIndex) || 0,
  };
}

function normalizeOverlaySeries(series) {
  return {
    index: series && series.index,
    sampleId: series && series.sample_id ? String(series.sample_id) : "",
    points: normalizeOverlayPoints(decimatePoints(
      series && series.points,
      OVERLAY_DISPLAY_MAX_POINTS_PER_SAMPLE,
    )),
  };
}

function normalizeOverlayPoints(points) {
  return Array.isArray(points)
    ? points.map((point) => ({
        positionMm: finiteOrNull(point.positionMm),
        forceN: finiteOrNull(point.forceN),
      })).filter((point) => point.positionMm !== null && point.forceN !== null)
    : [];
}

function pointCustomData(point) {
  return [
    point.testPhase || "NONE",
    point.stepIndex || 0,
    point.controlMode || "NONE",
    Number.isFinite(point.stepRateStepsS) ? point.stepRateStepsS : 0,
  ];
}

function hasCommandedForce(points) {
  return points.some((point) => Number.isFinite(point.commandedForceN));
}

function trimToRecent(points, maxPoints) {
  if (points.length > maxPoints) {
    points.splice(0, points.length - maxPoints);
  }
}

function recentItems(items, maxItems) {
  return Array.isArray(items) ? items.slice(-maxItems) : [];
}

function decimatePoints(points, maxPoints) {
  if (!Array.isArray(points)) {
    return [];
  }
  if (points.length <= maxPoints) {
    return points;
  }
  const result = [points[0]];
  const interval = (points.length - 1) / (maxPoints - 1);
  for (let index = 1; index < maxPoints - 1; index += 1) {
    result.push(points[Math.round(index * interval)]);
  }
  result.push(points[points.length - 1]);
  return result;
}

function finiteOrNull(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function escapeTemplate(value) {
  return String(value)
    .replace(/[%{}]/g, "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
