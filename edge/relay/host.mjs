import fs from 'node:fs/promises';
import path from 'node:path';
import http from 'node:http';
import https from 'node:https';
import {fileURLToPath} from 'node:url';
import WebSocket from 'ws';
import {HttpsProxyAgent} from 'https-proxy-agent';
import {PROTOCOL, MAX_REQUEST, MAX_RESPONSE, PUBLIC_ROUTES, REQUEST_HEADERS, RESPONSE_HEADERS,
  identifier, random, sha256, fingerprint, fromBase64, toBase64, headers, exact} from './protocol.mjs';

async function ancestors(filename) {
  let current = path.resolve(filename);
  while (true) {
    const info = await fs.lstat(current);
    if (info.isSymbolicLink()) throw new Error('relay_private_path_required');
    const parent = path.dirname(current); if (parent === current) return info; current = parent;
  }
}
export async function privateJSON(filename) {
  await ancestors(filename); const info = await fs.stat(filename);
  if (!info.isFile() || info.uid !== process.getuid() || (info.mode & 0o077) || info.size > 65536) throw new Error('relay_private_file_required');
  return JSON.parse(await fs.readFile(filename, 'utf8'));
}
async function syncDirectory(directory) {
  const fd = await fs.open(directory, 'r'); try { await fd.sync(); } finally { await fd.close(); }
}
export async function saveReceipt(directory, id, value) {
  const filename = path.join(directory, id + '.json'); const temporary = path.join(directory, '.' + random());
  const fd = await fs.open(temporary, 'wx', 0o600);
  try { await fd.writeFile(JSON.stringify(value)); await fd.sync(); }
  finally { await fd.close(); }
  try { await fs.rename(temporary, filename); await syncDirectory(directory); }
  finally { await fs.unlink(temporary).catch(() => {}); }
}
export function validateHost(config) {
  const edge = new URL(config.edge_origin); const upstream = new URL(config.upstream_origin);
  if (config.format !== PROTOCOL || !identifier(config.member_id) || edge.protocol !== 'https:'
      || edge.username || edge.password || edge.search || edge.hash || edge.pathname !== '/'
      || upstream.protocol !== 'http:' || !['127.0.0.1', '[::1]'].includes(upstream.hostname)
      || upstream.username || upstream.password || upstream.search || upstream.hash || upstream.pathname !== '/'
      || !path.isAbsolute(config.token_file) || !path.isAbsolute(config.state_directory)
      || !Array.isArray(config.routes) || !config.routes.length
      || config.routes.some(route => !PUBLIC_ROUTES.has(`${route.method} ${route.path}`))
      || typeof config.upstream_host !== 'string' || !/^[A-Za-z0-9.\[\]:-]{1,253}$/.test(config.upstream_host)) {
    throw new Error('relay_host_config_invalid');
  }
  if (config.proxy_url) {
    const proxy = new URL(config.proxy_url);
    if (proxy.protocol !== 'http:' || !['127.0.0.1', '[::1]'].includes(proxy.hostname)
        || proxy.username || proxy.password || proxy.search || proxy.hash || proxy.pathname !== '/') throw new Error('relay_local_proxy_required');
  }
  return config;
}
function requestOnce(url, options, raw, maximum, timeout) {
  return new Promise((resolve, reject) => {
    const request = (url.protocol === 'https:' ? https : http).request(url, options, response => {
      const chunks = []; let length = 0;
      response.on('data', chunk => {
        length += chunk.length;
        if (length > maximum) { request.destroy(new Error('relay_response_oversized')); return; }
        chunks.push(chunk);
      });
      response.on('end', () => {
        try { resolve({status: response.statusCode,
          headers: headers(new Headers(Object.entries(response.headers).filter(([, value]) => typeof value === 'string')), RESPONSE_HEADERS),
          body: Buffer.concat(chunks)}); } catch (error) { reject(error); }
      });
      response.on('aborted', () => reject(new Error('relay_upstream_response_unknown')));
      response.on('error', reject);
    });
    const deadline = setTimeout(() => request.destroy(new Error('relay_upstream_deadline_unknown')), timeout);
    request.on('close', () => clearTimeout(deadline));
    request.on('error', reject); request.end(raw);
  });
}
export async function execute(config, frame, connection, perform = requestOnce) {
  if (!exact(frame, ['type', 'protocol', 'connection_id', 'request_id', 'fingerprint', 'method', 'path', 'headers', 'body_base64', 'deadline_ms'])
      || frame.type !== 'request' || frame.protocol !== PROTOCOL || frame.connection_id !== connection
      || !identifier(frame.request_id) || !Number.isSafeInteger(frame.deadline_ms)
      || !config.routes.some(route => route.method === frame.method && route.path === frame.path)) throw new Error('relay_frame_not_authorized');
  const raw = fromBase64(frame.body_base64, MAX_REQUEST); const selected = headers(frame.headers, REQUEST_HEADERS);
  if (await fingerprint(frame.method, frame.path, selected, raw) !== frame.fingerprint) throw new Error('relay_request_fingerprint_conflict');
  if (frame.deadline_ms <= Date.now() || frame.deadline_ms > Date.now() + 30000) throw new Error('relay_request_expired');
  // No request body or Authorization is written to the transport receipt.
  const receipt = {protocol: PROTOCOL, request_id: frame.request_id, fingerprint: frame.fingerprint,
    state: 'admitted', admitted_at: Date.now(), method: frame.method, path: frame.path};
  // Exclusive admission also fences overlapping old/new host processes. A
  // partial file after a crash remains an unknown marker, never a retry grant.
  const claim = await fs.open(path.join(config.state_directory, frame.request_id + '.json'), 'wx', 0o600);
  try { await claim.writeFile(JSON.stringify(receipt)); await claim.sync(); }
  finally { await claim.close(); }
  await syncDirectory(config.state_directory);
  try {
    const result = await perform(new URL(frame.path, config.upstream_origin),
      {method: frame.method, headers: {...selected, host: config.upstream_host}, agent: config.localAgent},
      raw, MAX_RESPONSE, Math.max(1, Math.min(25000, frame.deadline_ms - Date.now())));
    if (result.status < 200 || result.status > 599) throw new Error('relay_upstream_status_invalid');
    await saveReceipt(config.state_directory, frame.request_id, {...receipt, state: 'completed',
      status: result.status, response_sha256: await sha256(result.body)});
    return {type: 'response', protocol: PROTOCOL, request_id: frame.request_id, connection_id: connection,
      status: result.status, headers: result.headers, body_base64: toBase64(result.body), outcome: 'responded'};
  } catch {
    await saveReceipt(config.state_directory, frame.request_id, {...receipt, state: 'unknown'});
    return unknownResponse(frame.request_id, connection);
  }
}
function unknownResponse(id, connection) {
  return {type: 'response', protocol: PROTOCOL, request_id: id, connection_id: connection,
    status: 502, headers: {'content-type': 'application/json'}, outcome: 'unknown', body_base64:
      toBase64(new TextEncoder().encode(JSON.stringify({error: 'relay_original_attempt_unknown',
        request_id: id, automatic_retry: false, retry_with_new_id: false})))};
}
export async function run(filename) {
  const config = validateHost(await privateJSON(filename));
  await fs.mkdir(config.state_directory, {mode: 0o700, recursive: true}); await ancestors(config.state_directory);
  const info = await fs.stat(config.state_directory);
  if (!info.isDirectory() || info.uid !== process.getuid() || (info.mode & 0o777) !== 0o700) throw new Error('relay_private_directory_required');
  config.localAgent = new http.Agent({keepAlive: true, maxSockets: 4});
  const proxy = config.proxy_url ? new HttpsProxyAgent(config.proxy_url) : undefined;
  const tokenDocument = await privateJSON(config.token_file);
  if (!exact(tokenDocument, ['format', 'member_id', 'token']) || tokenDocument.format !== PROTOCOL
      || tokenDocument.member_id !== config.member_id || typeof tokenDocument.token !== 'string'
      || !/^[A-Za-z0-9_-]{32,256}$/.test(tokenDocument.token)) throw new Error('relay_member_token_invalid');
  let stopped = false, socket, failures = 0, wake;
  const stop = () => { stopped = true; socket?.close(1000, 'host stopping'); wake?.(); };
  process.on('SIGTERM', stop); process.on('SIGINT', stop);
  try {
    while (!stopped) {
      try {
        const ticketResponse = await requestOnce(new URL('/_mesh/relay/ticket', config.edge_origin),
          {method: 'POST', agent: proxy, headers: {'content-type': 'application/json',
            authorization: 'Bearer ' + tokenDocument.token}},
          Buffer.from(JSON.stringify({member_id: config.member_id})), 4096, 10000);
        const ticket = JSON.parse(ticketResponse.body.toString('utf8'));
        if (ticketResponse.status !== 200 || ticket.protocol !== PROTOCOL || !/^[0-9a-f]{64}$/.test(ticket.ticket)) throw new Error('relay_ticket_rejected');
        if (stopped) break;
        const target = new URL('/_mesh/relay/connect', config.edge_origin); target.protocol = 'wss:';
        await new Promise(resolve => {
          const current = new WebSocket(target, ['mesh-relay-v1', 'ticket.' + ticket.ticket],
            {agent: proxy, maxPayload: 800000, handshakeTimeout: 10000, followRedirects: false});
          socket = current;
          let connection = null, active = 0;
          const ping = setInterval(() => { if (current.readyState === WebSocket.OPEN) current.send('mesh-ping'); }, 45000);
          current.on('message', async data => {
            if (data.toString() === 'mesh-pong') return;
            let frame;
            try {
              frame = JSON.parse(data.toString());
              if (frame.type === 'ready' && frame.protocol === PROTOCOL && frame.member_id === config.member_id
                  && /^[0-9a-f]{64}$/.test(frame.connection_id) && !connection) {
                connection = frame.connection_id; failures = 0;
                console.log(JSON.stringify({protocol: PROTOCOL, state: 'connected', authority_switch: false})); return;
              }
              if (!connection || active >= 4) throw new Error('relay_frame_capacity');
              active += 1;
              try {
                const response = await execute(config, frame, connection);
                if (current.readyState === WebSocket.OPEN) current.send(JSON.stringify(response));
              } finally { active -= 1; }
            } catch {
              if (identifier(frame?.request_id) && connection && current.readyState === WebSocket.OPEN) {
                current.send(JSON.stringify(unknownResponse(frame.request_id, connection)));
              } else current.close(1008, 'invalid relay frame');
            }
          });
          current.on('error', () => current.close());
          current.on('close', () => { clearInterval(ping); resolve(); });
        });
      } catch { console.error(JSON.stringify({protocol: PROTOCOL, state: 'disconnected', details_suppressed: true})); }
      failures += 1;
      if (!stopped) await new Promise(resolve => {
        const timer = setTimeout(resolve, Math.min(60000, 1000 * 2 ** Math.min(failures, 6)));
        wake = () => { clearTimeout(timer); resolve(); };
      });
      // Reconnect the transport only. Never replay any admitted HTTP request.
    }
  } finally { config.localAgent.destroy(); proxy?.destroy(); process.off('SIGTERM', stop); process.off('SIGINT', stop); }
}
if (process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1])) {
  run(process.argv[2]).catch(() => { console.error('relay_host_unavailable'); process.exitCode = 1; });
}
