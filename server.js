import { createServer } from "node:http";
import { spawn } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { extname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(fileURLToPath(new URL(".", import.meta.url)));
const FRONTEND_DIR = join(ROOT, "frontend");

loadEnvFile();

const FRONTEND_PORT = parsePort(process.env.FRONTEND_PORT || process.env.PORT || "58888", "FRONTEND_PORT");
const BACKEND_PORT = parsePort(process.env.BACKEND_PORT || "56666", "BACKEND_PORT");
const HOST = process.env.HOST || "127.0.0.1";
const CORS_HEADERS = {
  "Access-Control-Allow-Origin": process.env.CORS_ORIGIN || "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};
const PAPER_MONITOR_DEFAULT_INTERVAL_MS = parsePositiveInteger(
  process.env.PAPER_MONITOR_INTERVAL_MS || "60000",
  "PAPER_MONITOR_INTERVAL_MS",
);
const PAPER_MONITOR_AUTO_START = String(process.env.PAPER_MONITOR_AUTO_START || "1") !== "0";

function parsePort(value, name) {
  const port = Number(value);
  if (!Number.isInteger(port) || port < 0 || port > 65535) {
    throw new Error(`${name} must be an integer between 0 and 65535. Received: ${value}`);
  }
  return port;
}

function parsePositiveInteger(value, name) {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed <= 0) {
    throw new Error(`${name} must be a positive integer. Received: ${value}`);
  }
  return parsed;
}

function parseEnvLine(line) {
  let text = line.trim();
  if (!text || text.startsWith("#")) {
    return null;
  }
  if (text.startsWith("export ")) {
    text = text.slice("export ".length).trimStart();
  }
  const equalsIndex = text.indexOf("=");
  if (equalsIndex <= 0) {
    return null;
  }
  const key = text.slice(0, equalsIndex).trim();
  if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) {
    return null;
  }
  let value = stripInlineComment(text.slice(equalsIndex + 1).trim());
  if (value.length >= 2 && value[0] === value[value.length - 1] && (value[0] === "\"" || value[0] === "'")) {
    value = value.slice(1, -1);
  }
  return [key, value];
}

function stripInlineComment(value) {
  let inSingle = false;
  let inDouble = false;
  for (let index = 0; index < value.length; index += 1) {
    const char = value[index];
    if (char === "'" && !inDouble) {
      inSingle = !inSingle;
    } else if (char === "\"" && !inSingle) {
      inDouble = !inDouble;
    } else if (char === "#" && !inSingle && !inDouble && (index === 0 || /\s/.test(value[index - 1]))) {
      return value.slice(0, index).trimEnd();
    }
  }
  return value.trim();
}

function loadEnvFile() {
  const envPath = join(ROOT, ".env");
  if (!existsSync(envPath)) {
    return;
  }
  for (const line of readFileSync(envPath, "utf-8").split(/\r?\n/)) {
    const parsed = parseEnvLine(line);
    if (!parsed) {
      continue;
    }
    const [key, value] = parsed;
    if (!(key in process.env)) {
      process.env[key] = value;
    }
  }
}

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
};

function send(res, status, body, headers = {}) {
  res.writeHead(status, headers);
  res.end(body);
}

function readBody(req) {
  return new Promise((resolveBody, reject) => {
    let body = "";
    req.on("data", (chunk) => {
      body += chunk;
      if (body.length > 1024 * 1024) {
        reject(new Error("request body too large"));
        req.destroy();
      }
    });
    req.on("end", () => resolveBody(body));
    req.on("error", reject);
  });
}

function pythonBin() {
  const venvPython = join(ROOT, ".venv", "bin", "python");
  if (existsSync(venvPython)) {
    return venvPython;
  }
  return process.env.PYTHON || "python3";
}

function runWeatherApi(command, payload) {
  return new Promise((resolveApi, reject) => {
    const child = spawn(pythonBin(), ["-m", "weather_quant.web_api", command], {
      cwd: ROOT,
      env: {
        ...process.env,
        PYTHONPATH: [join(ROOT, "src"), process.env.PYTHONPATH || ""].filter(Boolean).join(":"),
      },
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
    });
    child.on("error", reject);
    child.on("close", (code) => {
      if (code !== 0) {
        reject(new Error(stderr || `weather api exited with ${code}`));
        return;
      }
      try {
        resolveApi(JSON.parse(stdout || "{}"));
      } catch (error) {
        reject(error);
      }
    });
    child.stdin.end(JSON.stringify(payload));
  });
}

function errorMessage(error) {
  const raw = error instanceof Error ? error.message : String(error);
  try {
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed.error === "string") {
      return parsed.error;
    }
  } catch {
    // Fall through to the raw message.
  }
  return raw.trim();
}

async function handleApi(req, res, command) {
  try {
    const body = await readBody(req);
    const url = new URL(req.url || "/", `http://${req.headers.host || "localhost"}`);
    const queryPayload = Object.fromEntries(url.searchParams.entries());
    const payload = { ...queryPayload, ...(body ? JSON.parse(body) : {}) };
    const result = await runWeatherApi(command, payload);
    send(res, 200, JSON.stringify(result), {
      "Content-Type": "application/json; charset=utf-8",
      ...CORS_HEADERS,
    });
  } catch (error) {
    send(
      res,
      500,
      JSON.stringify({ error: errorMessage(error) }),
      { "Content-Type": "application/json; charset=utf-8", ...CORS_HEADERS },
    );
  }
}

const paperMonitor = {
  enabled: false,
  running: false,
  intervalMs: PAPER_MONITOR_DEFAULT_INTERVAL_MS,
  payloadOverrides: {},
  timer: null,
  lastRunAt: null,
  lastFinishedAt: null,
  lastResult: null,
  lastError: null,
  consecutiveErrors: 0,
  tickCount: 0,
  skippedCount: 0,
};

function paperMonitorPayload(overrides = {}) {
  return {
    initialCash: Number(process.env.PAPER_INITIAL_CASH || 1000),
    feeRate: Number(process.env.PAPER_FEE_RATE || 0.05),
    targetProfit: Number(process.env.PAPER_TARGET_PROFIT || 0.10),
    minCashoutRatio: Number(process.env.PAPER_MIN_CASHOUT_RATIO || 0.50),
    markCacheSeconds: Number(process.env.PAPER_MARK_CACHE_SECONDS || 5),
    limit: Number(process.env.PAPER_MONITOR_LIMIT || 20),
    ...paperMonitor.payloadOverrides,
    ...overrides,
  };
}

function paperMonitorStatus() {
  return {
    enabled: paperMonitor.enabled,
    running: paperMonitor.running,
    intervalMs: paperMonitor.intervalMs,
    lastRunAt: paperMonitor.lastRunAt,
    lastFinishedAt: paperMonitor.lastFinishedAt,
    lastResult: paperMonitor.lastResult,
    lastError: paperMonitor.lastError,
    consecutiveErrors: paperMonitor.consecutiveErrors,
    tickCount: paperMonitor.tickCount,
    skippedCount: paperMonitor.skippedCount,
    payloadOverrides: paperMonitor.payloadOverrides,
  };
}

async function runPaperMonitorOnce(reason = "interval", payloadOverrides = {}) {
  if (paperMonitor.running) {
    paperMonitor.skippedCount += 1;
    return { skipped: true, reason: "RUNNING", monitor: paperMonitorStatus() };
  }
  paperMonitor.running = true;
  paperMonitor.lastRunAt = new Date().toISOString();
  paperMonitor.tickCount += 1;
  try {
    const payload = paperMonitorPayload(payloadOverrides);
    const portfolio = await runWeatherApi("paper-portfolio", payload);
    const openPositionCount = Number(portfolio?.summary?.openPositionCount || 0);
    let result;
    if (openPositionCount > 0) {
      result = await runWeatherApi("paper-mark", payload);
    } else {
      result = {
        summary: {
          openPositionCount,
          markCount: 0,
          warningCount: 0,
          skippedReason: "NO_OPEN_POSITIONS",
          noRealOrder: true,
        },
        portfolio,
      };
    }
    paperMonitor.lastResult = {
      reason,
      summary: result.summary || {},
      portfolioSummary: result.portfolio?.summary || portfolio?.summary || {},
    };
    paperMonitor.lastError = null;
    paperMonitor.consecutiveErrors = 0;
    return { result, monitor: paperMonitorStatus() };
  } catch (error) {
    paperMonitor.lastError = errorMessage(error);
    paperMonitor.consecutiveErrors += 1;
    throw error;
  } finally {
    paperMonitor.running = false;
    paperMonitor.lastFinishedAt = new Date().toISOString();
  }
}

function startPaperMonitor(intervalMs = PAPER_MONITOR_DEFAULT_INTERVAL_MS, payloadOverrides = {}) {
  paperMonitor.intervalMs = Math.max(1000, Number(intervalMs) || PAPER_MONITOR_DEFAULT_INTERVAL_MS);
  paperMonitor.payloadOverrides = { ...payloadOverrides };
  paperMonitor.enabled = true;
  if (paperMonitor.timer) {
    clearInterval(paperMonitor.timer);
  }
  paperMonitor.timer = setInterval(() => {
    void runPaperMonitorOnce("interval").catch(() => {});
  }, paperMonitor.intervalMs);
  void runPaperMonitorOnce("start").catch(() => {});
  return paperMonitorStatus();
}

function stopPaperMonitor() {
  paperMonitor.enabled = false;
  if (paperMonitor.timer) {
    clearInterval(paperMonitor.timer);
    paperMonitor.timer = null;
  }
  return paperMonitorStatus();
}

async function handlePaperMonitor(req, res, action) {
  try {
    const body = req.method === "POST" ? await readBody(req) : "";
    const payload = body ? JSON.parse(body) : {};
    let result;
    if (action === "start") {
      const { intervalMs, ...payloadOverrides } = payload;
      result = { monitor: startPaperMonitor(intervalMs || PAPER_MONITOR_DEFAULT_INTERVAL_MS, payloadOverrides) };
    } else if (action === "stop") {
      result = { monitor: stopPaperMonitor() };
    } else if (action === "tick") {
      result = await runPaperMonitorOnce("manual", payload);
    } else {
      result = { monitor: paperMonitorStatus() };
    }
    send(res, 200, JSON.stringify(result), {
      "Content-Type": "application/json; charset=utf-8",
      ...CORS_HEADERS,
    });
  } catch (error) {
    send(
      res,
      500,
      JSON.stringify({ error: errorMessage(error), monitor: paperMonitorStatus() }),
      { "Content-Type": "application/json; charset=utf-8", ...CORS_HEADERS },
    );
  }
}

function serveStatic(req, res) {
  const url = new URL(req.url || "/", `http://${req.headers.host || "localhost"}`);
  const requestedPath = url.pathname === "/" ? "/index.html" : url.pathname;
  const filePath = resolve(join(FRONTEND_DIR, requestedPath));
  if (!filePath.startsWith(FRONTEND_DIR) || !existsSync(filePath)) {
    send(res, 404, "Not found", { "Content-Type": "text/plain; charset=utf-8" });
    return;
  }
  send(res, 200, readFileSync(filePath), {
    "Content-Type": MIME[extname(filePath)] || "application/octet-stream",
  });
}

const backendServer = createServer((req, res) => {
  if (req.method === "OPTIONS") {
    send(res, 204, "", CORS_HEADERS);
    return;
  }
  if (req.method === "POST" && req.url === "/api/markets") {
    void handleApi(req, res, "markets");
    return;
  }
  if (req.method === "POST" && req.url === "/api/portfolio") {
    void handleApi(req, res, "portfolio");
    return;
  }
  if (req.method === "POST" && req.url === "/api/forecast") {
    void handleApi(req, res, "forecast");
    return;
  }
  if (req.method === "POST" && req.url === "/api/ensemble") {
    void handleApi(req, res, "ensemble");
    return;
  }
  if (req.method === "POST" && req.url === "/api/ensemble-signal") {
    void handleApi(req, res, "ensemble-signal");
    return;
  }
  if (req.method === "POST" && req.url === "/api/llm-summary") {
    void handleApi(req, res, "llm-summary");
    return;
  }
  if (req.method === "GET" && req.url?.startsWith("/api/cities")) {
    void handleApi(req, res, "cities");
    return;
  }
  if (req.method === "POST" && req.url === "/api/cities") {
    void handleApi(req, res, "city-save");
    return;
  }
  if (req.method === "POST" && req.url === "/api/station-lookup") {
    void handleApi(req, res, "station-lookup");
    return;
  }
  if (req.method === "GET" && req.url?.startsWith("/api/db/runs")) {
    void handleApi(req, res, "db-runs");
    return;
  }
  if (req.method === "GET" && req.url?.startsWith("/api/db/probabilities")) {
    void handleApi(req, res, "db-probabilities");
    return;
  }
  if (req.method === "POST" && req.url === "/api/settlements/import") {
    void handleApi(req, res, "settlement-import");
    return;
  }
  if (req.method === "POST" && req.url === "/api/signals/reconcile") {
    void handleApi(req, res, "settlement-reconcile");
    return;
  }
  if (req.method === "GET" && req.url?.startsWith("/api/settlements/recent")) {
    void handleApi(req, res, "settlements-recent");
    return;
  }
  if (req.method === "GET" && req.url?.startsWith("/api/signals/outcomes")) {
    void handleApi(req, res, "signal-outcomes");
    return;
  }
  if (req.method === "GET" && req.url?.startsWith("/api/calibration")) {
    void handleApi(req, res, "calibration");
    return;
  }
  if (req.method === "POST" && req.url === "/api/paper/preview") {
    void handleApi(req, res, "paper-preview");
    return;
  }
  if (req.method === "POST" && req.url === "/api/paper/buy") {
    void handleApi(req, res, "paper-buy");
    return;
  }
  if (req.method === "GET" && req.url?.startsWith("/api/paper/portfolio")) {
    void handleApi(req, res, "paper-portfolio");
    return;
  }
  if (req.method === "POST" && req.url === "/api/paper/mark") {
    void handleApi(req, res, "paper-mark");
    return;
  }
  if (req.method === "GET" && req.url?.startsWith("/api/paper/monitor/status")) {
    void handlePaperMonitor(req, res, "status");
    return;
  }
  if (req.method === "POST" && req.url === "/api/paper/monitor/start") {
    void handlePaperMonitor(req, res, "start");
    return;
  }
  if (req.method === "POST" && req.url === "/api/paper/monitor/stop") {
    void handlePaperMonitor(req, res, "stop");
    return;
  }
  if (req.method === "POST" && req.url === "/api/paper/monitor/tick") {
    void handlePaperMonitor(req, res, "tick");
    return;
  }
  if (req.method === "POST" && req.url === "/api/paper/reconcile") {
    void handleApi(req, res, "paper-reconcile");
    return;
  }
  if (req.method === "POST" && req.url === "/api/paper/exit-preview") {
    void handleApi(req, res, "paper-exit-preview");
    return;
  }
  if (req.method === "POST" && req.url === "/api/paper/hedge-preview") {
    void handleApi(req, res, "paper-hedge-preview");
    return;
  }
  send(res, 404, JSON.stringify({ error: "Not found" }), {
    "Content-Type": "application/json; charset=utf-8",
    ...CORS_HEADERS,
  });
});

const frontendServer = createServer((req, res) => {
  if (req.method === "OPTIONS") {
    send(res, 204, "", CORS_HEADERS);
    return;
  }
  if (req.method === "POST" && req.url === "/api/markets") {
    void handleApi(req, res, "markets");
    return;
  }
  if (req.method === "POST" && req.url === "/api/portfolio") {
    void handleApi(req, res, "portfolio");
    return;
  }
  if (req.method === "POST" && req.url === "/api/forecast") {
    void handleApi(req, res, "forecast");
    return;
  }
  if (req.method === "POST" && req.url === "/api/ensemble") {
    void handleApi(req, res, "ensemble");
    return;
  }
  if (req.method === "POST" && req.url === "/api/ensemble-signal") {
    void handleApi(req, res, "ensemble-signal");
    return;
  }
  if (req.method === "POST" && req.url === "/api/llm-summary") {
    void handleApi(req, res, "llm-summary");
    return;
  }
  if (req.method === "GET" && req.url?.startsWith("/api/cities")) {
    void handleApi(req, res, "cities");
    return;
  }
  if (req.method === "POST" && req.url === "/api/cities") {
    void handleApi(req, res, "city-save");
    return;
  }
  if (req.method === "POST" && req.url === "/api/station-lookup") {
    void handleApi(req, res, "station-lookup");
    return;
  }
  if (req.method === "GET" && req.url?.startsWith("/api/db/runs")) {
    void handleApi(req, res, "db-runs");
    return;
  }
  if (req.method === "GET" && req.url?.startsWith("/api/db/probabilities")) {
    void handleApi(req, res, "db-probabilities");
    return;
  }
  if (req.method === "POST" && req.url === "/api/settlements/import") {
    void handleApi(req, res, "settlement-import");
    return;
  }
  if (req.method === "POST" && req.url === "/api/signals/reconcile") {
    void handleApi(req, res, "settlement-reconcile");
    return;
  }
  if (req.method === "GET" && req.url?.startsWith("/api/settlements/recent")) {
    void handleApi(req, res, "settlements-recent");
    return;
  }
  if (req.method === "GET" && req.url?.startsWith("/api/signals/outcomes")) {
    void handleApi(req, res, "signal-outcomes");
    return;
  }
  if (req.method === "GET" && req.url?.startsWith("/api/calibration")) {
    void handleApi(req, res, "calibration");
    return;
  }
  if (req.method === "POST" && req.url === "/api/paper/preview") {
    void handleApi(req, res, "paper-preview");
    return;
  }
  if (req.method === "POST" && req.url === "/api/paper/buy") {
    void handleApi(req, res, "paper-buy");
    return;
  }
  if (req.method === "GET" && req.url?.startsWith("/api/paper/portfolio")) {
    void handleApi(req, res, "paper-portfolio");
    return;
  }
  if (req.method === "POST" && req.url === "/api/paper/mark") {
    void handleApi(req, res, "paper-mark");
    return;
  }
  if (req.method === "GET" && req.url?.startsWith("/api/paper/monitor/status")) {
    void handlePaperMonitor(req, res, "status");
    return;
  }
  if (req.method === "POST" && req.url === "/api/paper/monitor/start") {
    void handlePaperMonitor(req, res, "start");
    return;
  }
  if (req.method === "POST" && req.url === "/api/paper/monitor/stop") {
    void handlePaperMonitor(req, res, "stop");
    return;
  }
  if (req.method === "POST" && req.url === "/api/paper/monitor/tick") {
    void handlePaperMonitor(req, res, "tick");
    return;
  }
  if (req.method === "POST" && req.url === "/api/paper/reconcile") {
    void handleApi(req, res, "paper-reconcile");
    return;
  }
  if (req.method === "POST" && req.url === "/api/paper/exit-preview") {
    void handleApi(req, res, "paper-exit-preview");
    return;
  }
  if (req.method === "POST" && req.url === "/api/paper/hedge-preview") {
    void handleApi(req, res, "paper-hedge-preview");
    return;
  }
  if (req.method === "GET" || req.method === "HEAD") {
    serveStatic(req, res);
    return;
  }
  send(res, 405, "Method not allowed", { "Content-Type": "text/plain; charset=utf-8" });
});

backendServer.listen(BACKEND_PORT, HOST, () => {
  console.log(`Weather API: http://${HOST}:${BACKEND_PORT}`);
  if (PAPER_MONITOR_AUTO_START) {
    startPaperMonitor(PAPER_MONITOR_DEFAULT_INTERVAL_MS);
    console.log(`Paper monitor: enabled every ${PAPER_MONITOR_DEFAULT_INTERVAL_MS} ms`);
  }
});

frontendServer.listen(FRONTEND_PORT, HOST, () => {
  console.log(`Weather dashboard: http://${HOST}:${FRONTEND_PORT}`);
});
