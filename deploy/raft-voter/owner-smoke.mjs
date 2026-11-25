import assert from 'node:assert/strict';
import test from 'node:test';
import { createHash } from 'node:crypto';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { OWNER_TRANSFER } from './codec.mjs';
import { VoterDriver, flusher } from './driver.mjs';
import { authenticatedForward, handle } from './http.mjs';
import { loadNodeVoter, pins } from './loader-node.mjs';
import { FileStore, sqliteStore } from './storage-node.mjs';

// Independent producer for the public canonical JSON checkpoint contract.
function checkpointDigest(value) {
  const keys = new Set();
  JSON.stringify(value, (key, child) => { keys.add(key); return child; });
  return createHash('sha256').update(JSON.stringify(value, [...keys].sort())).digest('hex');
}
const group = 'owner-smoke', scope = 'business-fixture';
const digit = n => n.toString(16).repeat(64);
function transfer(operation_id, previous, source, next_owner) {
  const checkpoint = { authority: 'business', incarnation: 'original-database', generation: 2,
    manifest_sha256: digit(1), sealed_journal_head: 510, typed_rows_sha256: digit(2),
    external_dependencies_manifest_sha256: digit(3), worker_claim_protocol: 'amita-worker-claim/1',
    source_holder: source, source_seal_sha256: digit(4),
    holder_receipts: [source, next_owner].sort().map((holder, i) => ({ holder, sha256: digit(5 + i) })),
    retained_effects_sha256: digit(7) };
  return { format: OWNER_TRANSFER, operation_id, group, scope, role: 'replica_writer', previous,
    next_owner, next_epoch: previous.epoch + 1, checkpoint, checkpoint_sha256: checkpointDigest(checkpoint) };
}

test('controlled owner transfer survives interruption at durable apply', async t => {
  process.umask(0o077);
  const artifact = process.env.AMITA_RAFT_ARTIFACT ?? join(dirname(fileURLToPath(import.meta.url)), 'artifact');
  const Voter = await loadNodeVoter(artifact);
  const directory = mkdtempSync(join(tmpdir(), 'amita-owner-'));
  const members = [1, 2], drivers = new Map(), flushers = new Map();
  const operatorKey = digit(10), ownerKey = digit(11);
  let now = 1800000000000, interrupt = false, interrupted = false;
  async function open(id) {
    const peers = [1, 2, 3].filter(other => other !== id);
    const config = { group, scope, member: id, operator_key: operatorKey, owner_command_key: ownerKey,
      links: Object.fromEntries(peers.map(peer => [peer, [id, peer].sort().join('').padEnd(64, '0')])),
      peers: Object.fromEntries(peers.map(peer => [peer, `https://peer-${peer}.invalid/raft/peer`])),
      tick_ms: 5000, start_enabled: true };
    const path = join(directory, String(id));
    const store = id === 1 ? new FileStore(path) : await sqliteStore(path);
    const save = store.save.bind(store);
    store.save = async image => {
      if (id === 2 && interrupt && image.receipts['transfer-winner']) {
        interrupt = false; interrupted = true;
        throw new Error('interrupted-before-durable-apply');
      }
      await save(image);
    };
    const driver = new VoterDriver({ Voter, config, store, clock: () => now });
    drivers.set(id, driver);
    await driver.start();
    flushers.set(id, flusher(driver, row => {
      const remote = drivers.get(row.to);
      if (!remote || remote.dead) throw new Error('peer offline');
      return remote.receive(row);
    }));
  }
  t.after(async () => {
    for (const driver of drivers.values()) await driver.close();
    rmSync(directory, { recursive: true, force: true });
  });
  async function pump() {
    for (let i = 0; i < 100; i++) {
      let count = 0;
      for (const id of members) if (!drivers.get(id).dead) count += await flushers.get(id)();
      if (!count) return;
    }
    assert.fail('peer delivery did not quiesce');
  }
  async function manage(member, action, fields = {}, key = ownerKey) {
    const driver = drivers.get(member);
    const request = new Request('https://fixture.invalid/raft/manage', { method: 'POST',
      headers: { authorization: 'Bearer ' + key, 'content-type': 'application/json' },
      body: JSON.stringify({ group, scope, member, action, ...fields }) });
    return handle(driver, await authenticatedForward(driver.config, request, () => now));
  }
  for (const id of members) await open(id);
  for (let i = 0; i < 50 && drivers.get(1).status().leader_hint !== 1; i++) {
    now += 5000; await drivers.get(1).tick(); await pump();
  }
  assert.equal(drivers.get(1).status().leader_hint, 1);
  const legacy = { format: 'amita-quorum-digest/1', operation_id: 'opaque', digest: digit(8) };
  await manage(1, 'propose', { proposal: legacy }, operatorKey); await pump();
  assert.equal((await manage(2, 'receipt', { operation_id: 'opaque' })).receipt.digest, legacy.digest);
  assert.equal((await manage(2, 'owner')).current, null);

  const genesis = { owner: null, epoch: 0, head: { index: 0, sha256: digit(0) } };
  const seed = transfer('first-controlled-transfer', genesis, 'vps', 'laptop');
  await assert.rejects(manage(1, 'propose', { proposal: seed }, operatorKey), /owner_command_key_required/);
  await assert.rejects(manage(1, 'propose', { proposal: { ...seed, scope: 'foreign' } }), /owner_group_scope_binding/);
  await assert.rejects(manage(1, 'propose', { proposal: { ...seed, checkpoint_sha256: digit(9) } }), /sealed_checkpoint_digest/);
  await manage(1, 'propose', { proposal: seed }); await pump();
  const seedReceipt = (await manage(2, 'receipt', { operation_id: seed.operation_id })).receipt;
  assert.equal(seedReceipt.status, 'applied');
  const winner = transfer('transfer-winner', seedReceipt.grant, 'laptop', 'vps');
  const conflict = transfer('transfer-conflict', seedReceipt.grant, 'laptop', 'alternate');
  interrupt = true;
  for (const proposal of [winner, conflict, winner]) {
    assert.equal((await manage(1, 'propose', { proposal })).status, 'submitted_not_commit_receipt');
  }
  await pump();
  assert.equal(interrupted, true);
  assert.equal(drivers.get(2).status().core_closed, true);
  assert.deepEqual((await manage(2, 'owner')).current.head, seedReceipt.grant.head);
  assert.equal((await manage(2, 'receipt', { operation_id: winner.operation_id })).status, 'unknown_here');
  await drivers.get(2).close();
  await open(2);
  await pump();
  const committed = (await manage(1, 'receipt', { operation_id: winner.operation_id })).receipt;
  assert.equal(committed.status, 'applied');
  assert.equal(committed.grant.epoch, 2);
  for (const id of members) {
    const status = await manage(id, 'status');
    assert.equal(status.business_fencing, false);
    assert.equal(status.ha_qualified, false);
    assert.equal(status.owner_read_consistency, 'local_durable_applied');
    assert.equal(status.replica_writer.owner, winner.next_owner);
    assert.deepEqual(status.replica_writer.checkpoint, winner.checkpoint);
    assert.deepEqual(status.replica_writer.head, committed.grant.head);
    assert.deepEqual(status.receipts[seed.operation_id], seedReceipt);
    assert.deepEqual(status.receipts[winner.operation_id], committed);
    const rejected = status.receipts[conflict.operation_id];
    assert.equal(rejected.status, 'rejected');
    assert.equal(rejected.reason, 'owner_predecessor_conflict');
    assert.deepEqual(await manage(id, 'propose', { proposal: conflict }), rejected);
    assert.deepEqual(await manage(id, 'propose', { proposal: winner }), committed);
    await assert.rejects(manage(id, 'propose', { proposal: { ...conflict, operation_id: winner.operation_id } }),
      /original_operation_digest_conflict/);
  }
  await drivers.get(2).close();
  await open(2);
  assert.deepEqual((await manage(2, 'receipt', { operation_id: winner.operation_id })).receipt, committed);
  const local = drivers.get(2);
  const incomplete = await local.store.read();
  delete incomplete.replica_writer;
  await local.store.save(incomplete);
  await local.close();
  await assert.rejects(open(2), /original_applied_owner_state_required/);
  t.diagnostic('WASM sha256 ' + pins.files['wasm/amita_raft_portability_bg.wasm']);
});
