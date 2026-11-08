import assert from 'node:assert/strict';
import test from 'node:test';
import { SelfMaintenanceSupervisor, freshSupervisor, restoreSupervisor } from '../entry/src/main/ets/selfmaintenance/SelfMaintenance.ts';

const snapshot = { diagnostic: 'signed_data_sync', state: 'verification_failed', data_version: 2, pending_receipts: 1 };
function fixture() {
  const state = freshSupervisor();
  const calls = []; let durable; let loseAck = false; let created = 0;
  const host = {
    id: () => 'original-incident',
    async save(value) { durable = structuredClone(value); },
    async exchange(action, args) {
      calls.push(action);
      if (action === 'mesh_maintenance_submit') {
        assert.equal(durable.records[0].state, 'attempted'); created++;
        if (loseAck) { loseAck = false; throw new Error('lost ACK'); }
      }
      return { format: 'mesh-phone-maintenance/1', incident_id: args.incident_id,
        task_id: 'original-task', operation_id: 'original-operation', task_epoch: 1,
        task_created: action === 'mesh_maintenance_submit', status: 'pending',
        local_cognition: 'not_configured', automatic_repair: false, result_available: false };
    }
  };
  return { state, calls, host, engine: new SelfMaintenanceSupervisor(state, host),
    loseAck() { loseAck = true; }, get created() { return created; }, get durable() { return durable; } };
}
test('default disabled; lost ACK preserves original incident and only queries original task', async () => {
  const f = fixture();
  assert.equal(await f.engine.incident(snapshot), null);
  await f.engine.setEnabled(true);
  const row = await f.engine.incident(snapshot);
  f.loseAck(); await assert.rejects(f.engine.advance('original-session', row));
  assert.equal(row.state, 'unknown');
  const same = await f.engine.incident({ ...snapshot, state: 'unconfirmed' });
  assert.equal(same, row);
  await f.engine.advance('original-session', row);
  assert.deepEqual(f.calls, ['mesh_maintenance_submit', 'mesh_maintenance_status']);
  assert.equal(f.created, 1);
  assert.equal(row.task_id, 'original-task');
  assert.equal(row.operation_id, 'original-operation');
});
test('cold attempted is observe-only and a changed response cannot replace task identity', async () => {
  const f = fixture(); await f.engine.setEnabled(true);
  const row = await f.engine.incident(snapshot); row.state = 'attempted';
  restoreSupervisor(f.state); assert.equal(row.state, 'unknown');
  await f.engine.advance('original-session', row);
  assert.deepEqual(f.calls, ['mesh_maintenance_status']);
  f.host.exchange = async () => ({ format: 'mesh-phone-maintenance/1', incident_id: row.incident_id,
    task_id: 'replacement', operation_id: 'replacement', local_cognition: 'not_configured', automatic_repair: false });
  await assert.rejects(f.engine.advance('original-session', row));
  assert.equal(row.task_id, 'original-task');
});
test('results are returned as suggestions without executing repairs', async () => {
  const f = fixture(); await f.engine.setEnabled(true);
  const row = await f.engine.incident(snapshot);
  f.host.exchange = async (action, args) => ({ format: 'mesh-phone-maintenance/1',
    incident_id: args.incident_id, task_id: 'original-task', operation_id: 'original-operation',
    status: 'completed', task_epoch: 1, task_created: false, local_cognition: 'not_configured',
    automatic_repair: false, result_available: true,
    ...(action === 'mesh_maintenance_result' ? { result: 'Retain trusted signing key.' } : {}) });
  await f.engine.advance('session', row);
  assert.equal(row.state, 'finished'); assert.equal(row.result, 'Retain trusted signing key.');
  assert.equal(await f.engine.incident(snapshot), row); // persistent failure is the same episode
  await f.engine.incident({ ...snapshot, state: 'ok' });
  f.host.id = () => 'next-recurrent-incident';
  const recurrent = await f.engine.incident(snapshot);
  assert.equal(recurrent.incident_id, 'next-recurrent-incident');
  assert.equal(f.state.records[0].task_id, 'original-task');
});
