import http from 'node:http';
import { existsSync, lstatSync, readFileSync, chmodSync, unlinkSync } from 'node:fs';
import { isAbsolute } from 'node:path';
import { need } from './codec.mjs';
import { VoterDriver, flusher, pollExchanges } from './driver.mjs';
import { FileStore, sqliteStore } from './storage-node.mjs';
import { loadNodeVoter } from './loader-node.mjs';
import { handle, transport } from './http.mjs';

process.umask(0o077);
const configPath = process.argv[2]; need(configPath && isAbsolute(configPath), 'private_config_argument');
const stat = lstatSync(configPath);
need(stat.isFile() && !stat.isSymbolicLink() && stat.uid === process.getuid() && (stat.mode & 0o077) === 0 && stat.size <= 16384, 'private_config');
const local = JSON.parse(readFileSync(configPath, 'utf8'));
need(local.storage === 'file' || local.storage === 'sqlite', 'storage_selection');
need(local.listen && (local.listen.socket && isAbsolute(local.listen.socket) ||
  local.listen.host === '127.0.0.1' && Number.isInteger(local.listen.port) && local.listen.port > 1023 && local.listen.port < 65536),
'loopback_or_private_uds_only');
if (local.listen.socket) need(!existsSync(local.listen.socket), 'original_socket_exists_do_not_replace');
const store = local.storage === 'sqlite' ? await sqliteStore(local.state_directory) : new FileStore(local.state_directory);
const driver = new VoterDriver({ Voter: await loadNodeVoter(local.artifact_directory), store, config: local.voter });
await driver.start();
const send = transport(driver.config), flush = flusher(driver, send);
let timer = null, stopping = false;
const laterFlush = () => { void flush().catch(error => console.error(String(error))); };
const server = http.createServer(async (incoming, outgoing) => {
  try {
    let length = 0; const chunks = [];
    for await (const chunk of incoming) { length += chunk.length; need(length <= 49152, 'http_body_budget'); chunks.push(chunk); }
    const request = new Request(`http://127.0.0.1${incoming.url}`, { method: incoming.method,
      headers: incoming.headers, body: Buffer.concat(chunks), duplex: 'half' });
    const result = await handle(driver, request);
    outgoing.writeHead(200, { 'content-type': 'application/json', 'cache-control': 'no-store' }); outgoing.end(JSON.stringify(result));
    setImmediate(laterFlush); // never await a reciprocal peer while holding HTTP ACK
  } catch (error) {
    outgoing.writeHead(409, { 'content-type': 'application/json', 'cache-control': 'no-store' });
    outgoing.end(JSON.stringify({ ok: false, error: String(error), business_fencing: false }));
  }
});
server.requestTimeout = 5000; server.headersTimeout = 5000; server.maxConnections = 16;
const options = local.listen.socket ? { path: local.listen.socket } : { host: '127.0.0.1', port: local.listen.port };
await new Promise((resolve, reject) => { server.once('error', reject); server.listen(options, resolve); });
if (local.listen.socket) chmodSync(local.listen.socket, 0o600);
async function tick() {
  if (stopping) return;
  const expiredAtStart = driver.expired(), due = Date.now() + driver.config.tick_ms;
  try {
    await driver.tick();
    await Promise.all([flush(), pollExchanges(driver, send)]);
  } catch (error) { console.error(String(error)); }
  if (!stopping && !expiredAtStart) schedule(due);
}
function schedule(due) {
  const next = Math.min(due, driver.config.qualification_until_ms ?? Infinity);
  timer = setTimeout(tick, Math.max(0, next - Date.now()));
}
if (!driver.expired()) schedule(Date.now() + driver.config.tick_ms);
laterFlush();
console.log(JSON.stringify({ format: 'amita-raft-node-listening/1', member: driver.config.member,
  group: driver.config.group, listen: options, business_fencing: false }));
async function stop() {
  if (stopping) return; stopping = true; clearTimeout(timer);
  server.close(); await driver.close();
  if (local.listen.socket && existsSync(local.listen.socket) && lstatSync(local.listen.socket).isSocket()) unlinkSync(local.listen.socket);
}
process.once('SIGTERM', () => { void stop(); }); process.once('SIGINT', () => { void stop(); });
