// Zero-dependency local reverse proxy that injects Cloudflare Access
// service-token headers, so n8n-mcp (which cannot send custom headers) can
// reach an n8n instance protected by Cloudflare Zero Trust.
//
// Listens on loopback and forwards every request to N8N_CF_UPSTREAM, adding
// CF-Access-Client-Id / CF-Access-Client-Secret. Secrets come from the
// environment (resolved from 1Password by `op run`), never from disk.
//
// Run standalone (for testing):  node n8n-cf-proxy.mjs
// Or import { startProxy } from the launcher.

import http from 'node:http';
import https from 'node:https';

// Hop-by-hop headers must not be forwarded to the upstream.
const HOP_BY_HOP = ['connection', 'proxy-connection', 'keep-alive', 'upgrade', 'te', 'trailer'];

export function startProxy({
  upstream = process.env.N8N_CF_UPSTREAM,
  port = Number(process.env.N8N_CF_LISTEN_PORT || 5680),
  host = process.env.N8N_CF_LISTEN_HOST || '127.0.0.1',
  cfId = process.env.CF_ACCESS_CLIENT_ID,
  cfSecret = process.env.CF_ACCESS_CLIENT_SECRET,
} = {}) {
  if (!upstream) throw new Error('N8N_CF_UPSTREAM is required (e.g. https://n8n.timetravels.com)');
  if (!cfId || !cfSecret) {
    throw new Error(
      'CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET must be set. ' +
      'Did you launch via `op run --env-file=.env -- ...` so 1Password resolved them?'
    );
  }

  const upstreamUrl = new URL(upstream);
  const isHttps = upstreamUrl.protocol === 'https:';
  const transport = isHttps ? https : http;
  const upstreamPort = upstreamUrl.port || (isHttps ? 443 : 80);

  const server = http.createServer((req, res) => {
    const headers = { ...req.headers };
    for (const h of HOP_BY_HOP) delete headers[h];
    headers['host'] = upstreamUrl.host;
    headers['cf-access-client-id'] = cfId;
    headers['cf-access-client-secret'] = cfSecret;

    const upReq = transport.request(
      {
        protocol: upstreamUrl.protocol,
        hostname: upstreamUrl.hostname,
        port: upstreamPort,
        method: req.method,
        path: req.url,
        headers,
      },
      (upRes) => {
        res.writeHead(upRes.statusCode || 502, upRes.headers);
        upRes.pipe(res);
      }
    );

    upReq.on('error', (err) => {
      process.stderr.write(`[n8n-cf-proxy] upstream error: ${err.message}\n`);
      if (!res.headersSent) res.writeHead(502, { 'content-type': 'text/plain' });
      res.end(`n8n-cf-proxy upstream error: ${err.message}`);
    });

    req.pipe(upReq);
  });

  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(port, host, () => {
      process.stderr.write(
        `[n8n-cf-proxy] listening on http://${host}:${port} -> ${upstreamUrl.origin} (CF Access headers injected)\n`
      );
      resolve(server);
    });
  });
}

// Standalone execution (handy for testing the proxy on its own).
if (import.meta.url === `file://${process.argv[1]}`) {
  startProxy().catch((err) => {
    process.stderr.write(`[n8n-cf-proxy] fatal: ${err.message}\n`);
    process.exit(1);
  });
}
