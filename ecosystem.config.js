// PM2 ecosystem — long-lived HTTP-transport backrest-mcp process.
//
// backrest-mcp fronts a single Backrest instance (forge :9898), so this is ONE
// process (not per-agent like githost-mcp). scoped-mcp reaches it over
// http://127.0.0.1:8626/mcp with a bearer token.
//
// HTTP mode fails closed: main() refuses to start without a >=16-char
// BACKREST_MCP_AUTH_TOKEN and refuses a non-loopback bind unless
// BACKREST_MCP_ALLOW_NONLOOPBACK=1. Keep the bind on 127.0.0.1.
//
// Secrets are NOT hardcoded here. This file parses ~/.secrets/forge.env itself
// at load time (same pattern as githost-mcp/ecosystem.config.js) — PM2 6.x has
// no `--env-file` flag on `pm2 start`, so that has to happen here rather than
// on the CLI. Providing BACKREST_MCP_AUTH_TOKEN (bearer) and
// BACKREST_USERNAME/PASSWORD there is sufficient; just run:
//   pm2 start ecosystem.config.js
// Sysadmin owns the deploy: confirm the port against host-forge/services.md and
// cut the scoped-mcp manifests to `type:http` (url http://127.0.0.1:8626/mcp).
// For the previous stdio launch, set BACKREST_MCP_TRANSPORT=stdio (or unset it).
"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");

function parseEnvFile(filePath) {
  const env = {};
  if (!fs.existsSync(filePath)) return env;
  for (const line of fs.readFileSync(filePath, "utf8").split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#") || !trimmed.includes("=")) continue;
    const idx = trimmed.indexOf("=");
    env[trimmed.slice(0, idx).trim()] = trimmed.slice(idx + 1).trim();
  }
  return env;
}

const HOME = os.homedir();
const sharedEnv = parseEnvFile(path.join(HOME, ".secrets", "forge.env"));

const env = {
  // Transport — HTTP for the long-lived PM2 service (loopback + bearer only).
  BACKREST_MCP_TRANSPORT: "http",
  BACKREST_MCP_HTTP_HOST: "127.0.0.1",
  BACKREST_MCP_HTTP_PORT: "8626",

  BACKREST_URL: "http://localhost:9898",
  // Safety controls — restrictive defaults, override explicitly to enable writes
  BACKREST_READONLY: "true",
  BACKREST_ALLOW_DESTRUCTIVE: "false",
  BACKREST_RESTORE_ALLOWED_PREFIX: "/tmp/backrest-restore/",
  BACKREST_AUDIT_LOG: "/home/ted/logs/backrest-mcp-audit.jsonl",
  LOG_LEVEL: "INFO",
  LOG_FILE: "/home/ted/logs/backrest-mcp.log",
};

for (const key of ["BACKREST_MCP_AUTH_TOKEN", "BACKREST_USERNAME", "BACKREST_PASSWORD"]) {
  if (sharedEnv[key]) env[key] = sharedEnv[key];
}

module.exports = {
  apps: [{
    name: "backrest-mcp",
    script: ".venv/bin/python3",
    args: ["-m", "backrest_mcp.server"],
    cwd: "/home/ted/repos/personal/backrest-mcp",
    interpreter: "none",
    autorestart: true,
    watch: false,
    env,
  }],
};
