// One bounded acceptance, using the ACTUAL pinned upstream WASM and two real
// Node storage backends. In-memory transport simulates a partition only; it is
// not a second consensus implementation or Cloudflare execution claim.
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { FileStore, sqliteStore } from './storage-node.mjs';
import { loadNodeVoter, pins } from './loader-node.mjs';
import { VoterDriver, flusher } from './driver.mjs';
import { authenticatedForward, boundedJSON } from './http.mjs';

process.umask(0o077);
const artifact = process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), 'artifact');
const Voter = await loadNodeVoter(artifact), directory = mkdtempSync(join(tmpdir(), 'amita-three-voter-'));
let now = 1800000000000, connected = new Set([1, 2, 3]);
const drivers = new Map(), flushers = new Map();
let ingressExercised = false;
// Explicit synthetic test fixtures, never runtime defaults or production keys.
const pairKey = (from, to) => [Math.min(from, to), Math.max(from, to)].join('').padEnd(64, 'a');
const config = id => ({ group: 'synthetic-host-smoke', scope: 'digest-only-smoke', member: id,
  links: Object.fromEntries([1, 2, 3].filter(peer => peer !== id).map(peer => [peer, pairKey(id, peer)])),
  peers: Object.fromEntries([1, 2, 3].filter(peer => peer !== id).map(peer => [peer, `https://member-${peer}.invalid/raft/peer`])),
  operator_key: null, tick_ms: 5000, start_enabled: true });
async function open(id) {
  const store = id === 3 ? await sqliteStore(join(directory, String(id))) : new FileStore(join(directory, String(id)));
  const driver = new VoterDriver({ Voter, store, config: config(id), clock: () => now });
  await driver.start(); drivers.set(id, driver);
  flushers.set(id, flusher(driver, async row => {
    if (!connected.has(id) || !connected.has(row.to)) throw new Error('simulated partition');
    if (!ingressExercised) {
      const peerConfig = config(row.to);
      const peerRequest = value => new Request('https://voter.invalid/raft/peer', {
        method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(value)
      });
      await assert.rejects(authenticatedForward(peerConfig, peerRequest({ ...row, mac: 'AAAA' })),
        /peer_authentication/);
      const forwarded = await authenticatedForward(peerConfig, peerRequest(row));
      assert.deepEqual(await boundedJSON(forwarded), row);
      const management = new Request('https://voter.invalid/raft/manage', {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ group: peerConfig.group, scope: peerConfig.scope, member: row.to, action: 'enable' })
      });
      await assert.rejects(authenticatedForward(peerConfig, management), /voter_ingress_not_authorized/);
      assert.equal(management.bodyUsed, false);
      ingressExercised = true;
    }
    return drivers.get(row.to).receive(row);
  }));
}
async function pump() {
  for (let pass = 0; pass < 128; pass++) {
    let delivered = 0;
    for (const id of [1, 2, 3]) delivered += await flushers.get(id)();
    if (!delivered) return;
  }
  assert.fail('bounded transport failed to quiesce');
}
function leader(ids) {
  return ids.find(id => drivers.get(id).status().leader_hint === id &&
    ids.every(peer => drivers.get(peer).status().leader_hint === id));
}
async function elect(ids) {
  for (let round = 0; round < 100; round++) {
    now += 5000;
    for (const id of ids) { await drivers.get(id).tick(); await pump(); }
    const selected = leader(ids); if (selected) return selected;
  }
  assert.fail('actual upstream voters did not elect within bounded ticks');
}
const proposal = (operation_id, digit) => ({ format: 'amita-quorum-digest/1', operation_id, digest: digit.repeat(64) });
try {
  for (const id of [1, 2, 3]) await open(id);
  const firstLeader = await elect([1, 2, 3]);
  assert.equal(ingressExercised, true);
  await drivers.get(firstLeader).propose(proposal('original-majority-1', '1')); await pump();
  for (const driver of drivers.values()) assert.equal(driver.status().receipts['original-majority-1']?.digest, '1'.repeat(64));

  const follower = [1, 2, 3].find(id => id !== firstLeader);
  const before = drivers.get(follower).status();
  await drivers.get(follower).close(); await open(follower);
  assert.deepEqual(drivers.get(follower).status().receipts, before.receipts);
  assert.equal(drivers.get(follower).status().applied, before.applied);

  connected = new Set([firstLeader]);
  await drivers.get(firstLeader).propose(proposal('original-isolated-proposal', '2'));
  for (let round = 0; round < 24; round++) { now += 5000; await drivers.get(firstLeader).tick(); await pump(); }
  for (const driver of drivers.values()) assert.equal(driver.status().receipts['original-isolated-proposal'], undefined,
    'a minority must not invent a committed receipt');

  const majority = [1, 2, 3].filter(id => id !== firstLeader); connected = new Set(majority);
  const secondLeader = await elect(majority);
  await drivers.get(secondLeader).propose(proposal('original-majority-2', '3')); await pump();
  for (const id of majority) assert.equal(drivers.get(id).status().receipts['original-majority-2']?.digest, '3'.repeat(64));

  connected = new Set([1, 2, 3]); await pump();
  for (let round = 0; round < 4; round++) { now += 5000; for (const driver of drivers.values()) await driver.tick(); await pump(); }
  for (const driver of drivers.values()) {
    assert.equal(driver.status().receipts['original-majority-2']?.digest, '3'.repeat(64));
    assert.equal(driver.status().receipts['original-isolated-proposal'], undefined);
  }
  // Existing durable peer sequence and MAC bindings reject cross-group replay
  // without changing the original state or acknowledging a replacement packet.
  const victim = drivers.get(1), original = victim.status();
  await assert.rejects(victim.receive({ format: 'amita-raft-peer/1', group: 'foreign', scope: config(1).scope,
    from: 2, to: 1, sequence: 1, wire: '', mac: '' }), /peer_binding/);
  assert.deepEqual(victim.status(), original);
  console.log(JSON.stringify({ format: 'amita-three-voter-host-smoke/1', status: 'passed',
    wasm_sha256: pins.files['wasm/amita_raft_portability_bg.wasm'], first_leader: firstLeader,
    majority_replacement_leader: secondLeader, restored_member: follower,
    backends: ['atomic-fsync-file', 'SQLite-WAL-FULL'], checks: ['actual-election', 'digest-proposal-commit',
      'restart-preserves-applied-receipts', 'minority-does-not-commit', 'majority-replacement', 'heal-original-log', 'foreign-group-denied'],
    cloudflare_runtime_tested: false, business_fencing: false, ha_qualified: false }));
} finally {
  for (const driver of drivers.values()) await driver.close();
  rmSync(directory, { recursive: true, force: true });
}
