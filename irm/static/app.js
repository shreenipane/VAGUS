// Intelligent Resource Manager Dashboard JS
// All text injected via textContent only. No inline scripts, styles, or external dependencies.

const SVG_NS = "http://www.w3.org/2000/svg";

let selectedCgroup = null;
let lastCgroupsData = [];

document.addEventListener("DOMContentLoaded", () => {
  refreshAll();
  setInterval(refreshAll, 5000);
});

async function refreshAll() {
  try {
    const [cgroupsRes, recsRes, reportsRes] = await Promise.all([
      fetch("/api/cgroups"),
      fetch("/api/recommendations"),
      fetch("/api/reports"),
    ]);

    if (cgroupsRes.ok) {
      const cgroups = await cgroupsRes.json();
      lastCgroupsData = cgroups || [];
      renderMonitor(lastCgroupsData);
    }

    if (recsRes.ok) {
      const recs = await recsRes.json();
      renderPlan(recs);
    }

    if (reportsRes.ok) {
      const reports = await reportsRes.json();
      renderAnalyse(reports);
    }
  } catch (err) {
    // Network / parse error, maintain current UI state without crashing
  }
}

function clearElement(el) {
  while (el && el.firstChild) {
    el.removeChild(el.firstChild);
  }
}

function renderMonitor(cgroups) {
  const hostRow = cgroups.find((c) => c.cgroup === "host");
  const hostCpuEl = document.getElementById("host-cpu");
  const hostMemEl = document.getElementById("host-mem");
  const hostPsiEl = document.getElementById("host-psi");

  if (hostRow) {
    hostCpuEl.textContent = hostRow.cpu_cores != null ? hostRow.cpu_cores.toFixed(2) + " cores" : "-";
    hostMemEl.textContent = hostRow.mem_bytes != null ? (hostRow.mem_bytes / (1024 * 1024 * 1024)).toFixed(2) + " GiB" : "-";
    hostPsiEl.textContent = hostRow.cpu_psi != null ? hostRow.cpu_psi.toFixed(2) : "-";
  } else {
    hostCpuEl.textContent = "-";
    hostMemEl.textContent = "-";
    hostPsiEl.textContent = "-";
  }

  const leaves = cgroups
    .filter((c) => c.cgroup !== "host")
    .sort((a, b) => (b.cpu_cores || 0) - (a.cpu_cores || 0));

  const top12 = leaves.slice(0, 12);

  // Default selection to busiest cgroup if none selected or no longer present
  if (!selectedCgroup || !top12.some((c) => c.cgroup === selectedCgroup)) {
    if (top12.length > 0) {
      selectedCgroup = top12[0].cgroup;
    }
  }

  const tbody = document.getElementById("cgroups-tbody");
  clearElement(tbody);

  top12.forEach((cg) => {
    const tr = document.createElement("tr");
    if (cg.cgroup === selectedCgroup) {
      tr.classList.add("selected-row");
    }

    const shortName = cg.cgroup.split("/").filter(Boolean).pop() || cg.cgroup;

    const tdCg = document.createElement("td");
    tdCg.textContent = shortName;
    tdCg.setAttribute("title", cg.cgroup);

    const tdCpu = document.createElement("td");
    tdCpu.textContent = cg.cpu_cores != null ? cg.cpu_cores.toFixed(2) : "-";

    const tdMem = document.createElement("td");
    tdMem.textContent = cg.mem_bytes != null ? (cg.mem_bytes / (1024 * 1024)).toFixed(1) : "-";

    const tdTh = document.createElement("td");
    tdTh.textContent = cg.throttled_ratio != null ? (cg.throttled_ratio * 100).toFixed(1) + "%" : "-";

    const tdPsi = document.createElement("td");
    tdPsi.textContent = cg.cpu_psi != null ? cg.cpu_psi.toFixed(2) : "-";

    tr.appendChild(tdCg);
    tr.appendChild(tdCpu);
    tr.appendChild(tdMem);
    tr.appendChild(tdTh);
    tr.appendChild(tdPsi);

    tr.addEventListener("click", () => {
      selectedCgroup = cg.cgroup;
      renderMonitor(lastCgroupsData);
      renderLiveChart();
    });

    tbody.appendChild(tr);
  });

  renderLiveChart();
}

async function renderLiveChart() {
  const chartContainer = document.getElementById("chart-container");
  const chartSelection = document.getElementById("chart-selection");

  if (!selectedCgroup) {
    chartSelection.textContent = "Selected: none";
    clearElement(chartContainer);
    const msg = document.createElement("div");
    msg.classList.add("placeholder");
    msg.textContent = "No cgroup selected";
    chartContainer.appendChild(msg);
    return;
  }

  chartSelection.textContent = "Selected: " + selectedCgroup;

  try {
    const [cpuRes, thRes] = await Promise.all([
      fetch("/api/series?cgroup=" + encodeURIComponent(selectedCgroup) + "&metric=cpu_cores&hours=1"),
      fetch("/api/series?cgroup=" + encodeURIComponent(selectedCgroup) + "&metric=throttled_ratio&hours=1"),
    ]);

    if (!cpuRes.ok) {
      clearElement(chartContainer);
      const msg = document.createElement("div");
      msg.classList.add("placeholder");
      msg.textContent = "Unable to load series data";
      chartContainer.appendChild(msg);
      return;
    }

    const cpuPoints = await cpuRes.json();
    const thPoints = thRes.ok ? await thRes.json() : [];

    if (!Array.isArray(cpuPoints) || cpuPoints.length === 0) {
      clearElement(chartContainer);
      const msg = document.createElement("div");
      msg.classList.add("placeholder");
      msg.textContent = "No data points for selected cgroup";
      chartContainer.appendChild(msg);
      return;
    }

    clearElement(chartContainer);
    const svg = createChartSvg(cpuPoints, thPoints);
    chartContainer.appendChild(svg);
  } catch (err) {
    clearElement(chartContainer);
    const msg = document.createElement("div");
    msg.classList.add("placeholder");
    msg.textContent = "Error rendering chart";
    chartContainer.appendChild(msg);
  }
}

function createChartSvg(cpuPoints, thPoints) {
  const width = 520;
  const height = 240;
  const padLeft = 45;
  const padRight = 20;
  const padTop = 25;
  const padBottom = 35;
  const plotW = width - padLeft - padRight;
  const plotH = height - padTop - padBottom;

  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.classList.add("chart-svg");

  let maxCpu = 0;
  cpuPoints.forEach((p) => {
    if (p.value != null && p.value > maxCpu) maxCpu = p.value;
  });
  if (maxCpu <= 0) maxCpu = 1.0;
  const yMax = maxCpu * 1.15;

  let minTs = cpuPoints[0].ts;
  let maxTs = cpuPoints[cpuPoints.length - 1].ts;
  if (minTs === maxTs) {
    maxTs = minTs + 1;
  }

  const getX = (ts) => padLeft + ((ts - minTs) / (maxTs - minTs)) * plotW;
  const getY = (val) => padTop + plotH - (Math.max(0, val) / yMax) * plotH;

  // Grid lines and Y axis ticks
  const yTicks = 4;
  for (let i = 0; i <= yTicks; i++) {
    const val = (yMax / yTicks) * i;
    const yPos = getY(val);

    const gridLine = document.createElementNS(SVG_NS, "line");
    gridLine.setAttribute("x1", padLeft);
    gridLine.setAttribute("y1", yPos);
    gridLine.setAttribute("x2", padLeft + plotW);
    gridLine.setAttribute("y2", yPos);
    gridLine.setAttribute("stroke", "var(--chart-grid)");
    gridLine.setAttribute("stroke-width", "1");
    svg.appendChild(gridLine);

    const text = document.createElementNS(SVG_NS, "text");
    text.setAttribute("x", padLeft - 6);
    text.setAttribute("y", yPos + 3);
    text.setAttribute("text-anchor", "end");
    text.setAttribute("fill", "var(--chart-text)");
    text.setAttribute("font-size", "10");
    text.textContent = val.toFixed(1);
    svg.appendChild(text);
  }

  // X axis line
  const xLine = document.createElementNS(SVG_NS, "line");
  xLine.setAttribute("x1", padLeft);
  xLine.setAttribute("y1", padTop + plotH);
  xLine.setAttribute("x2", padLeft + plotW);
  xLine.setAttribute("y2", padTop + plotH);
  xLine.setAttribute("stroke", "var(--border)");
  xLine.setAttribute("stroke-width", "1");
  svg.appendChild(xLine);

  // X axis labels
  const xTicks = 4;
  for (let i = 0; i <= xTicks; i++) {
    const ts = minTs + ((maxTs - minTs) / xTicks) * i;
    const xPos = getX(ts);
    const date = new Date(ts * 1000);
    const h = String(date.getHours()).padStart(2, "0");
    const m = String(date.getMinutes()).padStart(2, "0");
    const s = String(date.getSeconds()).padStart(2, "0");

    const text = document.createElementNS(SVG_NS, "text");
    text.setAttribute("x", xPos);
    text.setAttribute("y", padTop + plotH + 16);
    text.setAttribute("text-anchor", "middle");
    text.setAttribute("fill", "var(--chart-text)");
    text.setAttribute("font-size", "10");
    text.textContent = `${h}:${m}:${s}`;
    svg.appendChild(text);
  }

  // Axis Labels
  const yLabel = document.createElementNS(SVG_NS, "text");
  yLabel.setAttribute("x", padLeft);
  yLabel.setAttribute("y", 12);
  yLabel.setAttribute("fill", "var(--chart-text)");
  yLabel.setAttribute("font-size", "10");
  yLabel.setAttribute("text-anchor", "start");
  yLabel.textContent = "cores";
  svg.appendChild(yLabel);

  // Build Line 1: CPU Cores
  let pathD1 = "";
  cpuPoints.forEach((p, idx) => {
    const x = getX(p.ts);
    const y = getY(p.value || 0);
    pathD1 += (idx === 0 ? "M " : " L ") + x.toFixed(1) + " " + y.toFixed(1);
  });

  const path1 = document.createElementNS(SVG_NS, "path");
  path1.setAttribute("d", pathD1);
  path1.setAttribute("fill", "none");
  path1.setAttribute("stroke", "var(--chart-line1)");
  path1.setAttribute("stroke-width", "2");
  svg.appendChild(path1);

  // Build Line 2: throttled_ratio * max(cpu)
  if (Array.isArray(thPoints) && thPoints.length > 0) {
    let pathD2 = "";
    thPoints.forEach((p, idx) => {
      const x = getX(p.ts);
      const val = (p.value || 0) * maxCpu;
      const y = getY(val);
      pathD2 += (idx === 0 ? "M " : " L ") + x.toFixed(1) + " " + y.toFixed(1);
    });

    const path2 = document.createElementNS(SVG_NS, "path");
    path2.setAttribute("d", pathD2);
    path2.setAttribute("fill", "none");
    path2.setAttribute("stroke", "var(--chart-line2)");
    path2.setAttribute("stroke-width", "1.5");
    path2.setAttribute("stroke-dasharray", "4,2");
    svg.appendChild(path2);
  }

  // Legend
  const legG = document.createElementNS(SVG_NS, "g");

  // Line 1 legend
  const legLine1 = document.createElementNS(SVG_NS, "line");
  legLine1.setAttribute("x1", width - 210);
  legLine1.setAttribute("y1", 10);
  legLine1.setAttribute("x2", width - 190);
  legLine1.setAttribute("y2", 10);
  legLine1.setAttribute("stroke", "var(--chart-line1)");
  legLine1.setAttribute("stroke-width", "2");
  legG.appendChild(legLine1);

  const legText1 = document.createElementNS(SVG_NS, "text");
  legText1.setAttribute("x", width - 185);
  legText1.setAttribute("y", 13);
  legText1.setAttribute("fill", "var(--chart-text)");
  legText1.setAttribute("font-size", "10");
  legText1.textContent = "cpu_cores";
  legG.appendChild(legText1);

  // Line 2 legend
  const legLine2 = document.createElementNS(SVG_NS, "line");
  legLine2.setAttribute("x1", width - 125);
  legLine2.setAttribute("y1", 10);
  legLine2.setAttribute("x2", width - 105);
  legLine2.setAttribute("y2", 10);
  legLine2.setAttribute("stroke", "var(--chart-line2)");
  legLine2.setAttribute("stroke-width", "1.5");
  legLine2.setAttribute("stroke-dasharray", "4,2");
  legG.appendChild(legLine2);

  const legText2 = document.createElementNS(SVG_NS, "text");
  legText2.setAttribute("x", width - 100);
  legText2.setAttribute("y", 13);
  legText2.setAttribute("fill", "var(--chart-text)");
  legText2.setAttribute("font-size", "10");
  legText2.textContent = "throttled \u00d7 max";
  legG.appendChild(legText2);

  svg.appendChild(legG);

  return svg;
}

function renderPlan(recs) {
  const tableWrapper = document.getElementById("recs-table-wrapper");
  const placeholder = document.getElementById("recs-placeholder");
  const tbody = document.getElementById("recs-tbody");
  const pairsSection = document.getElementById("pairs-section");
  const pairsTbody = document.getElementById("pairs-tbody");

  clearElement(tbody);
  clearElement(pairsTbody);

  if (!recs || !recs.items || recs.items.length === 0) {
    tableWrapper.classList.add("hidden");
    placeholder.classList.remove("hidden");
    pairsSection.classList.add("hidden");
    return;
  }

  tableWrapper.classList.remove("hidden");
  placeholder.classList.add("hidden");

  recs.items.forEach((item) => {
    const tr = document.createElement("tr");

    const tdCg = document.createElement("td");
    tdCg.textContent = item.cgroup;

    const tdSrc = document.createElement("td");
    tdSrc.textContent = item.source || "-";

    const tdPeak = document.createElement("td");
    tdPeak.textContent = item.peak_cores != null ? item.peak_cores.toFixed(2) : "-";

    const tdMax = document.createElement("td");
    tdMax.textContent = item.cpu_max || "-";

    const tdMem = document.createElement("td");
    tdMem.textContent = item.memory_high || "-";

    const tdReason = document.createElement("td");
    tdReason.textContent = item.reason || "-";

    tr.appendChild(tdCg);
    tr.appendChild(tdSrc);
    tr.appendChild(tdPeak);
    tr.appendChild(tdMax);
    tr.appendChild(tdMem);
    tr.appendChild(tdReason);

    tbody.appendChild(tr);
  });

  if (recs.pairs && recs.pairs.length > 0) {
    pairsSection.classList.remove("hidden");
    recs.pairs.forEach((pair) => {
      const tr = document.createElement("tr");

      const tdA = document.createElement("td");
      tdA.textContent = pair.a;

      const tdB = document.createElement("td");
      tdB.textContent = pair.b;

      const tdRho = document.createElement("td");
      tdRho.textContent = pair.rho != null ? pair.rho.toFixed(2) : "-";

      const tdK = document.createElement("td");
      tdK.textContent = pair.k != null ? pair.k.toFixed(2) : "-";

      tr.appendChild(tdA);
      tr.appendChild(tdB);
      tr.appendChild(tdRho);
      tr.appendChild(tdK);

      pairsTbody.appendChild(tr);
    });
  } else {
    pairsSection.classList.add("hidden");
  }
}

function renderAnalyse(reports) {
  // Overhead line
  const overheadLine = document.getElementById("overhead-line");
  if (reports && reports.overhead) {
    const pct = reports.overhead.pct_of_one_core != null ? reports.overhead.pct_of_one_core.toFixed(2) : "?";
    const cg = reports.overhead.cgroups != null ? Math.round(reports.overhead.cgroups) : "?";
    overheadLine.textContent = `Monitor overhead: ${pct}% of one core (${cg} cgroups)`;
  } else {
    overheadLine.textContent = "Monitor overhead: N/A";
  }

  // Forecast table
  const forecastTbody = document.getElementById("forecast-tbody");
  clearElement(forecastTbody);

  const forecast = reports ? reports.forecast : null;
  const forecastRows = [];

  if (forecast) {
    if (forecast.test_all) {
      if (forecast.test_all.lstm) {
        forecastRows.push({ name: "LSTM", metrics: forecast.test_all.lstm });
      }
      if (forecast.test_all.last_window) {
        forecastRows.push({ name: "Last Window", metrics: forecast.test_all.last_window });
      }
    }
    if (forecast.test_arima_subset && forecast.test_arima_subset.arima) {
      forecastRows.push({ name: "ARIMA", metrics: forecast.test_arima_subset.arima });
    }
  }

  if (forecastRows.length > 0) {
    forecastRows.forEach((r) => {
      const tr = document.createElement("tr");

      const tdModel = document.createElement("td");
      tdModel.textContent = r.name;

      const tdPinball = document.createElement("td");
      tdPinball.textContent = r.metrics.pinball != null ? r.metrics.pinball.toFixed(4) : "-";

      const tdCoverage = document.createElement("td");
      tdCoverage.textContent = r.metrics.coverage != null ? (r.metrics.coverage * 100).toFixed(1) + "%" : "-";

      const tdUnder = document.createElement("td");
      const underVal = r.metrics.mean_under != null ? r.metrics.mean_under : r.metrics.mean_under_prediction;
      tdUnder.textContent = underVal != null ? underVal.toFixed(4) : "-";

      tr.appendChild(tdModel);
      tr.appendChild(tdPinball);
      tr.appendChild(tdCoverage);
      tr.appendChild(tdUnder);

      forecastTbody.appendChild(tr);
    });
  } else {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.setAttribute("colspan", "4");
    td.classList.add("placeholder");
    td.textContent = "No forecast data";
    tr.appendChild(td);
    forecastTbody.appendChild(tr);
  }

  // Placement table
  const placementTbody = document.getElementById("placement-tbody");
  clearElement(placementTbody);

  const placement = reports ? reports.placement : null;
  const placementPolicies = ["FirstFit", "BestFit", "DQN"];
  let hasPlacement = false;

  if (placement) {
    placementPolicies.forEach((pol) => {
      const m = placement[pol];
      if (m) {
        hasPlacement = true;
        const tr = document.createElement("tr");

        const tdPol = document.createElement("td");
        tdPol.textContent = pol;

        const tdEnergy = document.createElement("td");
        tdEnergy.textContent = m.energy_kwh != null ? m.energy_kwh.toFixed(2) : "-";

        const tdSla = document.createElement("td");
        tdSla.textContent = m.sla_overload_frac != null ? (m.sla_overload_frac * 100).toFixed(2) + "%" : "-";

        const tdMig = document.createElement("td");
        tdMig.textContent = m.migrations != null ? m.migrations : "-";

        const tdHosts = document.createElement("td");
        tdHosts.textContent = m.mean_active_hosts != null ? m.mean_active_hosts.toFixed(1) : "-";

        tr.appendChild(tdPol);
        tr.appendChild(tdEnergy);
        tr.appendChild(tdSla);
        tr.appendChild(tdMig);
        tr.appendChild(tdHosts);

        placementTbody.appendChild(tr);
      }
    });
  }

  if (!hasPlacement) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.setAttribute("colspan", "5");
    td.classList.add("placeholder");
    td.textContent = "No placement data";
    tr.appendChild(td);
    placementTbody.appendChild(tr);
  }

  // Forecast example SVG
  const exampleSec = document.getElementById("forecast-example-section");
  const exampleContainer = document.getElementById("forecast-example-container");
  clearElement(exampleContainer);

  if (forecast && forecast.example) {
    exampleSec.classList.remove("hidden");
    const svg = createForecastExampleSvg(forecast.example);
    exampleContainer.appendChild(svg);
  } else {
    exampleSec.classList.add("hidden");
  }
}

function createForecastExampleSvg(ex) {
  const width = 480;
  const height = 180;
  const padLeft = 35;
  const padRight = 15;
  const padTop = 25;
  const padBottom = 25;
  const plotW = width - padLeft - padRight;
  const plotH = height - padTop - padBottom;

  const history = ex.history || [];
  const actual = ex.actual || [];
  const lstm = ex.lstm || [];
  const lastWindow = Array.isArray(ex.last_window) ? ex.last_window : (ex.last_window != null ? new Array(actual.length).fill(ex.last_window) : []);

  const totalSteps = Math.max(1, history.length + actual.length);
  const allVals = [...history, ...actual, ...lstm, ...lastWindow].filter((v) => typeof v === "number" && !isNaN(v));
  const maxVal = allVals.length > 0 ? Math.max(...allVals) * 1.15 : 1.0;
  const minVal = 0;

  const getX = (idx) => padLeft + (idx / (totalSteps - 1)) * plotW;
  const getY = (val) => padTop + plotH - ((val - minVal) / (maxVal - minVal)) * plotH;

  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.classList.add("chart-svg");

  // X axis
  const axis = document.createElementNS(SVG_NS, "line");
  axis.setAttribute("x1", padLeft);
  axis.setAttribute("y1", padTop + plotH);
  axis.setAttribute("x2", padLeft + plotW);
  axis.setAttribute("y2", padTop + plotH);
  axis.setAttribute("stroke", "var(--border)");
  svg.appendChild(axis);

  const makePath = (points, startIndex, stroke, dash) => {
    if (points.length === 0) return null;
    let d = "";
    points.forEach((val, idx) => {
      const x = getX(startIndex + idx);
      const y = getY(val);
      d += (idx === 0 ? "M " : " L ") + x.toFixed(1) + " " + y.toFixed(1);
    });
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", d);
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", stroke);
    path.setAttribute("stroke-width", "1.5");
    if (dash) path.setAttribute("stroke-dasharray", dash);
    return path;
  };

  if (history.length > 0) {
    const pHistory = makePath(history, 0, "#2563eb", null);
    if (pHistory) svg.appendChild(pHistory);
  }

  const startIdx = history.length > 0 ? history.length - 1 : 0;
  const attachHistoryStart = (arr) => (history.length > 0 ? [history[history.length - 1], ...arr] : arr);

  if (actual.length > 0) {
    const pActual = makePath(attachHistoryStart(actual), startIdx, "#16a34a", null);
    if (pActual) svg.appendChild(pActual);
  }

  if (lstm.length > 0) {
    const pLstm = makePath(attachHistoryStart(lstm), startIdx, "#8b5cf6", "3,2");
    if (pLstm) svg.appendChild(pLstm);
  }

  if (lastWindow.length > 0) {
    const pLw = makePath(attachHistoryStart(lastWindow), startIdx, "#f59e0b", "2,2");
    if (pLw) svg.appendChild(pLw);
  }

  // Legend
  const legItems = [
    { label: "history", color: "#2563eb", dash: null },
    { label: "actual", color: "#16a34a", dash: null },
    { label: "lstm", color: "#8b5cf6", dash: "3,2" },
    { label: "last_window", color: "#f59e0b", dash: "2,2" },
  ];

  let legX = padLeft;
  legItems.forEach((item) => {
    const line = document.createElementNS(SVG_NS, "line");
    line.setAttribute("x1", legX);
    line.setAttribute("y1", 12);
    line.setAttribute("x2", legX + 16);
    line.setAttribute("y2", 12);
    line.setAttribute("stroke", item.color);
    line.setAttribute("stroke-width", "2");
    if (item.dash) line.setAttribute("stroke-dasharray", item.dash);
    svg.appendChild(line);

    const txt = document.createElementNS(SVG_NS, "text");
    txt.setAttribute("x", legX + 20);
    txt.setAttribute("y", 15);
    txt.setAttribute("fill", "var(--chart-text)");
    txt.setAttribute("font-size", "10");
    txt.textContent = item.label;
    svg.appendChild(txt);

    legX += 80;
  });

  return svg;
}
