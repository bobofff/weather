const $ = (id) => document.getElementById(id);
const API_BASE = window.WEATHER_API_BASE || window.location.origin;
const SELECTED_CITY_STORAGE_KEY = "weatherSelectedCityId";
const PAPER_MONITOR_INTERVAL_MS = 60000;
const PAPER_MONITOR_STATUS_MIN_MS = PAPER_MONITOR_INTERVAL_MS;
const PAPER_MONITOR_STATUS_MAX_MS = PAPER_MONITOR_INTERVAL_MS;
let cityRecords = [];
let latestEnsembleSignalResult = null;
let latestPaperPortfolioResult = null;
let latestPaperMonitorResult = null;
let paperMonitorStatusTimer = null;
let paperMonitorStatusTimerMs = null;
let paperMonitorPortfolioRefreshKey = null;
let paperMonitorHedgeRefreshKey = null;
let paperHedgePreviewRunning = false;
let paperHedgeNoticeKey = null;

function money(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return Number(value).toFixed(2);
}

function price(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return Number(value).toFixed(4);
}

function temperature(value, unit) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return `${Number(value).toFixed(1)} ${unit || ""}`.trim();
}

function pct(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return `${(Number(value) * 100).toFixed(2)}%`;
}

function compactNumber(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return null;
  const rounded = Math.round(number * 10) / 10;
  return Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(1);
}

function bucketBoundary(value, direction) {
  const number = Number(value);
  if (!Number.isFinite(number)) return null;
  const adjusted = direction === "lower" ? number + 0.5 : number - 0.5;
  return Number.isInteger(adjusted) ? compactNumber(adjusted) : compactNumber(number);
}

function compactBucketKeyLabel(key) {
  const parts = String(key || "").split(":");
  if (parts.length !== 3) return null;
  const lower = parts[1] === "-inf" ? null : Number(parts[1]);
  const upper = parts[2] === "inf" ? null : Number(parts[2]);
  const lowerLabel = lower === null ? null : bucketBoundary(lower, "lower");
  const upperLabel = upper === null ? null : bucketBoundary(upper, "upper");
  if (lower === null && upperLabel) return `${upperLabel} or below`;
  if (upper === null && lowerLabel) return `${lowerLabel} or above`;
  if (lowerLabel && upperLabel) return lowerLabel === upperLabel ? lowerLabel : `${lowerLabel} to ${upperLabel}`;
  return null;
}

function compactBucketLabel(label, key) {
  const keyLabel = compactBucketKeyLabel(key);
  if (keyLabel) return keyLabel;
  const text = String(label || "").trim();
  if (!text) return "-";
  const rangeMatch = text.match(/(-?\d+(?:\.\d+)?)\s*(?:-|to|through|and|到|至)\s*(-?\d+(?:\.\d+)?)/i);
  if (rangeMatch) return `${compactNumber(rangeMatch[1])} to ${compactNumber(rangeMatch[2])}`;
  const temperatureMatch = text.match(/(-?\d+(?:\.\d+)?)\s*(?:°\s*)?[CF]\b/i);
  const numberLabel = temperatureMatch ? compactNumber(temperatureMatch[1]) : null;
  if (numberLabel && /\b(?:below|under|lower)\b|以下|低于|不高于/i.test(text)) return `${numberLabel} or below`;
  if (numberLabel && /\b(?:above|over|higher)\b|以上|高于|不低于/i.test(text)) return `${numberLabel} or above`;
  if (numberLabel) return numberLabel;
  return text.length > 18 ? `${text.slice(0, 18)}...` : text;
}

function numberValue(value) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function signedPct(value) {
  const number = numberValue(value);
  if (number === null) return "-";
  return `${number > 0 ? "+" : ""}${(number * 100).toFixed(2)}%`;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;",
  })[char]);
}

function todayLocalISO() {
  const now = new Date();
  const offsetMs = now.getTimezoneOffset() * 60 * 1000;
  return new Date(now.getTime() - offsetMs).toISOString().slice(0, 10);
}

function formatLocalTime(timezone) {
  const zone = String(timezone || "").trim();
  if (!zone || zone.toLowerCase() === "auto") return "-";
  try {
    const parts = new Intl.DateTimeFormat("zh-CN", {
      timeZone: zone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
      timeZoneName: "shortOffset",
    }).formatToParts(new Date());
    const value = (type) => parts.find((part) => part.type === type)?.value || "";
    const date = `${value("year")}-${value("month")}-${value("day")}`;
    const time = `${value("hour")}:${value("minute")}`;
    const offset = value("timeZoneName");
    return `${date} ${time}${offset ? ` ${offset}` : ""}`;
  } catch {
    return "-";
  }
}

function formatBeijingTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  try {
    const parts = new Intl.DateTimeFormat("zh-CN", {
      timeZone: "Asia/Shanghai",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hourCycle: "h23",
    }).formatToParts(date);
    const part = (type) => parts.find((item) => item.type === type)?.value || "";
    const milliseconds = String(date.getMilliseconds()).padStart(3, "0");
    return `${part("year")}-${part("month")}-${part("day")} ${part("hour")}:${part("minute")}:${part("second")}.${milliseconds} 北京时间`;
  } catch {
    return String(value);
  }
}

function sourceName(value) {
  const source = String(value || "");
  if (source === "polymarket") return "Polymarket";
  if (source === "csv") return "CSV";
  if (source === "positions") return "持仓推导";
  return "-";
}

function methodName(value) {
  const method = String(value || "");
  if (method === "events_keyset") return "Events";
  if (method === "markets") return "Markets";
  return "-";
}

function kindName(value) {
  return String(value || "") === "low" ? "最低温" : "最高温";
}

function actionName(value) {
  const action = String(value || "");
  if (action === "BUY_YES") return "买入";
  if (action === "WATCH") return "观察";
  if (action === "SKIP_NO_ASK") return "无 ask";
  if (action === "SKIP_NO_EDGE") return "不交易";
  return action || "-";
}

function actionTone(value) {
  const action = String(value || "");
  if (action === "BUY_YES") return "";
  if (action === "WATCH") return "warn";
  return "danger";
}

function scoreTone(value) {
  const score = numberValue(value);
  if (score === null) return "";
  if (score >= 75) return "good";
  if (score >= 55) return "watch";
  return "bad";
}

function reasonName(value) {
  const reason = String(value || "");
  const labels = {
    "missing executable ask": "缺少可执行 ask",
    "edge clears threshold, but spread is wide": "edge 达标，价差偏宽",
    "executable edge clears threshold": "可执行 edge 达标",
    "model is above market midpoint, but executable edge is below threshold": "模型高于市场中点，但可执行 edge 不足",
    "market price is above model fair probability": "市场价格高于模型 fair price",
    "edge below threshold": "edge 未达阈值",
  };
  return labels[reason] || reason || "-";
}

function modelName(value) {
  const model = String(value || "");
  if (model === "ecmwf_ifs025") return "ECMWF IFS 0.25";
  if (model === "ecmwf_ifs") return "ECMWF IFS HRES";
  if (model === "ecmwf_aifs025") return "ECMWF AIFS 0.25";
  if (model === "icon_seamless") return "ICON Seamless";
  if (model === "meteofrance_seamless") return "Meteo-France Seamless";
  if (model === "gfs_seamless") return "GFS Seamless";
  if (model === "ukmo_seamless") return "UKMO Seamless";
  if (model === "nws") return "NWS";
  return model || "-";
}

function metric(label, value, tone = "") {
  return `<div class="metric"><span>${escapeHtml(label)}</span><strong class="${escapeHtml(tone)}">${escapeHtml(value)}</strong></div>`;
}

function table(headers, rows) {
  const head = `<tr>${headers.map((item) => `<th>${escapeHtml(item)}</th>`).join("")}</tr>`;
  const body = rows.length
    ? rows.join("")
    : `<tr><td colspan="${headers.length}">暂无数据</td></tr>`;
  return `<table>${head}${body}</table>`;
}

function showNotice(message, isError = false) {
  const notice = $("notice");
  notice.hidden = !message;
  notice.textContent = message || "";
  notice.style.borderColor = isError ? "var(--notice-error-line)" : "var(--notice-warn-line)";
  notice.style.color = isError ? "var(--notice-error-text)" : "var(--notice-warn-text)";
  notice.style.background = isError ? "var(--notice-error-bg)" : "var(--notice-warn-bg)";
}

function syncLlmButtons() {
  const signalButton = $("explainSignalButton");
  if (signalButton) signalButton.disabled = !latestEnsembleSignalResult;
  const hasSignals = Boolean(latestEnsembleSignalResult?.signals?.length);
  const previewButton = $("paperPreviewButton");
  const buyButton = $("paperBuyButton");
  const hedgeButton = $("paperHedgePreviewButton");
  if (previewButton) previewButton.disabled = !hasSignals;
  if (buyButton) buyButton.disabled = !hasSignals;
  if (hedgeButton) hedgeButton.disabled = !hasSignals;
}

function showLlmSummary(summary) {
  const target = $("llmSummary");
  if (!target) return;
  target.textContent = summary || "";
}

function selectedCityOption() {
  return $("citySelect").selectedOptions[0];
}

function savedCityId() {
  try {
    return window.localStorage.getItem(SELECTED_CITY_STORAGE_KEY) || "";
  } catch {
    return "";
  }
}

function rememberCityId(cityId) {
  try {
    if (cityId) {
      window.localStorage.setItem(SELECTED_CITY_STORAGE_KEY, cityId);
    } else {
      window.localStorage.removeItem(SELECTED_CITY_STORAGE_KEY);
    }
  } catch {
    // localStorage can be unavailable in private or embedded contexts.
  }
}

function selectedCityRecord() {
  return cityRecords.find((city) => city.cityId === $("citySelect").value) || null;
}

function marketCityValue() {
  return selectedCityRecord()?.name || $("citySelect").value;
}

function syncUnitFromCity() {
  const city = selectedCityRecord();
  if (city) {
    $("unit").value = city.settlementUnit || "F";
    rememberCityId(city.cityId);
    return;
  }
  const unit = selectedCityOption()?.dataset.unit;
  if (unit) {
    $("unit").value = unit;
  }
}

function customLocationPayload() {
  const city = selectedCityRecord();
  return {
    useStoredCity: true,
    locationId: city?.cityId || $("citySelect").value,
    locationName: city?.name || "",
    latitude: city?.latitude ?? "",
    longitude: city?.longitude ?? "",
    timezone: city?.timezone || "auto",
    settlementUnit: city?.settlementUnit || $("unit").value,
    settlementStation: city?.settlementStation || "",
    stationId: city?.stationId || "",
    forecastGranularity: city?.forecastGranularity || "city",
    elevation: city?.elevation ?? "",
    cellSelection: city?.cellSelection || "",
  };
}

function marketPayload() {
  const city = selectedCityRecord();
  return {
    ...customLocationPayload(),
    marketSlug: $("marketSlug").value,
    conditionId: $("conditionId").value,
    city: marketCityValue(),
    marketQuery: $("marketQuery").value,
    targetDate: $("targetDate").value,
    temperatureKind: $("temperatureKind").value,
    includeOrderbooks: $("includeOrderbooks").checked,
    unit: city?.settlementUnit || $("unit").value,
  };
}

function ensemblePayload(includeMarketBuckets = false) {
  const city = selectedCityRecord();
  return {
    city: $("citySelect").value,
    ...customLocationPayload(),
    unit: city?.settlementUnit || $("unit").value,
    targetDate: $("targetDate").value,
    temperatureKind: $("temperatureKind").value,
    model: $("ensembleModel").value,
    marketSlug: $("marketSlug").value,
    conditionId: $("conditionId").value,
    marketQuery: $("marketQuery").value,
    includeOrderbooks: $("includeOrderbooks").checked,
    includeMarketBuckets,
    feeRate: Number($("feeRate").value),
    minEdge: Number($("minEdge").value),
    saveSqlite: $("saveSqlite").checked,
  };
}

function settlementPayload() {
  const city = selectedCityRecord();
  return {
    city: $("citySelect").value,
    ...customLocationPayload(),
    unit: city?.settlementUnit || $("unit").value,
    targetDate: $("targetDate").value,
    temperatureKind: $("temperatureKind").value,
    marketSlug: $("marketSlug").value,
    conditionId: $("conditionId").value,
    marketQuery: $("marketQuery").value,
    includeOrderbooks: $("includeOrderbooks").checked,
    includeMarketBuckets: true,
  };
}

function bestPaperSignal() {
  const signals = latestEnsembleSignalResult?.signals || [];
  const buySignals = signals
    .map(signalWithDerived)
    .filter((row) => row.recommendation === "BUY_YES")
    .sort((left, right) => (right._edge ?? -99) - (left._edge ?? -99));
  return buySignals[0] || null;
}

function paperPayload(signal = bestPaperSignal(), { includeDefaultStake = false } = {}) {
  const city = selectedCityRecord();
  const latest = latestEnsembleSignalResult || {};
  const targetProfitInput = $("paperTargetProfit") || $("targetProfit");
  const minCashoutInput = $("paperMinCashoutRatio") || $("minCashoutRatio");
  const payload = {
    city: $("citySelect").value,
    ...customLocationPayload(),
    unit: city?.settlementUnit || $("unit").value,
    targetDate: $("targetDate").value,
    temperatureKind: $("temperatureKind").value,
    marketSlug: $("marketSlug").value,
    conditionId: $("conditionId").value,
    marketQuery: $("marketQuery").value,
    includeOrderbooks: $("includeOrderbooks").checked,
    initialCash: Number($("paperInitialCash").value),
    minEdge: Number($("minEdge").value),
    feeRate: Number($("feeRate").value),
    maxSpread: Number($("paperMaxSpread").value),
    minAskDepthShares: Number($("paperMinAskDepth").value),
    maxMarketExposure: Number($("paperMaxMarketExposure").value),
    maxCityDateExposure: Number($("paperMaxCityDateExposure").value),
    targetProfit: Number(targetProfitInput?.value || 0.10),
    minCashoutRatio: Number(minCashoutInput?.value || 0.50),
    tailProbabilityCutoff: Number($("tailProbabilityCutoff").value),
    maxTailProbability: Number($("maxTailProbability").value),
    summary: latest.summary || {},
    signals: latest.signals || [],
    signal,
    marketBuckets: latest.marketBuckets || [],
  };
  const stakeText = $("paperStake").value.trim();
  if (stakeText || includeDefaultStake) {
    payload.stakeUsdc = Number(stakeText || 25);
  }
  return payload;
}

function paperMarkPayload() {
  const targetProfitInput = $("paperTargetProfit") || $("targetProfit");
  const minCashoutInput = $("paperMinCashoutRatio") || $("minCashoutRatio");
  return {
    initialCash: Number($("paperInitialCash").value || 1000),
    feeRate: Number($("feeRate").value || 0.05),
    targetProfit: Number(targetProfitInput?.value || 0.10),
    minCashoutRatio: Number(minCashoutInput?.value || 0.50),
    markCacheSeconds: 5,
    limit: 20,
  };
}

function rejectReasonName(value) {
  const reason = String(value || "");
  const labels = {
    NO_BUY_SIGNAL: "非 BUY_YES",
    EDGE_TOO_LOW: "Edge 不足",
    NO_ASK: "无 ask",
    INSUFFICIENT_BALANCE: "余额不足",
    SPREAD_TOO_WIDE: "Spread 过宽",
    INSUFFICIENT_DEPTH: "Ask 深度不足",
    EXPOSURE_LIMIT: "暴露超限",
    MISSING_SETTLEMENT_SOURCE: "缺少结算站配置",
    NO_BID: "无 bid",
  };
  return labels[reason] || reason || "-";
}

function settlementImportIssueText(result) {
  const imports = result?.settlementImports || {};
  const errors = Array.isArray(imports.errors) ? imports.errors : [];
  const skipped = Array.isArray(imports.skipped) ? imports.skipped : [];
  const parts = [];
  if (errors.length) {
    parts.push(
      `失败：${errors
        .map((item) => `${item.cityId || "-"} ${item.targetDate || "-"} ${item.kind || "-"} ${item.error || "-"}`)
        .join("；")}`,
    );
  }
  if (skipped.length) {
    parts.push(
      `跳过：${skipped
        .map((item) => `${item.cityId || "-"} ${item.targetDate || "-"} ${item.kind || "-"} ${item.reason || "-"}`)
        .join("；")}`,
    );
  }
  return parts.length ? `（${parts.join("；")}）` : "";
}

function paperStatusTone(value) {
  const status = String(value || "");
  if (status === "FILLED" || status === "OPEN" || status === "ACCEPTED") return "";
  if (status === "REJECTED" || status === "SETTLED") return status === "REJECTED" ? "danger" : "warn";
  return "warn";
}

function renderPaperPreview(preview) {
  const target = $("paperPreview");
  if (!target) return;
  if (!preview) {
    target.innerHTML = "<h2>买入 Preview</h2>";
    return;
  }
  const metrics = [
    metric("状态", preview.accepted ? "可虚拟买入" : "拒绝", preview.accepted ? "good" : "bad"),
    metric("拒绝原因", rejectReasonName(preview.rejectReason)),
    metric("温度桶", compactBucketLabel(preview.bucketLabel || preview.outcome, preview.bucketKey)),
    metric("模型概率", pct(preview.ensembleProbability)),
    metric("入场 VWAP", price(preview.vwap ?? preview.executableEntryCost)),
    metric("Fee", money(preview.fee)),
    metric("Shares", money(preview.filledShares)),
    metric("定仓", preview.sizingMethod === "kelly" ? "Kelly" : "手动"),
    metric("Kelly stake", money(preview.kellySizing?.stake)),
    metric("Edge", signedPct(preview.edge)),
    metric("Net cost", money(preview.netCost)),
    metric("Spread", price(preview.spread)),
    metric("Ask 深度", money(preview.askDepth)),
    metric("虚拟单", preview.noRealOrder ? "是" : "否"),
  ].join("");
  target.innerHTML = `<h2>买入 Preview</h2><div class="metrics paper-metrics">${metrics}</div>`;
}

function renderPaperHedgePreview(result) {
  const target = $("paperHedgePreview");
  if (!target) return;
  if (!result) {
    target.innerHTML = "<h2>Hedge Preview</h2>";
    return;
  }
  const summary = result.summary || {};
  const adjacent = result.adjacent || {};
  const tail = result.tailRiskLock || {};
  const hedgeFeasible = tail.feasible ?? summary.hedgeFeasible ?? true;
  const hedgeReason = Array.isArray(tail.notes) ? tail.notes.join("；") : "";
  const metrics = [
    metric("相邻桶建议", adjacent.recommendation || "-"),
    metric("主桶", adjacent.mainOutcome || "-"),
    metric("邻桶", adjacent.adjacentOutcome || "-"),
    metric("邻桶概率", pct(adjacent.adjacentProbability)),
    metric("相邻桶成本", money(adjacent.hedgeCost)),
    metric("Worst 改善", money(adjacent.riskReduction)),
    metric("Covered probability", pct(summary.coveredProbability)),
    metric("Tail risk", pct(summary.uncoveredTailProbability)),
    metric("Covered worst PnL", money(summary.coveredWorstCasePnl)),
    metric("Global worst PnL", money(summary.globalWorstCasePnl)),
    metric("Tail hedge cost", money(tail.hedgeCost)),
    metric("Hedge 状态", hedgeFeasible ? "可计算" : "不可行", hedgeFeasible ? "" : "bad"),
    metric("Tail lock", summary.isTailRiskLock ? "是" : "否", summary.isTailRiskLock ? "good" : "watch"),
    metric("真套利", summary.isTrueArbitrage ? "是" : "否"),
  ].join("");
  const adjacentRows = adjacent.hedgeShares ? [`<tr>
    <td>相邻桶</td>
    <td>${escapeHtml(adjacent.adjacentOutcome || "-")}</td>
    <td>${money(adjacent.hedgeShares)}</td>
    <td>${price(adjacent.vwap)}</td>
    <td>${money(adjacent.hedgeCost)}</td>
  </tr>`] : [];
  const tailRows = (tail.hedgeLegs || []).map((row) => `<tr>
    <td>Tail lock</td>
    <td>${escapeHtml(row.outcome)}</td>
    <td>${money(row.shares)}</td>
    <td>${price(row.price)}</td>
    <td>${money(row.totalCost ?? row.cost)}</td>
  </tr>`);
  const hedgeRows = [...adjacentRows, ...tailRows];
  target.innerHTML = `
    <h2>Hedge Preview</h2>
    <div class="metrics paper-metrics">${metrics}</div>
    ${hedgeReason ? `<p>${escapeHtml(hedgeReason)}</p>` : ""}
    ${table(["类型", "温度桶", "份额", "价格", "成本"], hedgeRows)}
  `;
}

function renderPaperPortfolio(result) {
  const portfolio = result?.portfolio || result || {};
  latestPaperPortfolioResult = portfolio;
  const summary = portfolio.summary || {};
  const summaryMetrics = [
    metric("现金", money(summary.cash)),
    metric("持仓成本", money(summary.openPositionCost)),
    metric("Mark value", money(summary.markValue)),
    metric("可兑现值", money(summary.liquidationValue)),
    metric("Realized PnL", money(summary.realizedPnl), Number(summary.realizedPnl) >= 0 ? "good" : "bad"),
    metric("Unrealized PnL", money(summary.unrealizedPnl), Number(summary.unrealizedPnl) >= 0 ? "good" : "bad"),
    metric("Total equity", money(summary.totalEquity)),
    metric("OPEN 持仓", String(summary.openPositionCount ?? 0)),
  ].join("");
  $("paperSummary").innerHTML = `<div class="metrics paper-metrics">${summaryMetrics}</div>`;
  const orderRows = (portfolio.orders || []).map((row) => `<tr>
    <td>${escapeHtml(formatBeijingTime(row.createdAt))}</td>
    <td>${escapeHtml(row.targetDate || "-")}</td>
    <td>${escapeHtml(compactBucketLabel(row.bucketLabel || row.outcome, row.bucketKey))}</td>
    <td>${money(row.stakeUsdc)}</td>
    <td>${money(row.filledShares)}</td>
    <td>${price(row.vwap)}</td>
    <td>${signedPct(row.edge)}</td>
    <td><span class="badge ${paperStatusTone(row.status)}">${escapeHtml(row.status || "-")}</span></td>
    <td>${escapeHtml(rejectReasonName(row.rejectReason))}</td>
  </tr>`);
  const positions = [...(portfolio.positions || [])].sort((left, right) => (
    String(right.targetDate || "").localeCompare(String(left.targetDate || ""))
    || String(right.updatedAt || "").localeCompare(String(left.updatedAt || ""))
  ));
  const positionRows = positions.map((row) => `<tr>
    <td>${escapeHtml(row.targetDate || "-")}</td>
    <td>${escapeHtml(compactBucketLabel(row.bucketLabel || row.outcome, row.bucketKey))}</td>
    <td>${money(row.openShares)}</td>
    <td>${money(row.totalCost)}</td>
    <td>${price(row.averageEntryPrice)}</td>
    <td>${price(row.bestBid)}</td>
    <td>${price(row.bestAsk)}</td>
    <td>${money(row.markValue)}</td>
    <td>${money(row.liquidationValue)}</td>
    <td>${money(row.realizedPnl)}</td>
    <td>${money(row.unrealizedPnl)}</td>
    <td>${escapeHtml(row.latestMark?.exitSignal || "-")}</td>
    <td><span class="badge ${paperStatusTone(row.status)}">${escapeHtml(row.status || "-")}</span></td>
  </tr>`);
  $("paperOrders").innerHTML = `<h2>最近虚拟订单</h2>${table(["下单时间", "盘口日期", "温度桶", "Stake", "Shares", "VWAP", "Edge", "状态", "拒绝原因"], orderRows)}`;
  $("paperPositions").innerHTML = `<h2>虚拟持仓</h2>${table(["盘口日期", "温度桶", "Shares", "成本", "均价", "Bid", "Ask", "Mark", "可兑现", "Realized", "Unrealized", "退出信号", "状态"], positionRows)}`;
  renderPaperMarks(portfolio.marks || []);
}

function renderPaperMarks(marks) {
  const target = $("paperMarks");
  if (!target) return;
  const rows = (marks || []).map((row) => `<tr>
    <td>${escapeHtml(formatBeijingTime(row.fetchedAt))}</td>
    <td>${escapeHtml(compactBucketLabel(row.bucketLabel || row.outcome, row.bucketKey))}</td>
    <td>${price(row.bestBid)}</td>
    <td>${price(row.bestAsk)}</td>
    <td>${price(row.spread)}</td>
    <td>${money(row.bidDepth)}</td>
    <td>${money(row.markValue)}</td>
    <td>${money(row.liquidationValue)}</td>
    <td>${money(row.executablePnl)}</td>
    <td>${escapeHtml(row.exitSignal || "-")}</td>
    <td>${escapeHtml(rejectReasonName(row.warning))}</td>
  </tr>`);
  target.innerHTML = `<h2>持仓盘口轮询</h2>${table(["时间", "温度桶", "Bid", "Ask", "Spread", "Bid 深度", "Mark", "可兑现", "Exec PnL", "退出信号", "警告"], rows)}`;
}

function renderPaperMonitorStatus(result) {
  const target = $("paperMonitorStatus");
  if (!target) return;
  const monitor = result?.monitor || result || {};
  latestPaperMonitorResult = monitor;
  const lastSummary = monitor.lastResult?.summary || {};
  const metrics = [
    metric("轮询", monitor.enabled ? "运行中" : "已停止", monitor.enabled ? "good" : "watch"),
    metric("执行中", monitor.running ? "是" : "否", monitor.running ? "watch" : ""),
    metric("间隔", `${monitor.intervalMs || "-"} ms`),
    metric("Tick", String(monitor.tickCount ?? 0)),
    metric("上次 mark", formatBeijingTime(monitor.lastFinishedAt)),
    metric("上次持仓", String(lastSummary.openPositionCount ?? "-")),
    metric("上次快照", String(lastSummary.markCount ?? "-")),
    metric("跳过", String(monitor.skippedCount ?? 0)),
    metric("连续错误", String(monitor.consecutiveErrors ?? 0), Number(monitor.consecutiveErrors) > 0 ? "bad" : ""),
  ].join("");
  const errorHtml = monitor.lastError ? `<p class="notice error">最近错误：${escapeHtml(monitor.lastError)}</p>` : "";
  target.innerHTML = `<div class="metrics paper-metrics paper-monitor-metrics">${metrics}</div>${errorHtml}`;
}

function paperMonitorStatusIntervalMs(monitor) {
  const intervalMs = Number(monitor?.intervalMs || $("paperMonitorIntervalMs")?.value || PAPER_MONITOR_INTERVAL_MS);
  const bounded = Number.isFinite(intervalMs) ? intervalMs : PAPER_MONITOR_INTERVAL_MS;
  return Math.max(PAPER_MONITOR_STATUS_MIN_MS, Math.min(PAPER_MONITOR_STATUS_MAX_MS, bounded));
}

function paperMonitorRefreshKey(monitor) {
  if (!monitor?.lastFinishedAt) return null;
  return `${monitor.tickCount ?? ""}:${monitor.lastFinishedAt}`;
}

async function maybeRefreshPaperPortfolioFromMonitor(monitor) {
  const refreshKey = paperMonitorRefreshKey(monitor);
  if (!refreshKey || refreshKey === paperMonitorPortfolioRefreshKey) return;
  paperMonitorPortfolioRefreshKey = refreshKey;
  await loadPaperPortfolio();
  await maybeRefreshPaperHedgeFromMonitor(monitor);
}

function paperHedgeNotice(result) {
  const adjacent = result?.adjacent || {};
  const tail = result?.tailRiskLock || {};
  if (adjacent.recommendation === "HEDGE_ADJACENT") {
    return {
      key: [
        "adjacent",
        adjacent.mainOutcome || "",
        adjacent.adjacentOutcome || "",
        Number(adjacent.hedgeShares || 0).toFixed(2),
        Number(adjacent.hedgeCost || 0).toFixed(2),
      ].join(":"),
      message: `轮询发现相邻桶对冲建议：${compactBucketLabel(adjacent.adjacentOutcome)}，成本 ${money(adjacent.hedgeCost)}，Worst-case 改善 ${money(adjacent.riskReduction)}。`,
    };
  }
  if (tail.isTailRiskLock && (tail.hedgeLegs || []).length) {
    const firstLeg = tail.hedgeLegs[0] || {};
    return {
      key: [
        "tail",
        firstLeg.outcome || "",
        Number(tail.hedgeCost || 0).toFixed(2),
        Number(tail.coveredWorstCasePnl || 0).toFixed(2),
      ].join(":"),
      message: `轮询发现 Tail-risk lock 方案：覆盖概率 ${pct(tail.coveredProbability)}，成本 ${money(tail.hedgeCost)}，核心最差 PnL ${money(tail.coveredWorstCasePnl)}。`,
    };
  }
  return null;
}

async function fetchPaperHedgePreview({ notify = false, refreshKey = null } = {}) {
  const signal = bestPaperSignal();
  if (!signal || paperHedgePreviewRunning) return null;
  paperHedgePreviewRunning = true;
  try {
    const response = await fetch(`${API_BASE}/api/paper/hedge-preview`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(paperPayload(signal, { includeDefaultStake: true })),
    });
    const result = await readJsonResponse(response, "Hedge preview 失败");
    renderPaperHedgePreview(result);
    if (refreshKey) {
      paperMonitorHedgeRefreshKey = refreshKey;
    }
    if (notify) {
      const notice = paperHedgeNotice(result);
      if (notice && notice.key !== paperHedgeNoticeKey) {
        paperHedgeNoticeKey = notice.key;
        showNotice(notice.message);
      }
    }
    return result;
  } finally {
    paperHedgePreviewRunning = false;
    syncLlmButtons();
  }
}

async function maybeRefreshPaperHedgeFromMonitor(monitor) {
  const refreshKey = paperMonitorRefreshKey(monitor);
  const openPositionCount = Number(monitor?.lastResult?.summary?.openPositionCount ?? latestPaperPortfolioResult?.summary?.openPositionCount ?? 0);
  if (
    !refreshKey
    || refreshKey === paperMonitorHedgeRefreshKey
    || openPositionCount <= 0
    || !latestEnsembleSignalResult?.signals?.length
  ) {
    return;
  }
  try {
    await fetchPaperHedgePreview({ notify: true, refreshKey });
  } catch {
    paperMonitorHedgeRefreshKey = refreshKey;
  }
}

function stopPaperMonitorStatusPolling() {
  if (paperMonitorStatusTimer) {
    clearInterval(paperMonitorStatusTimer);
    paperMonitorStatusTimer = null;
  }
  paperMonitorStatusTimerMs = null;
}

function syncPaperMonitorStatusPolling(monitor = latestPaperMonitorResult) {
  if (!monitor?.enabled) {
    stopPaperMonitorStatusPolling();
    return;
  }
  const intervalMs = paperMonitorStatusIntervalMs(monitor);
  if (paperMonitorStatusTimer && paperMonitorStatusTimerMs === intervalMs) return;
  stopPaperMonitorStatusPolling();
  paperMonitorStatusTimerMs = intervalMs;
  paperMonitorStatusTimer = setInterval(() => {
    void loadPaperMonitorStatus({ refreshPortfolio: true });
  }, intervalMs);
}

function levelText(levels) {
  if (!levels || !levels.length) return "-";
  return levels
    .slice(0, 3)
    .map((level) => `${price(level.price)} x ${money(level.size)}`)
    .join(" / ");
}

function probabilityChart(chart) {
  const rawLabels = chart.bucketLabels || [];
  const bucketKeys = chart.bucketKeys || [];
  const labels = rawLabels.map((label, index) => compactBucketLabel(label, bucketKeys[index]));
  const probabilities = chart.bucketProbabilities || [];
  const market = chart.marketImpliedProbabilities || [];
  const members = chart.memberValues || [];
  const width = Math.max(1120, labels.length * 112 + 80);
  const height = 300;
  const left = 44;
  const right = 24;
  const top = 24;
  const bottom = 76;
  const plotWidth = width - left - right;
  const plotHeight = height - top - bottom;
  const maxProb = Math.max(0.01, ...probabilities, ...market.filter((value) => value !== null));
  const slot = plotWidth / Math.max(1, labels.length);
  const barWidth = Math.max(12, Math.min(42, slot * 0.55));
  const bars = labels.map((label, index) => {
    const probability = probabilities[index] || 0;
    const x = left + index * slot + (slot - barWidth) / 2;
    const y = top + plotHeight - (probability / maxProb) * plotHeight;
    const h = (probability / maxProb) * plotHeight;
    const marketProbability = market[index];
    const marketY = marketProbability === null || marketProbability === undefined
      ? null
      : top + plotHeight - (marketProbability / maxProb) * plotHeight;
    const title = String(rawLabels[index] || "") !== label
      ? `<title>${escapeHtml(rawLabels[index] || "")}</title>`
      : "";
    return `
      <rect class="chart-bar" x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barWidth.toFixed(1)}" height="${h.toFixed(1)}" rx="3"></rect>
      ${marketY === null ? "" : `<circle class="chart-market-dot" cx="${(x + barWidth / 2).toFixed(1)}" cy="${marketY.toFixed(1)}" r="4"></circle>`}
      <text class="chart-label" x="${(x + barWidth / 2).toFixed(1)}" y="${height - 42}" text-anchor="middle">${title}${escapeHtml(label)}</text>
      <text class="chart-label" x="${(x + barWidth / 2).toFixed(1)}" y="${Math.max(14, y - 6).toFixed(1)}" text-anchor="middle">${pct(probability)}</text>
    `;
  }).join("");
  const keyToIndex = new Map((chart.bucketKeys || []).map((key, index) => [key, index]));
  const rug = members.map((member, index) => {
    const bucketIndex = keyToIndex.get(member.bucketKey);
    if (bucketIndex === undefined) return "";
    const jitter = ((index % 7) - 3) * 3;
    const x = left + bucketIndex * slot + slot / 2 + jitter;
    return `<line class="chart-member-rug" x1="${x.toFixed(1)}" x2="${x.toFixed(1)}" y1="${height - 34}" y2="${height - 24}" stroke-width="1.5"></line>`;
  }).join("");
  return `
    <div class="chart-wrap">
      <svg class="prob-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="Ensemble probability chart">
        <line class="chart-axis" x1="${left}" y1="${top + plotHeight}" x2="${width - right}" y2="${top + plotHeight}"></line>
        <line class="chart-axis" x1="${left}" y1="${top}" x2="${left}" y2="${top + plotHeight}"></line>
        ${bars}
        ${rug}
      </svg>
    </div>
  `;
}

function renderEnsemble(result) {
  const summary = result.summary;
  const metrics = [
    metric("地点", summary.cityName || summary.cityId || "-"),
    metric("坐标", `${price(summary.latitude)}, ${price(summary.longitude)}`),
    metric("成员数", summary.memberCount ?? "-"),
    metric("未命中", summary.unmatchedCount ?? "-"),
    metric("均值", temperature(summary.empiricalMean, summary.unit)),
    metric("Std", temperature(summary.empiricalStd, summary.unit)),
    metric("P10", temperature(summary.p10, summary.unit)),
    metric("P50", temperature(summary.p50, summary.unit)),
    metric("P90", temperature(summary.p90, summary.unit)),
    metric("保存", summary.saved ? "是" : "否"),
  ].join("");
  const probabilityRows = result.probabilities.map((row) => `<tr>
    <td title="${escapeHtml(row.bucketLabel)}">${escapeHtml(compactBucketLabel(row.bucketLabel, row.bucketKey))}</td>
    <td>${money(row.hitCount)}</td>
    <td>${money(row.totalMembers)}</td>
    <td>${pct(row.probability)}</td>
  </tr>`);
  $("ensembleProbability").innerHTML = `
    <div class="metrics">${metrics}</div>
    ${probabilityChart(result.chart || {})}
    ${table(["温度桶", "命中", "成员", "概率"], probabilityRows)}
  `;
  renderSignalPanel(result.signals || [], summary);
  showNotice(`已生成 ${summary.model} ensemble 经验分布。`);
}

function signalWithDerived(row) {
  const edge = numberValue(row.edge);
  const ensembleProbability = numberValue(row.ensembleProbability);
  const marketImplied = numberValue(row.marketImpliedProbability ?? row.marketMidpoint);
  const bestBid = numberValue(row.bestBid);
  const bestAsk = numberValue(row.bestAsk);
  const spread = numberValue(row.spread) ?? (
    bestBid !== null && bestAsk !== null ? Math.max(0, bestAsk - bestBid) : null
  );
  const rawEdge = numberValue(row.rawEdge) ?? (
    ensembleProbability !== null && marketImplied !== null
      ? ensembleProbability - marketImplied
      : null
  );
  const score = numberValue(row.signalScore) ?? Math.round(Math.max(
    0,
    Math.min(
      100,
      50
        + (edge ?? 0) * 600
        + (rawEdge ?? 0) * 180
        - (spread ?? 0) * 200,
    ),
  ));
  return {
    ...row,
    _edge: edge,
    _marketImplied: marketImplied,
    _rawEdge: rawEdge,
    _score: score,
    _spread: spread,
  };
}

function renderSignalPanel(signals, summary) {
  const target = $("signalScore");
  if (!target) return;
  const rows = signals
    .map(signalWithDerived)
    .sort((left, right) => (right._score - left._score) || ((right._edge ?? -1) - (left._edge ?? -1)));
  const top = rows[0] || null;
  const buyCount = rows.filter((row) => row.recommendation === "BUY_YES").length;
  const watchCount = rows.filter((row) => row.recommendation === "WATCH").length;
  const metrics = [
    metric("最佳桶", top?.outcome || "-"),
    metric("当地时间", formatLocalTime(summary.timezone)),
    metric("评分", top ? String(top._score) : "-", top ? scoreTone(top._score) : ""),
    metric("可执行 Edge", top ? signedPct(top._edge) : "-"),
    metric("动作", top ? actionName(top.recommendation) : "-"),
    metric("买入候选", buyCount),
    metric("观察候选", watchCount),
    metric("市场桶数", summary.marketBucketCount ?? rows.length),
    metric("最小 Edge", pct($("minEdge")?.value || 0.03)),
  ].join("");
  const tableRows = rows.map((row) => `<tr>
    <td><span class="score-pill ${scoreTone(row._score)}">${escapeHtml(row._score)}</span></td>
    <td>${escapeHtml(row.outcome)}</td>
    <td>${pct(row.ensembleProbability)}</td>
    <td>${pct(row._marketImplied)}</td>
    <td>${signedPct(row._rawEdge)}</td>
    <td>${price(row.executableEntryCost)}</td>
    <td>${price((numberValue(row.fee) ?? 0) + (numberValue(row.expectedExitCost) ?? 0))}</td>
    <td>${signedPct(row._edge)}</td>
    <td>${price(row.bestBid)} / ${price(row.bestAsk)}</td>
    <td>${price(row._spread)}</td>
    <td>${money(row.askDepth)}</td>
    <td><span class="badge ${actionTone(row.recommendation)}">${escapeHtml(actionName(row.recommendation))}</span></td>
    <td>${escapeHtml(reasonName(row.reason))}</td>
  </tr>`);
  target.innerHTML = `
    <div class="metrics">${metrics}</div>
    <div class="signal-table">${table(
      ["评分", "温度桶", "模型概率", "市场隐含", "Raw edge", "入场", "成本", "Edge", "Bid / Ask", "Spread", "Ask 深度", "动作", "原因"],
      tableRows,
    )}</div>
  `;
}

function statusName(value) {
  const status = String(value || "");
  if (status === "success" || status === "settled") return "成功";
  if (status === "failed") return "失败";
  return status || "-";
}

function statusTone(value) {
  const status = String(value || "");
  if (status === "failed") return "danger";
  if (status === "success" || status === "settled") return "";
  return "warn";
}

function renderCalibration(calibrationResult) {
  const summary = calibrationResult.summary || {};
  const metrics = [
    metric("已复盘信号", summary.outcomeCount ?? 0),
    metric("命中率", pct(summary.hitRate)),
    metric("平均 Brier", price(summary.averageBrierScore)),
    metric("平均误差", signedPct(summary.averageProbabilityError)),
    metric("买入信号", summary.buySignalCount ?? 0),
    metric("买入命中率", pct(summary.buySignalHitRate)),
  ].join("");
  const binRows = (calibrationResult.probabilityBins || [])
    .filter((row) => Number(row.count) > 0)
    .map((row) => `<tr>
      <td>${escapeHtml(row.label)}</td>
      <td>${money(row.count)}</td>
      <td>${pct(row.averageProbability)}</td>
      <td>${pct(row.observedRate)}</td>
      <td>${price(row.averageBrierScore)}</td>
    </tr>`);
  $("calibrationSummary").innerHTML = `
    <div class="metrics">${metrics}</div>
    ${table(["概率桶", "数量", "平均预测", "实际命中", "Brier"], binRows)}
  `;
}

function renderHistory(
  runsResult,
  probabilitiesResult,
  settlementsResult = {},
  outcomesResult = {},
  calibrationResult = {},
) {
  renderCalibration(calibrationResult);
  const settlementRows = (settlementsResult.settlements || []).map((row) => `<tr>
    <td>${escapeHtml(row.city_id)}</td>
    <td>${escapeHtml(row.target_date)}</td>
    <td>${escapeHtml(kindName(row.kind))}</td>
    <td>${temperature(row.observed_value, row.unit)}</td>
    <td>${escapeHtml(row.bucket_label || "-")}</td>
    <td><span class="badge ${statusTone(row.status)}">${escapeHtml(statusName(row.status))}</span></td>
  </tr>`);
  const outcomeRows = (outcomesResult.outcomes || []).map((row) => `<tr>
    <td>${escapeHtml(row.city_id)}</td>
    <td>${escapeHtml(row.target_date)}</td>
    <td>${escapeHtml(row.outcome)}</td>
    <td>${pct(row.ensemble_probability)}</td>
    <td>${signedPct(row.edge)}</td>
    <td><span class="badge ${row.won ? "" : "danger"}">${row.won ? "命中" : "未中"}</span></td>
    <td>${price(row.brier_score)}</td>
  </tr>`);
  const runRows = (runsResult.runs || []).map((row) => `<tr>
    <td>${escapeHtml(row.city_id)}</td>
    <td>${escapeHtml(row.model)}</td>
    <td>${escapeHtml(row.target_date)}</td>
    <td>${escapeHtml(row.kind)}</td>
    <td>${money(row.member_count)}</td>
  </tr>`);
  const probabilityRows = (probabilitiesResult.probabilities || []).map((row) => `<tr>
    <td>${escapeHtml(row.bucket_label)}</td>
    <td>${pct(row.probability)}</td>
    <td>${money(row.hit_count)} / ${money(row.total_members)}</td>
  </tr>`);
  $("recentSettlements").innerHTML = `<h2>最近结算</h2>${table(["城市", "日期", "类型", "实际温度", "命中桶", "状态"], settlementRows)}`;
  $("recentOutcomes").innerHTML = `<h2>最近 Outcomes</h2>${table(["城市", "日期", "温度桶", "预测概率", "Edge", "结果", "Brier"], outcomeRows)}`;
  $("recentRuns").innerHTML = `<h2>最近 Runs</h2>${table(["城市", "模型", "日期", "类型", "成员"], runRows)}`;
  $("recentProbabilities").innerHTML = `<h2>最近 Probabilities</h2>${table(["温度桶", "概率", "命中"], probabilityRows)}`;
}

function renderMarkets(result) {
  const summary = result.summary;
  const timezone = summary.timezone || selectedCityRecord()?.timezone;
  const metricHtml = [
    metric("盘口来源", sourceName(summary.marketSource)),
    metric("发现方式", methodName(summary.selector?.method)),
    metric("类型", summary.selector?.kind === "low" ? "最低温" : "最高温"),
    metric("日期", summary.selector?.targetDate || "-"),
    metric("当地时间", formatLocalTime(timezone)),
    metric("市场桶数", summary.marketCount ?? "-"),
    metric("Bid sum", price(summary.bidSum)),
    metric("Ask sum", price(summary.askSum)),
    metric("Mid sum", price(summary.midpointSum)),
    metric("Overround", summary.isOverround ? "是" : "否"),
  ].join("");
  const rows = result.buckets.map((row) => `<tr>
    <td>${escapeHtml(row.outcome)}</td>
    <td>${price(row.markPrice)}</td>
    <td>${price(row.bestBid)}</td>
    <td>${price(row.bestAsk)}</td>
    <td>${price(row.spread)}</td>
    <td>${escapeHtml(levelText(row.orderbook?.bids))}</td>
    <td>${escapeHtml(levelText(row.orderbook?.asks))}</td>
    <td>${escapeHtml(row.tokenId || "-")}</td>
  </tr>`);
  $("markets").innerHTML = `<div class="metrics">${metricHtml}</div>${table(
    ["温度桶", "mark", "best bid", "best ask", "spread", "bid depth", "ask depth", "token"],
    rows,
  )}`;
  showNotice(`已从 Polymarket 接口获取 ${summary.marketCount} 个天气盘口。`);
}

async function readJsonResponse(response, fallbackMessage) {
  const text = await response.text();
  let result = {};
  if (text) {
    try {
      result = JSON.parse(text);
    } catch {
      result = { error: text };
    }
  }
  if (!response.ok || result.error) {
    throw new Error(result.error || fallbackMessage);
  }
  return result;
}

async function fetchMarkets() {
  const button = $("fetchMarketButton");
  button.disabled = true;
  showNotice("正在从 Polymarket 获取盘口...");
  try {
    const response = await fetch(`${API_BASE}/api/markets`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(marketPayload()),
    });
    const result = await readJsonResponse(response, "获取盘口失败");
    renderMarkets(result);
  } catch (error) {
    showNotice(error instanceof Error ? error.message : String(error), true);
  } finally {
    button.disabled = false;
  }
}

async function loadCities(selectedCityId = savedCityId() || $("citySelect").value) {
  try {
    const response = await fetch(`${API_BASE}/api/cities`);
    const result = await readJsonResponse(response, "读取城市失败");
    cityRecords = result.cities || [];
    const selected = cityRecords.some((city) => city.cityId === selectedCityId)
      ? selectedCityId
      : cityRecords[0]?.cityId;
    $("citySelect").innerHTML = cityRecords.map((city) => (
      `<option value="${escapeHtml(city.cityId)}" data-unit="${escapeHtml(city.settlementUnit)}">${escapeHtml(city.name)}</option>`
    )).join("");
    const competitionCities = $("competitionCities");
    if (competitionCities) {
      competitionCities.innerHTML = cityRecords.map((city) => (
        `<option value="${escapeHtml(city.cityId)}">${escapeHtml(city.name)}</option>`
      )).join("");
      const current = selected || cityRecords[0]?.cityId;
      for (const option of competitionCities.options) option.selected = option.value === current;
    }
    if (selected) {
      $("citySelect").value = selected;
      rememberCityId(selected);
      syncUnitFromCity();
    }
  } catch (error) {
    showNotice(error instanceof Error ? error.message : String(error), true);
  }
}

function selectedValues(id) {
  const select = $(id);
  return select ? Array.from(select.selectedOptions).map((option) => option.value) : [];
}

function modelCompetitionPayload() {
  const city = selectedCityRecord();
  const payload = {
    cityIds: selectedValues("competitionCities"),
    models: selectedValues("competitionModels"),
    targetDate: $("targetDate").value,
    temperatureKind: $("temperatureKind").value,
    unit: city?.settlementUnit || $("unit").value,
    marketSlug: $("marketSlug").value,
    conditionId: $("conditionId").value,
    marketQuery: $("marketQuery").value,
    includeOrderbooks: $("includeOrderbooks").checked,
    initialCash: Number($("paperInitialCash").value || 1000),
    minEdge: Number($("minEdge").value || 0.03),
    feeRate: Number($("feeRate").value || 0.05),
    maxSpread: Number($("paperMaxSpread").value || 0.12),
    minAskDepthShares: Number($("paperMinAskDepth").value || 1),
    maxMarketExposure: Number($("paperMaxMarketExposure").value || 100),
    maxCityDateExposure: Number($("paperMaxCityDateExposure").value || 200),
  };
  const stakeText = $("competitionStake")?.value.trim();
  if (stakeText) payload.stakeUsdc = Number(stakeText);
  return payload;
}

function renderModelCompetitionStats(statistics = {}) {
  const modelRows = (statistics.byModel || []).map((row) => `<tr>
    <td>${escapeHtml(row.model)}</td><td>${row.orderCount}</td><td>${row.settledOrderCount}</td>
    <td>${row.hitCount}</td><td>${pct(row.winRate)}</td><td>${money(row.totalStake)}</td>
    <td>${money(row.totalPayout)}</td><td>${money(row.realizedPnl)}</td><td>${pct(row.roi)}</td><td>${signedPct(row.averageEdge)}</td>
  </tr>`);
  $("modelCompetitionLeaderboard").innerHTML = `<h2>模型排行榜</h2>${table(["模型", "订单", "已结算", "命中", "胜率", "Stake", "Payout", "PnL", "ROI", "平均 Edge"], modelRows)}`;
  const cityRows = (statistics.byCityModel || []).map((row) => `<tr>
    <td>${escapeHtml(row.cityName || row.cityId || "-")}</td><td>${escapeHtml(row.model)}</td>
    <td>${row.orderCount}</td><td>${row.settledOrderCount}</td><td>${pct(row.winRate)}</td>
    <td>${money(row.realizedPnl)}</td><td>${pct(row.roi)}</td>
  </tr>`);
  $("modelCompetitionCityLeaderboard").innerHTML = `<h2>城市 × 模型排行榜</h2>${table(["城市", "模型", "订单", "已结算", "胜率", "PnL", "ROI"], cityRows)}`;
}

function renderModelCompetitionResults(result) {
  const rows = (result?.results || []).map((row) => `<tr>
    <td>${escapeHtml(row.model)}</td><td>${escapeHtml(row.cityName || row.cityId || "-")}</td>
    <td>${escapeHtml(compactBucketLabel(row.bucketLabel, row.bucketKey))}</td><td>${pct(row.ensembleProbability)}</td>
    <td>${signedPct(row.edge)}</td><td><span class="badge ${row.accepted ? "" : "danger"}">${row.accepted ? "已下单" : "未下单"}</span></td>
    <td>${escapeHtml(rejectReasonName(row.rejectReason))}</td>
  </tr>`);
  $("modelCompetitionResults").innerHTML = `<h2>本次模型竞赛</h2>${table(["模型", "城市", "温度桶", "概率", "Edge", "结果", "原因"], rows)}`;
  renderModelCompetitionStats(result?.statistics || {});
}

async function runModelCompetition() {
  const button = $("runModelCompetitionButton");
  const payload = modelCompetitionPayload();
  if (!payload.models.length || !payload.cityIds.length) {
    showNotice("请至少选择一个模型和一个城市。", true);
    return;
  }
  button.disabled = true;
  showNotice("正在运行模型竞赛并保存 BUY_YES 虚拟订单...");
  try {
    const response = await fetch(`${API_BASE}/api/model-competition/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const result = await readJsonResponse(response, "运行模型竞赛失败");
    renderModelCompetitionResults(result);
    showNotice(`模型竞赛完成：${result.summary?.acceptedOrderCount || 0} 笔 BUY_YES 虚拟订单已成交。`);
  } catch (error) {
    showNotice(error instanceof Error ? error.message : String(error), true);
  } finally {
    button.disabled = false;
  }
}

async function refreshModelCompetitionStats() {
  try {
    const response = await fetch(`${API_BASE}/api/model-competition/stats`);
    const result = await readJsonResponse(response, "读取模型竞赛统计失败");
    renderModelCompetitionStats(result.statistics || {});
  } catch (error) {
    showNotice(error instanceof Error ? error.message : String(error), true);
  }
}

async function fetchEnsemble(endpoint = "ensemble") {
  const button = endpoint === "ensemble-signal"
    ? $("fetchEnsembleSignalButton")
    : $("fetchEnsembleButton");
  button.disabled = true;
  showNotice("正在计算 ensemble 经验分布...");
  try {
    const response = await fetch(`${API_BASE}/api/${endpoint}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(ensemblePayload(endpoint === "ensemble-signal")),
    });
    const result = await readJsonResponse(response, "获取 ensemble 失败");
    renderEnsemble(result);
    if (endpoint === "ensemble-signal") {
      latestEnsembleSignalResult = result;
      syncLlmButtons();
    }
    await loadHistory();
  } catch (error) {
    showNotice(error instanceof Error ? error.message : String(error), true);
  } finally {
    button.disabled = false;
  }
}

async function loadHistory() {
  try {
    const [
      runsResponse,
      probabilitiesResponse,
      settlementsResponse,
      outcomesResponse,
      calibrationResponse,
    ] = await Promise.all([
      fetch(`${API_BASE}/api/db/runs?limit=8`),
      fetch(`${API_BASE}/api/db/probabilities?limit=10`),
      fetch(`${API_BASE}/api/settlements/recent?limit=8`),
      fetch(`${API_BASE}/api/signals/outcomes?limit=10`),
      fetch(`${API_BASE}/api/calibration`),
    ]);
    const runs = await readJsonResponse(runsResponse, "读取 runs 失败");
    const probabilities = await readJsonResponse(probabilitiesResponse, "读取 probabilities 失败");
    const settlements = await readJsonResponse(settlementsResponse, "读取 settlements 失败");
    const outcomes = await readJsonResponse(outcomesResponse, "读取 outcomes 失败");
    const calibration = await readJsonResponse(calibrationResponse, "读取 calibration 失败");
    renderHistory(runs, probabilities, settlements, outcomes, calibration);
  } catch {
    renderHistory(
      { runs: [] },
      { probabilities: [] },
      { settlements: [] },
      { outcomes: [] },
      { summary: {}, probabilityBins: [] },
    );
  }
}

async function importSettlement() {
  const button = $("importSettlementButton");
  button.disabled = true;
  showNotice("正在通过结算接口导入观测...");
  try {
    const response = await fetch(`${API_BASE}/api/settlements/import`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(settlementPayload()),
    });
    const result = await readJsonResponse(response, "导入结算失败");
    const summary = result.summary || {};
    if (summary.status === "failed") {
      showNotice(summary.errorMessage || "结算接口导入失败", true);
    } else {
      showNotice(`结算已导入，匹配 ${summary.outcomeCount || 0} 条历史信号。`);
    }
    await loadHistory();
  } catch (error) {
    showNotice(error instanceof Error ? error.message : String(error), true);
  } finally {
    button.disabled = false;
  }
}

async function reconcileSignals() {
  const button = $("reconcileSignalsButton");
  button.disabled = true;
  showNotice("正在重新匹配历史信号...");
  try {
    const response = await fetch(`${API_BASE}/api/signals/reconcile`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        cityId: $("citySelect").value,
        targetDate: $("targetDate").value,
        kind: $("temperatureKind").value,
      }),
    });
    const result = await readJsonResponse(response, "重新匹配失败");
    showNotice(`已匹配 ${result.summary?.outcomeCount || 0} 条历史信号。`);
    await loadHistory();
  } catch (error) {
    showNotice(error instanceof Error ? error.message : String(error), true);
  } finally {
    button.disabled = false;
  }
}

async function loadPaperPortfolio() {
  try {
    const params = new URLSearchParams({
      initialCash: String(Number($("paperInitialCash").value || 1000)),
      limit: "10",
    });
    const response = await fetch(`${API_BASE}/api/paper/portfolio?${params.toString()}`);
    const result = await readJsonResponse(response, "读取虚拟盘失败");
    renderPaperPortfolio(result);
  } catch {
    renderPaperPortfolio({ summary: {}, orders: [], positions: [] });
  }
}

async function loadPaperMonitorStatus({ refreshPortfolio = false } = {}) {
  try {
    const response = await fetch(`${API_BASE}/api/paper/monitor/status`);
    const result = await readJsonResponse(response, "读取虚拟盘轮询状态失败");
    renderPaperMonitorStatus(result);
    syncPaperMonitorStatusPolling(latestPaperMonitorResult);
    if (refreshPortfolio) {
      await maybeRefreshPaperPortfolioFromMonitor(latestPaperMonitorResult);
    }
  } catch (error) {
    renderPaperMonitorStatus({
      enabled: false,
      running: false,
      intervalMs: Number($("paperMonitorIntervalMs")?.value || PAPER_MONITOR_INTERVAL_MS),
      lastError: error instanceof Error ? error.message : String(error),
    });
    stopPaperMonitorStatusPolling();
  }
}

async function refreshPaperMarks() {
  const button = $("paperMarkButton");
  button.disabled = true;
  showNotice("正在刷新持仓盘口...");
  try {
    const response = await fetch(`${API_BASE}/api/paper/monitor/tick`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(paperMarkPayload()),
    });
    const result = await readJsonResponse(response, "刷新持仓盘口失败");
    renderPaperMonitorStatus(result);
    syncPaperMonitorStatusPolling(latestPaperMonitorResult);
    paperMonitorPortfolioRefreshKey = paperMonitorRefreshKey(latestPaperMonitorResult);
    renderPaperPortfolio(result.result || result);
    const summary = result.result?.summary || result.summary || {};
    if (summary.skippedReason === "NO_OPEN_POSITIONS") {
      showNotice("当前没有 OPEN 虚拟持仓，轮询本次跳过。");
    } else {
      showNotice(`持仓盘口已刷新：${summary.markCount || 0} 个快照，${summary.warningCount || 0} 个警告。`);
    }
    await maybeRefreshPaperHedgeFromMonitor(latestPaperMonitorResult);
  } catch (error) {
    showNotice(error instanceof Error ? error.message : String(error), true);
  } finally {
    button.disabled = false;
  }
}

async function startPaperMonitor() {
  const button = $("paperMonitorStartButton");
  button.disabled = true;
  showNotice("正在启动虚拟盘持仓轮询...");
  try {
    const payload = {
      ...paperMarkPayload(),
      intervalMs: Number($("paperMonitorIntervalMs").value || PAPER_MONITOR_INTERVAL_MS),
    };
    const response = await fetch(`${API_BASE}/api/paper/monitor/start`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const result = await readJsonResponse(response, "启动虚拟盘轮询失败");
    renderPaperMonitorStatus(result);
    syncPaperMonitorStatusPolling(latestPaperMonitorResult);
    showNotice("虚拟盘持仓轮询已启动。");
  } catch (error) {
    showNotice(error instanceof Error ? error.message : String(error), true);
  } finally {
    button.disabled = false;
  }
}

async function stopPaperMonitor() {
  const button = $("paperMonitorStopButton");
  button.disabled = true;
  showNotice("正在停止虚拟盘持仓轮询...");
  try {
    const response = await fetch(`${API_BASE}/api/paper/monitor/stop`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const result = await readJsonResponse(response, "停止虚拟盘轮询失败");
    renderPaperMonitorStatus(result);
    stopPaperMonitorStatusPolling();
    showNotice("虚拟盘持仓轮询已停止。");
  } catch (error) {
    showNotice(error instanceof Error ? error.message : String(error), true);
  } finally {
    button.disabled = false;
  }
}

function requirePaperSignal() {
  const signal = bestPaperSignal();
  if (!signal) {
    showNotice("请先运行 Ensemble Signal，并确保存在 BUY_YES 候选。", true);
    return null;
  }
  return signal;
}

async function previewPaperBuy() {
  const signal = requirePaperSignal();
  if (!signal) return;
  const button = $("paperPreviewButton");
  button.disabled = true;
  showNotice("正在计算虚拟买入 preview...");
  try {
    const response = await fetch(`${API_BASE}/api/paper/preview`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(paperPayload(signal)),
    });
    const result = await readJsonResponse(response, "虚拟买入 preview 失败");
    renderPaperPreview(result.preview);
    renderPaperPortfolio(result);
    showNotice(result.preview?.accepted ? "虚拟买入 preview 可执行。" : `虚拟买入被拒绝：${rejectReasonName(result.preview?.rejectReason)}`);
  } catch (error) {
    showNotice(error instanceof Error ? error.message : String(error), true);
  } finally {
    syncLlmButtons();
  }
}

async function executePaperBuy() {
  const signal = requirePaperSignal();
  if (!signal) return;
  const button = $("paperBuyButton");
  button.disabled = true;
  showNotice("正在保存虚拟买入...");
  try {
    const response = await fetch(`${API_BASE}/api/paper/buy`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(paperPayload(signal)),
    });
    const result = await readJsonResponse(response, "虚拟买入失败");
    renderPaperPreview(result.preview);
    renderPaperPortfolio(result);
    showNotice(result.summary?.accepted ? "虚拟买入已记录。" : `虚拟订单已拒绝并记录：${rejectReasonName(result.summary?.rejectReason)}`);
    if (result.summary?.accepted) {
      await refreshPaperMarks();
      if (!latestPaperMonitorResult?.enabled) {
        await startPaperMonitor();
      }
    }
  } catch (error) {
    showNotice(error instanceof Error ? error.message : String(error), true);
  } finally {
    syncLlmButtons();
  }
}

async function previewPaperHedge() {
  const signal = requirePaperSignal();
  if (!signal) return;
  const button = $("paperHedgePreviewButton");
  button.disabled = true;
  showNotice("正在计算 hedge preview...");
  try {
    await fetchPaperHedgePreview();
    showNotice("Hedge preview 已更新。");
  } catch (error) {
    showNotice(error instanceof Error ? error.message : String(error), true);
  } finally {
    button.disabled = false;
    syncLlmButtons();
  }
}

async function reconcilePaper() {
  const button = $("paperReconcileButton");
  button.disabled = true;
  showNotice("正在结算虚拟盘...");
  try {
    const response = await fetch(`${API_BASE}/api/paper/reconcile`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        initialCash: Number($("paperInitialCash").value),
        autoImportSettlements: true,
      }),
    });
    const result = await readJsonResponse(response, "虚拟盘结算失败");
    renderPaperPortfolio(result);
    const summary = result.summary || {};
    const importText = summary.importedSettlementCount
      ? `，自动导入 ${summary.importedSettlementCount} 条结算`
      : "";
    const skippedText = summary.skippedSettlementImportCount
      ? `，跳过 ${summary.skippedSettlementImportCount} 个未完成日期`
      : "";
    const errorText = summary.importErrorCount
      ? `，${summary.importErrorCount} 个结算导入失败`
      : "";
    const issueText = settlementImportIssueText(result);
    showNotice(
      `虚拟盘结算 ${summary.settledPositionCount || 0} 个持仓${importText}${skippedText}${errorText}，Realized PnL ${money(summary.realizedPnl)}。${issueText}`,
      Boolean(summary.importErrorCount) && !summary.settledPositionCount,
    );
  } catch (error) {
    showNotice(error instanceof Error ? error.message : String(error), true);
  } finally {
    button.disabled = false;
  }
}

async function explainResult() {
  const result = latestEnsembleSignalResult;
  const button = $("explainSignalButton");
  if (!result) {
    showNotice("请先运行 Ensemble Signal。", true);
    return;
  }
  button.disabled = true;
  showNotice("正在生成 AI 解读...");
  try {
    const response = await fetch(`${API_BASE}/api/llm-summary`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind: "ensemble-signal", result }),
    });
    const payload = await readJsonResponse(response, "生成 AI 解读失败");
    showLlmSummary(payload.summary || "-");
    showNotice("AI 解读已生成。");
  } catch (error) {
    showNotice(error instanceof Error ? error.message : String(error), true);
  } finally {
    syncLlmButtons();
  }
}

async function init() {
  $("targetDate").value = todayLocalISO();
  $("citySelect")?.addEventListener("change", syncUnitFromCity);
  $("fetchEnsembleButton")?.addEventListener("click", () => fetchEnsemble("ensemble"));
  $("fetchEnsembleSignalButton")?.addEventListener("click", () => fetchEnsemble("ensemble-signal"));
  $("fetchMarketButton")?.addEventListener("click", fetchMarkets);
  $("importSettlementButton")?.addEventListener("click", importSettlement);
  $("reconcileSignalsButton")?.addEventListener("click", reconcileSignals);
  $("paperPreviewButton")?.addEventListener("click", previewPaperBuy);
  $("paperBuyButton")?.addEventListener("click", executePaperBuy);
  $("paperMarkButton")?.addEventListener("click", refreshPaperMarks);
  $("paperMonitorStartButton")?.addEventListener("click", startPaperMonitor);
  $("paperMonitorStopButton")?.addEventListener("click", stopPaperMonitor);
  $("paperHedgePreviewButton")?.addEventListener("click", previewPaperHedge);
  $("paperReconcileButton")?.addEventListener("click", reconcilePaper);
  $("runModelCompetitionButton")?.addEventListener("click", runModelCompetition);
  $("refreshModelCompetitionStatsButton")?.addEventListener("click", refreshModelCompetitionStats);
  $("explainSignalButton")?.addEventListener("click", explainResult);
  syncLlmButtons();
  await loadCities();
  await loadHistory();
  await loadPaperPortfolio();
  await loadPaperMonitorStatus();
  await refreshModelCompetitionStats();
}

void init();
