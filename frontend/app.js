const $ = (id) => document.getElementById(id);
const API_BASE = window.WEATHER_API_BASE || window.location.origin;
const SELECTED_CITY_STORAGE_KEY = "weatherSelectedCityId";
let cityRecords = [];

const samplePositions = `outcome,shares,total_cost,price,best_bid,best_ask,probability
84 to 85,100,32,0.50,0.46,0.54,0.34
85 to 86,80,28,0.42,0.38,0.47,0.28`;

const sampleMarkets = `outcome,price,best_bid,best_ask,probability
83 to 84,0.20,0.16,0.24,0.08
84 to 85,0.50,0.46,0.54,0.34
85 to 86,0.42,0.38,0.47,0.28
86 to 87,0.18,0.14,0.23,0.07`;

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
  notice.style.borderColor = isError ? "#fecdca" : "#fed7aa";
  notice.style.color = isError ? "#b42318" : "#b54708";
  notice.style.background = isError ? "#fef3f2" : "#fff7ed";
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

function payload() {
  return {
    positionsCsv: $("positionsCsv").value,
    marketsCsv: $("marketsCsv").value,
    marketSlug: $("marketSlug").value,
    conditionId: $("conditionId").value,
    city: marketCityValue(),
    marketQuery: $("marketQuery").value,
    targetDate: $("targetDate").value,
    temperatureKind: $("temperatureKind").value,
    includeOrderbooks: $("includeOrderbooks").checked,
    unit: $("unit").value,
    feeRate: Number($("feeRate").value),
    minCashoutRatio: Number($("minCashoutRatio").value),
    targetProfit: Number($("targetProfit").value),
    tailProbabilityCutoff: Number($("tailProbabilityCutoff").value),
    maxTailProbability: Number($("maxTailProbability").value),
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

function selectedWeatherModels() {
  const models = Array.from($("weatherModels").selectedOptions).map((option) => option.value);
  return models.length ? models : ["ecmwf_ifs025", "icon_seamless", "meteofrance_seamless"];
}

function forecastPayload() {
  const city = selectedCityRecord();
  return {
    city: $("citySelect").value,
    ...customLocationPayload(),
    unit: city?.settlementUnit || $("unit").value,
    targetDate: $("targetDate").value,
    temperatureKind: $("temperatureKind").value,
    models: selectedWeatherModels(),
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
    marketsCsv: $("marketsCsv").value,
    includeOrderbooks: $("includeOrderbooks").checked,
    includeMarketBuckets,
    feeRate: Number($("feeRate").value),
    minEdge: Number($("minEdge").value),
    saveSqlite: $("saveSqlite").checked,
  };
}

function levelText(levels) {
  if (!levels || !levels.length) return "-";
  return levels
    .slice(0, 3)
    .map((level) => `${price(level.price)} x ${money(level.size)}`)
    .join(" / ");
}

function renderForecast(result) {
  const summary = result.summary;
  const metricHtml = [
    metric("城市", summary.cityName || summary.cityId || "-"),
    metric("坐标", `${price(summary.latitude)}, ${price(summary.longitude)}`),
    metric("时区", summary.timezone || "-"),
    metric("日期", summary.targetDate || "-"),
    metric("当地时间", formatLocalTime(summary.timezone)),
    metric("类型", kindName(summary.kind)),
    metric("均值", temperature(summary.mean, summary.unit)),
    metric("范围", `${temperature(summary.min, summary.unit)} / ${temperature(summary.max, summary.unit)}`),
    metric("模型数", summary.modelCount ?? "-"),
  ].join("");
  const rows = result.points.map((row) => `<tr>
    <td>${escapeHtml(modelName(row.sourceModel))}</td>
    <td>${temperature(row.value, row.unit)}</td>
    <td>${escapeHtml(row.provider || "open-meteo")}</td>
    <td>${escapeHtml(row.forecastGranularity || "-")}</td>
    <td>${escapeHtml(row.settlementStation || "-")}</td>
    <td>${escapeHtml(row.generatedAt || "-")}</td>
  </tr>`);
  $("forecast").innerHTML = `<div class="metrics">${metricHtml}</div>${table(
    ["模型", "温度", "来源", "粒度", "结算站", "生成时间"],
    rows,
  )}`;
  showNotice(`已获取 ${summary.modelCount} 个天气模型预报。`);
}

function probabilityChart(chart) {
  const labels = chart.bucketLabels || [];
  const probabilities = chart.bucketProbabilities || [];
  const market = chart.marketImpliedProbabilities || [];
  const members = chart.memberValues || [];
  const width = Math.max(720, labels.length * 82 + 80);
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
    return `
      <rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barWidth.toFixed(1)}" height="${h.toFixed(1)}" rx="3" fill="#2563eb"></rect>
      ${marketY === null ? "" : `<circle cx="${(x + barWidth / 2).toFixed(1)}" cy="${marketY.toFixed(1)}" r="4" fill="#b42318"></circle>`}
      <text class="chart-label" x="${(x + barWidth / 2).toFixed(1)}" y="${height - 42}" text-anchor="middle">${escapeHtml(label)}</text>
      <text class="chart-label" x="${(x + barWidth / 2).toFixed(1)}" y="${Math.max(14, y - 6).toFixed(1)}" text-anchor="middle">${pct(probability)}</text>
    `;
  }).join("");
  const keyToIndex = new Map((chart.bucketKeys || []).map((key, index) => [key, index]));
  const rug = members.map((member, index) => {
    const bucketIndex = keyToIndex.get(member.bucketKey);
    if (bucketIndex === undefined) return "";
    const jitter = ((index % 7) - 3) * 3;
    const x = left + bucketIndex * slot + slot / 2 + jitter;
    return `<line x1="${x.toFixed(1)}" x2="${x.toFixed(1)}" y1="${height - 34}" y2="${height - 24}" stroke="#0f766e" stroke-width="1.5"></line>`;
  }).join("");
  return `
    <div class="chart-wrap">
      <svg class="prob-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="Ensemble probability chart">
        <line x1="${left}" y1="${top + plotHeight}" x2="${width - right}" y2="${top + plotHeight}" stroke="#d9dee8"></line>
        <line x1="${left}" y1="${top}" x2="${left}" y2="${top + plotHeight}" stroke="#d9dee8"></line>
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
    <td>${escapeHtml(row.bucketLabel)}</td>
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

function renderHistory(runsResult, probabilitiesResult) {
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

function render(result) {
  const summary = result.summary;
  $("summary").innerHTML = [
    metric("盘口来源", sourceName(summary.marketSource)),
    metric("市场桶数", summary.marketCount ?? "-"),
    metric("当前成本", money(summary.currentCost)),
    metric("Mark value", money(summary.markValue)),
    metric("Liquidation", money(summary.liquidationValue)),
    metric("Cashout ratio", pct(summary.cashoutRatio)),
    metric("Hedge action", summary.recommendation),
    metric("Covered probability", pct(summary.coveredProbability)),
    metric("Tail risk", pct(summary.uncoveredTailProbability)),
    metric("Worst-case PnL", money(summary.worstCasePnl)),
    metric("Covered worst PnL", money(summary.coveredWorstCasePnl)),
    metric("Hedge cost", money(summary.hedgeCost)),
    metric("Ask sum", price(summary.askSum)),
    metric("True arbitrage", summary.isTrueArbitrage ? "是" : "否"),
  ].join("");

  const valuationRows = result.valuations.map(
    (row) => `<tr>
      <td>${escapeHtml(row.outcome)}</td>
      <td>${money(row.shares)}</td>
      <td>${money(row.cost)}</td>
      <td>${price(row.markPrice)}</td>
      <td>${price(row.bestBid)}</td>
      <td>${price(row.bestAsk)}</td>
      <td>${money(row.markValue)}</td>
      <td>${money(row.liquidationValue)}</td>
      <td>${pct(row.cashoutRatio)}</td>
      <td>${money(row.executablePnl)}</td>
    </tr>`,
  );
  $("valuations").innerHTML = table(
    ["温度桶", "份额", "成本", "mark", "bid", "ask", "Mark value", "Liquidation", "Cashout", "可执行 PnL"],
    valuationRows,
  );

  const exitRows = result.exits.flatMap((plan) =>
    plan.ladder.map(
      (leg) => `<tr>
        <td>${escapeHtml(plan.outcome)}</td>
        <td><span class="badge">${escapeHtml(plan.action)}</span></td>
        <td>${pct(leg.fraction)}</td>
        <td>${money(leg.shares)}</td>
        <td>${price(leg.limitPrice)}</td>
        <td>${money(leg.netValue)}</td>
        <td>${money(plan.retainedShares)}</td>
      </tr>`,
    ),
  );
  $("exits").innerHTML = table(
    ["温度桶", "动作", "比例", "份额", "限价", "成交后净额", "保留到结算"],
    exitRows,
  );

  const hedgeRows = result.hedgeLegs.map(
    (row) => `<tr>
      <td>${escapeHtml(row.outcome)}</td>
      <td>${escapeHtml(row.action)}</td>
      <td>${money(row.shares)}</td>
      <td>${price(row.price)}</td>
      <td>${money(row.cost)}</td>
    </tr>`,
  );
  $("hedgeLegs").innerHTML = table(["温度桶", "动作", "买入份额", "ask", "成本含费"], hedgeRows);

  const scenarioRows = result.scenarios.map(
    (row) => `<tr>
      <td>${escapeHtml(row.outcome)}</td>
      <td>${pct(row.probability)}</td>
      <td>${money(row.payoff)}</td>
      <td>${money(row.totalCost)}</td>
      <td>${money(row.netPnl)}</td>
      <td><span class="badge ${row.isCovered ? "" : "warn"}">${row.isCovered ? "核心覆盖" : "尾部风险"}</span></td>
    </tr>`,
  );
  $("scenarios").innerHTML = table(
    ["结算桶", "概率", "Payoff", "总成本", "净收益", "覆盖"],
    scenarioRows,
  );

  const notes = summary.notes && summary.notes.length ? `说明：${summary.notes.join("；")}` : "";
  showNotice(notes || "页面浮盈不是已实现收益，真实收益以成交净额和最终结算为准。");
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
    if (selected) {
      $("citySelect").value = selected;
      rememberCityId(selected);
      syncUnitFromCity();
    }
  } catch (error) {
    showNotice(error instanceof Error ? error.message : String(error), true);
  }
}

async function fetchForecast() {
  const button = $("fetchForecastButton");
  button.disabled = true;
  showNotice("正在从 Open-Meteo 获取预报...");
  try {
    const response = await fetch(`${API_BASE}/api/forecast`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(forecastPayload()),
    });
    const result = await readJsonResponse(response, "获取预报失败");
    renderForecast(result);
  } catch (error) {
    showNotice(error instanceof Error ? error.message : String(error), true);
  } finally {
    button.disabled = false;
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
    await loadHistory();
  } catch (error) {
    showNotice(error instanceof Error ? error.message : String(error), true);
  } finally {
    button.disabled = false;
  }
}

async function loadHistory() {
  try {
    const [runsResponse, probabilitiesResponse] = await Promise.all([
      fetch(`${API_BASE}/api/db/runs?limit=8`),
      fetch(`${API_BASE}/api/db/probabilities?limit=10`),
    ]);
    const runs = await readJsonResponse(runsResponse, "读取 runs 失败");
    const probabilities = await readJsonResponse(probabilitiesResponse, "读取 probabilities 失败");
    renderHistory(runs, probabilities);
  } catch {
    renderHistory({ runs: [] }, { probabilities: [] });
  }
}

async function run() {
  const button = $("runButton");
  button.disabled = true;
  showNotice("正在计算...");
  try {
    const response = await fetch(`${API_BASE}/api/portfolio`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload()),
    });
    const result = await readJsonResponse(response, "组合评估失败");
    render(result);
  } catch (error) {
    showNotice(error instanceof Error ? error.message : String(error), true);
  } finally {
    button.disabled = false;
  }
}

async function init() {
  $("positionsCsv").placeholder = samplePositions;
  $("marketsCsv").placeholder = sampleMarkets;
  $("targetDate").value = todayLocalISO();
  $("citySelect").addEventListener("change", syncUnitFromCity);
  $("fetchForecastButton").addEventListener("click", fetchForecast);
  $("fetchEnsembleButton").addEventListener("click", () => fetchEnsemble("ensemble"));
  $("fetchEnsembleSignalButton").addEventListener("click", () => fetchEnsemble("ensemble-signal"));
  $("fetchMarketButton").addEventListener("click", fetchMarkets);
  $("runButton").addEventListener("click", run);
  await loadCities();
  await loadHistory();
}

void init();
