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
// Secrets are NOT hardcoded here. Inject them at start time, e.g.:
//   pm2 start ecosystem.config.js --env-file ~/.secrets/forge.env
// providing BACKREST_MCP_AUTH_TOKEN (bearer) and BACKREST_USERNAME/PASSWORD.
// Sysadmin owns the deploy: confirm the port against host-forge/services.md and
// cut the scoped-mcp manifests to `type:http` (url http://127.0.0.1:8626/mcp).
// For the previous stdio launch, set BACKREST_MCP_TRANSPORT=stdio (or unset it).
module.exports = {
  apps: [{
    name: 'backrest-mcp',
    script: 'python3',
    args: ['-m', 'backrest_mcp.server'],
    cwd: '/home/ted/repos/personal/backrest-mcp',
    interpreter: 'none',
    autorestart: true,
    watch: false,
    env: {
      // Transport — HTTP for the long-lived PM2 service (loopback + bearer only).
      BACKREST_MCP_TRANSPORT: 'http',
      BACKREST_MCP_HTTP_HOST: '127.0.0.1',
      BACKREST_MCP_HTTP_PORT: '8626',
      // BACKREST_MCP_AUTH_TOKEN injected via --env-file ~/.secrets/forge.env

      BACKREST_URL: 'http://localhost:9898',
      // Safety controls — restrictive defaults, override explicitly to enable writes
      BACKREST_READONLY: 'true',
      BACKREST_ALLOW_DESTRUCTIVE: 'false',
      BACKREST_RESTORE_ALLOWED_PREFIX: '/tmp/backrest-restore/',
      BACKREST_AUDIT_LOG: '/home/ted/logs/backrest-mcp-audit.jsonl',
      LOG_LEVEL: 'INFO',
      LOG_FILE: '/home/ted/logs/backrest-mcp.log',
      // BACKREST_USERNAME, BACKREST_PASSWORD injected via:
      // pm2 start ecosystem.config.js --env-file ~/.secrets/forge.env
    },
  }],
};
