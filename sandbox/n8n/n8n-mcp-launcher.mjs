// Entry point referenced by .mcp.json.
//
// 1. Starts the Cloudflare-Access header-injecting proxy on loopback.
// 2. Spawns the real n8n-mcp server over stdio (JSON-RPC passes through
//    untouched via stdio: 'inherit'); it talks to the proxy through N8N_API_URL.
// 3. Mirrors n8n-mcp's lifecycle: forwards termination signals and exits with
//    the child's status, tearing the proxy down with it.
//
// IMPORTANT: never write to stdout here — stdout is the MCP JSON-RPC channel.
// All diagnostics go to stderr.

import { spawn } from 'node:child_process';
import { startProxy } from './n8n-cf-proxy.mjs';

const server = await startProxy();

const child = spawn('npx', ['-y', 'n8n-mcp'], {
  stdio: 'inherit',
  env: process.env,
});

const forward = (signal) => {
  try {
    child.kill(signal);
  } catch {
    /* child already gone */
  }
};
process.on('SIGTERM', () => forward('SIGTERM'));
process.on('SIGINT', () => forward('SIGINT'));

child.on('error', (err) => {
  process.stderr.write(`[n8n-mcp-launcher] failed to start n8n-mcp: ${err.message}\n`);
  server.close();
  process.exit(1);
});

child.on('exit', (code, signal) => {
  server.close();
  if (signal) {
    process.kill(process.pid, signal);
  } else {
    process.exit(code ?? 0);
  }
});
