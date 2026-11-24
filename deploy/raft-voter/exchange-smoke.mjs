// Real pinned WASM with file/SQLite persistence. Fetch routes requests in
// memory through the public Worker verifier and the shared DO request handler.
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { FileStore, sqliteStore } from './storage-node.mjs';
import { loadNodeVoter, pins } from './loader-node.mjs';
import { VoterDriver, flusher, pollExchanges } from './driver.mjs';
import { authenticatedForward, handle, transport } from './http.mjs';

process.umask(0o077);
const Voter = await loadNodeVoter(process.argv[2]), dir = mkdtempSync(join(tmpdir(), 'amita-raft-exchange-'));
let now = Date.now(), calls = 0, replayed = false, restarted = false, lost = null;
const cutoff = now + 60 * 60 * 1000;
const originalFetch = globalThis.fetch, drivers = new Map(), flush = new Map();
const config = member => {
  const others = [1, 2, 3].filter(id => id !== member);
  return { group: 'synthetic-exchange', scope: 'digest-only', member, operator_key: null, start_enabled: false,
    tick_ms: 5000, qualification_until_ms: cutoff, links: Object.fromEntries(others.map(id => [id,
      [Math.min(member, id), Math.max(member, id)].join('').padEnd(64, 'a')])),
    peers: Object.fromEntries(others.map(id => [id, member === 3 ? 'accept_exchange' :
      id === 3 ? 'https://edge.invalid/raft/exchange' : `https://holder-${id}.invalid/raft/peer`])) };
};
async function open(id) {
  const store = id === 3 ? await sqliteStore(join(dir, String(id))) : new FileStore(join(dir, String(id)));
  const driver = new VoterDriver({ Voter, config: config(id), store, clock: () => now });
  await driver.start(); drivers.set(id, driver);
  flush.set(id, flusher(driver, transport(driver.config)));
}
const edgeRequest = value => new Request('https://edge.invalid/raft/exchange', { method: 'POST',
  headers: { 'content-type': 'application/json' }, body: JSON.stringify(value) });
async function forward(value) {
  const request = await authenticatedForward(config(3), edgeRequest(value), () => now);
  calls++;
  return handle(drivers.get(3), request);
}
let invalidEgress = false;
globalThis.fetch = async (url, options) => {
  const request = JSON.parse(options.body);
  if (request.from === 3 || (request.to === 3 && url !== 'https://edge.invalid/raft/exchange')) {
    invalidEgress = true; throw new Error('unexpected push across outbound-only link');
  }
  if (request.to !== 3) throw new Error('synthetic holder-to-holder partition');
  const response = await forward(request);
  if (request.from === 1 && !lost && request.packet && response.packet) {
    lost = { request, response };
    throw new Error('synthetic response loss after durable receipt');
  }
  if (lost && !replayed && request.from === 1) {
    assert.deepEqual(request.packet, lost.request.packet);
    assert.deepEqual(request.ack, lost.request.ack);
    assert.deepEqual(response, lost.response);
    replayed = true;
  }
  return Response.json(response);
};
const poll = id => pollExchanges(drivers.get(id), transport(config(id)));
async function round() {
  now += 5000;
  for (const id of [1, 3]) await drivers.get(id).tick({ alarmWrite: id === 3 });
  await poll(1); await flush.get(1)(); await flush.get(3)();
  if (replayed && !restarted) {
    // The returned packet was accepted durably. Its ACK must survive a crash
    // before its next exchange, using the identical sequence and pair MAC.
    await drivers.get(1).close(); await open(1);
    const successor = await drivers.get(1).prepareExchange(3);
    assert.equal(successor.ack.sequence, lost.response.packet.sequence);
    await forward(successor);
    const stale = await forward(lost.request);
    assert.deepEqual(stale.ack, lost.response.ack);
    assert.equal(stale.packet, null);
    restarted = true;
  }
}
try {
  for (const id of [1, 2, 3]) await open(id);
  await poll(1); assert.equal(calls, 0);
  for (const driver of drivers.values()) await driver.enable(true);
  const empty = await drivers.get(1).prepareExchange(3);
  await assert.rejects(forward({ ...empty, group: 'foreign' }), /exchange_binding/);
  await assert.rejects(forward({ ...empty, expires_ms: now }), /exchange_expiry/);
  await assert.rejects(forward({ ...empty, expires_ms: now + 60000 }), /exchange_expiry/);
  await assert.rejects(forward({ ...empty, from: 2 }), /exchange_authentication/);
  const disabledRoute = edgeRequest(empty);
  await assert.rejects(authenticatedForward(config(1), disabledRoute), /voter_ingress_not_authorized/);
  assert.equal(disabledRoute.bodyUsed, false); assert.equal(calls, 0);
  await poll(1); await poll(2); assert.equal(calls, 2);
  await drivers.get(2).enable(false);
  let leader;
  for (let i = 0; i < 120; i++) {
    await round();
    leader = drivers.get(1).status().leader_hint;
    if (restarted && [1, 3].includes(leader) && drivers.get(3).status().leader_hint === leader) break;
    leader = null;
  }
  assert.ok(restarted && leader, 'holder and edge must elect through a retried exchange');
  const operation = 'original-exchange-majority', digest = 'a'.repeat(64);
  await drivers.get(leader).propose({ format: 'amita-quorum-digest/1', operation_id: operation, digest });
  for (let i = 0; i < 80 && [1, 3].some(id => !drivers.get(id).status().receipts[operation]); i++) await round();
  for (const id of [1, 3]) assert.equal(drivers.get(id).status().receipts[operation]?.digest, digest);
  assert.equal(drivers.get(2).status().receipts[operation], undefined); assert.equal(invalidEgress, false);
  now = cutoff;
  const callsBeforeCutoff = calls;
  await assert.rejects(forward(empty), /qualification_expired/);
  for (const id of [1, 3]) {
    const driver = drivers.get(id);
    assert.equal((await driver.tick()).enabled, false);
    await assert.rejects(driver.enable(true), /qualification_expired/);
    await flush.get(id)();
  }
  await poll(1); assert.equal(calls, callsBeforeCutoff);
  await drivers.get(3).close(); await open(3);
  assert.equal(drivers.get(3).status().enabled, false);
  assert.equal(drivers.get(3).status().receipts[operation].digest, digest);
  console.log(JSON.stringify({ format: 'amita-raft-exchange-smoke/1', status: 'passed',
    wasm_sha256: pins.files['wasm/amita_raft_portability_bg.wasm'], leader, exchanges: calls,
    durable_receipt_members: [1, 3], response_loss_retried: replayed, holder_restarted: restarted,
    stopped_at_deadline: true, transport: 'in-memory HTTP', cloudflare_runtime_tested: false, business_fencing: false }));
} finally {
  globalThis.fetch = originalFetch;
  for (const driver of drivers.values()) await driver.close();
  rmSync(dir, { recursive: true, force: true });
}
