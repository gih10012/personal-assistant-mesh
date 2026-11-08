import assert from 'node:assert/strict';
import test from 'node:test';
import { RuntimeApprovalGate } from './RuntimeApprovalGate.ts';

function permit() {
  return { ticket: { approval_id: 'synthetic-approval', nonce: 'synthetic-nonce', operation_id: 'original-op',
    task_id: 'original-task', revision: 3, expires_at: 30000, summary: 'Synthetic action', review_required: false },
    dispatch_json: '{"original-task-epoch":7}', input_json: '{}', input_hash: 'a'.repeat(64), owner_grant: 'synthetic-grant',
    permission_context: 'gui-action|synthetic-grant|synthetic-approval|' + 'a'.repeat(64), action_kind: 'action',
    fence: { valid: true, human_input_epoch: 0 }, state: 'awaiting_approval', hardware_armed: false };
}
const decision = p => ({ approval_id: p.ticket.approval_id, nonce: p.ticket.nonce,
  operation_id: p.ticket.operation_id, task_id: p.ticket.task_id, revision: p.ticket.revision,
  expires_at: p.ticket.expires_at, decision: 'approve' });
test('pending presentation cannot admit work; exact original tuple and explicit decision are required', async () => {
  const gate = new RuntimeApprovalGate(() => 10000), p = permit(), waiting = gate.begin(p);
  assert.equal(gate.allowed(p.dispatch_json, p.owner_grant), false);
  assert.equal(gate.matches({ ...decision(p), task_id: 'other-task' }), false);
  assert.equal(gate.matches({ ...decision(p), nonce: 'other-nonce' }), false);
  assert.equal(gate.matches({ ...decision(p), expires_at: 30001 }), false);
  assert.equal(p.hardware_armed, false); gate.armHardware(); assert.equal(p.hardware_armed, true);
  assert.equal(gate.matches(decision(p)), true);
  gate.complete(true); assert.equal(await waiting, true);
  assert.equal(gate.allowed('changed-epoch', p.owner_grant), false);
  assert.equal(gate.allowed(p.dispatch_json, 'revoked-grant'), false);
  assert.equal(gate.consume(p.dispatch_json, p.owner_grant), p);
  assert.throws(() => gate.consume(p.dispatch_json, p.owner_grant), /same_operation_approval_required/);
  gate.cancel();
});
test('refusal and deadline never turn into approval or source admission', async () => {
  let now = 10000;
  const gate = new RuntimeApprovalGate(() => now), p = permit();
  const denied = gate.begin(p); gate.complete(false);
  assert.equal(await denied, false); assert.equal(p.fence.valid, false);
  const expiredPermit = permit(), expired = gate.begin(expiredPermit);
  now = 30001; gate.complete(true);
  assert.equal(await expired, false); assert.equal(gate.current(), null);
});
test('ordinary input or disconnect cancels pending and invalidates an approved permit', async () => {
  const gate = new RuntimeApprovalGate(() => 10000), p = permit(), waiting = gate.begin(p);
  gate.cancel(); assert.equal(await waiting, false); assert.equal(p.fence.valid, false);
  const approved = permit(), next = gate.begin(approved); gate.complete(true); await next;
  gate.cancel(); assert.equal(approved.fence.valid, false);
  assert.equal(gate.allowed(approved.dispatch_json, approved.owner_grant), false);
});
