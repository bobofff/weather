import { createServer } from "node:http";
import { spawn } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { extname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(fileURLToPath(new URL(".", import.meta.url)));
const FRONTEND_DIR = join(ROOT, "frontend");
const FRONTEND_PORT = parsePort(process.env.FRONTEND_PORT || process.env.PORT || "58888", "FRONTEND_PORT");
const BACKEND_PORT = parsePort(process.env.BACKEND_PORT || "56666", "BACKEND_PORT");
const HOST = process.env.HOST || "127.0.0.1";
const CORS_HEADERS = {
  "Access-Control-Allow-Origin": process.env.CORS_ORIGIN || "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

function parsePort(value, name) {
  const port = Number(value);
  if (!Number.isInteger(port) || port < 0 || port > 65535) {
    throw new Error(`${name} must be an integer between 0 and 65535. Received: ${value}`);
  }
  return port;
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
  if (req.method === "GET" || req.method === "HEAD") {
    serveStatic(req, res);
    return;
  }
  send(res, 405, "Method not allowed", { "Content-Type": "text/plain; charset=utf-8" });
});

backendServer.listen(BACKEND_PORT, HOST, () => {
  console.log(`Weather API: http://${HOST}:${BACKEND_PORT}`);
});

frontendServer.listen(FRONTEND_PORT, HOST, () => {
  console.log(`Weather dashboard: http://${HOST}:${FRONTEND_PORT}`);
});
