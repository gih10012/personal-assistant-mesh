import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { registerHooks } from 'node:module';
import test from 'node:test';

// ArkTS uses extensionless local imports. Resolve only this core's local module;
// production code is loaded unchanged by Node's native TypeScript support.
registerHooks({ resolve(specifier, context, next) {
  return next(specifier === './Protocol' ? './Protocol.ts' : specifier, context);
} });
const { WorkEngine, coldResumeWork, freshWorkSnapshot, validateWorkConfig } =
  await import('../entry/src/main/ets/core/WorkProtocol.ts');
const clone = value => structuredClone(value);
const blob = value => ({ bytes: Buffer.byteLength(value),
  sha256: createHash('sha256').update(value).digest('hex'), base64: Buffer.from(value).toString('base64') });

function fixture() {
  let sequence = 0, invocations = 0, allowed = true, grant = 'grant-A';
  let failSaveAt = '', loseAckAt = '', revokeAt = '', serverCalls = [];
  let durable, resultReceipt;
  const nodeId = 'synthetic-phone';
  const config = { format: 'mesh-phone-client/1', resource_url: 'https://mesh.example.test/phone/node',
    node_id: nodeId, session_id: 'original-session', mesh_id: 'synthetic-mesh', authority_id: 'synthetic-authority' };
  const selection = { capability_id: nodeId + ':bytes-v1', epoch: 1, action: 'challenge.bytes.repeat',
    scope: { task: 'original-only' }, workload: { input: blob('abc'), repeat: 3 } };
  const pending = { operation_id: 'original-op', capability_id: selection.capability_id, provider: nodeId,
    state: 'pending', epoch: 1, receipt_id: null, selection, task_id: 'original-task', task_epoch: 7, deadline: 1060 };
  let remote = clone(pending);
  const controls = [];
  const snapshot = freshWorkSnapshot(nodeId); snapshot.config = config;
  const base = value => ({ format: 'mesh-interactive-node/1', mesh_id: config.mesh_id,
    execution_verified: false, native_tools_restricted: false, ...value });
  const host = {
    async save(value) {
      if (value.records.some(row => row.state === failSaveAt)) { throw new Error('synthetic-disk-failure'); }
      durable = clone(value);
    },
    id: () => 'id-' + ++sequence,
    now: () => 1000,
    permission: () => grant,
    allowed: (_, context) => allowed && (context === undefined || context === grant),
    async execute() {
      assert.equal(durable.records[0].state, 'attempted');
      assert.equal(durable.records[0].task_epoch, 7);
      assert.equal(durable.records[0].operation_id, 'original-op');
      invocations++; return blob('abcabcabc');
    },
    async exchange(action, args) {
      serverCalls.push({ action, args: clone(args) });
      if (action === 'mesh_connect' || action === 'mesh_node_update') {
        controls.push(clone(args));
        return base({ session: { node_id: nodeId, session_id: config.session_id,
          network: { state: args.online ? 'online' : 'offline', expires_at: 1030, presence_ttl_seconds: 30 } } });
      }
      if (action === 'mesh_discover') { return base({}); }
      if (action === 'mesh_work_pull') { return base({ dispatch: [clone(pending)] }); }
      assert.equal(args.session_id, config.session_id);
      const { action: providerAction, arguments: request } = args.provider;
      assert.equal(request.operation_id, pending.operation_id);
      assert.equal(request.capability_id, pending.capability_id);
      let response;
      if (providerAction === 'inspect') {
        response = { operation_id: pending.operation_id, task_id: pending.task_id, task_epoch: pending.task_epoch,
          plan: { selections: [clone(selection)] }, dispatch: [clone(remote)] };
      } else if (providerAction === 'result') {
        assert.deepEqual(request.result, blob('abcabcabc'));
        resultReceipt = { result_reference: 'phone-result:' + request.result.sha256,
          sha256: request.result.sha256, bytes: request.result.bytes };
        response = clone(resultReceipt);
      } else {
        assert.equal(request.epoch, remote.epoch);
        remote.epoch++;
        if (providerAction === 'accept') { remote.state = 'accepted'; remote.receipt_id = request.receipt_id; }
        if (providerAction === 'start') { remote.state = 'running'; }
        if (providerAction === 'unknown') { remote.state = 'unknown'; }
        if (providerAction === 'settle') {
          assert.ok(resultReceipt, 'full bytes must be durably held by authority before settlement');
          assert.equal(request.evidence.result_reference, resultReceipt.result_reference);
          remote.state = 'completed'; remote.settlement = { outcome: request.outcome, evidence: clone(request.evidence) };
        }
        response = { ...clone(remote), execute_once: providerAction === 'start' };
      }
      if (revokeAt === providerAction) { allowed = false; }
      if (loseAckAt === providerAction) { loseAckAt = ''; throw new Error('synthetic-lost-ACK'); }
      return base({ provider: response });
    }
  };
  let engine = new WorkEngine(snapshot, host);
  return { get engine() { return engine; }, host, pending, controls, config,
    get remote() { return remote; }, get calls() { return serverCalls; }, get invocations() { return invocations; },
    get durable() { return durable; },
    failSaveAt: value => { failSaveAt = value; }, loseAckAt: value => { loseAckAt = value; },
    revokeAt: value => { revokeAt = value; }, setAllowed: value => { allowed = value; },
    grant: value => { grant = value; },
    cold() { const saved = clone(durable); coldResumeWork(saved, nodeId); engine = new WorkEngine(saved, host); },
    mutateRemote: change => { change(remote); }
  };
}

test('foreground connects/discovers/updates without a task, then original allocation delivers complete bytes and settles once', async () => {
  const f = fixture();
  await f.engine.connect([f.pending.capability_id]); await f.engine.discover();
  await f.engine.control(true, [f.pending.capability_id]);
  assert.equal(f.engine.snapshot.records.length, 0);
  const [dispatch] = await f.engine.pull(); const row = await f.engine.run(dispatch);
  assert.equal(row.state, 'settled'); assert.equal(f.invocations, 1);
  assert.equal(row.task_id, 'original-task'); assert.equal(row.task_epoch, 7);
  assert.equal(f.remote.operation_id, 'original-op'); assert.equal(f.remote.state, 'completed');
  f.cold(); await f.engine.reconcile(f.engine.snapshot.records[0]);
  assert.equal(f.invocations, 1); assert.equal(f.engine.snapshot.records[0].state, 'settled');
});

test('start ACK loss survives cold resume as original unknown and never invokes native adapter', async () => {
  const f = fixture(); f.loseAckAt('start'); const row = await f.engine.run(f.pending);
  assert.equal(row.state, 'unknown'); assert.equal(f.invocations, 0); assert.equal(f.remote.state, 'running');
  f.cold(); await f.engine.run(f.pending);
  assert.equal(f.remote.state, 'unknown'); assert.equal(f.invocations, 0);
  assert.equal(f.engine.snapshot.records[0].receipt_id, row.receipt_id);
});

test('durable attempted write failure prevents even a native read', async () => {
  const f = fixture(); f.failSaveAt('attempted'); const row = await f.engine.run(f.pending);
  assert.equal(row.state, 'unknown'); assert.equal(f.invocations, 0);
  f.cold(); await f.engine.reconcile(f.engine.snapshot.records[0]); assert.equal(f.invocations, 0);
});

test('result and settlement ACK losses reconcile original bytes without reexecution or changed epoch report', async () => {
  for (const stage of ['result', 'settle']) {
    const f = fixture(); f.loseAckAt(stage); await f.engine.run(f.pending); f.cold();
    await f.engine.reconcile(f.engine.snapshot.records[0]);
    assert.equal(f.engine.snapshot.records[0].state, 'settled'); assert.equal(f.invocations, 1);
    const settled = f.calls.filter(call => call.args.provider?.action === 'settle');
    if (stage === 'settle') { assert.equal(settled.length, 1); }
  }
});

test('permission withdrawal during admission prevents invocation; buffered file cannot borrow a new grant', async () => {
  const f = fixture(); f.revokeAt('start'); const row = await f.engine.run(f.pending);
  assert.equal(row.state, 'unknown'); assert.equal(f.invocations, 0);
  const g = fixture(); g.loseAckAt('result'); await g.engine.run(g.pending);
  const resultCalls = g.calls.filter(call => call.args.provider?.action === 'result').length;
  g.grant('grant-B'); g.cold(); await g.engine.reconcile(g.engine.snapshot.records[0]);
  assert.equal(g.invocations, 1);
  assert.equal(g.calls.filter(call => call.args.provider?.action === 'result').length, resultCalls);
  assert.notEqual(g.engine.snapshot.records[0].state, 'settled');
});

test('allocation identity/content drift and expired dispatch cannot invoke another operation', async () => {
  const f = fixture(); f.mutateRemote(row => { row.provider = 'another-node'; });
  assert.equal((await f.engine.run(f.pending)).state, 'unknown'); assert.equal(f.invocations, 0);
  const g = fixture(); await assert.rejects(g.engine.run({ ...g.pending, deadline: 999 }), /permission_unavailable/);
  assert.equal(g.invocations, 0); assert.equal(g.engine.snapshot.records.length, 0);
  assert.throws(() => validateWorkConfig({ ...g.config, token: 'must-not-save' }, g.config.node_id), /public_configuration_only/);
});
