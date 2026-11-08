import assert from 'node:assert/strict';
import test from 'node:test';
import { ControlSession, freshControlState } from './ControlSession.ts';

// Synthetic provider only: no Harmony SDK, real screen, task, or owner material.
async function fixture() {
  let now = 100000, serial = 0, writes = [], invocations = [];
  const state = freshControlState('synthetic-phone');
  const host = { now: () => now, id: () => 'synthetic-' + ++serial,
    save: async value => { writes.push(structuredClone(value)); } };
  const provider = {
    availability: () => ({ provider: 'synthetic-fixture', inspect: true, act: true,
      human_input_observable: true, reason: '' }),
    inspect: async () => ({ bundle_name: 'synthetic.app', window_id: 'window-1', view_token: 'view-1',
      width: 100, height: 200, truncated: false,
      elements: [{ key: 'button-1', role: 'Button', label: 'Synthetic action', sensitive: false, enabled: true,
        actions: ['tap'], bounds: { left: 0, top: 0, width: 50, height: 50 } }] }),
    act: async invocation => {
      assert.equal(writes.at(-1).records.at(-1).state, 'attempted');
      assert.equal(invocation.fence.valid, true);
      invocations.push(invocation); return true;
    }
  };
  const session = new ControlSession(state, 'synthetic-phone', host, provider);
  await session.initialize(); session.setOwnerEnabled(true);
  const lease = session.acquire('original-task', 30000);
  const view = await session.inspect();
  const request = { operation_id: 'original-op', task_id: 'original-task', action: 'tap',
    element_ref: view.elements[0].element_ref, text: '' };
  return { session, state, host, provider, lease, view, request, writes, invocations,
    advance: ms => { now += ms; } };
}
test('durable original operation and one local decision precede a single input acknowledgement', async () => {
  const f = await fixture();
  const ticket = await f.session.request(f.request, f.lease.lease_id);
  assert.equal(ticket.task_id, 'original-task');
  assert.equal(f.session.receipt('original-op').state, 'awaiting_approval');
  assert.equal(f.invocations.length, 0); // Presentation/request is never approval.
  await assert.rejects(f.session.execute('original-op'), /not_approved/);
  await assert.rejects(f.session.decide(ticket.approval_id, 'wrong', 'approve'), /approval_mismatch/);
  const decision = { approval_id: ticket.approval_id, nonce: ticket.nonce, operation_id: ticket.operation_id,
    task_id: ticket.task_id, revision: ticket.revision, expires_at: ticket.expires_at, decision: 'approve' };
  await assert.rejects(f.session.decideBound({ ...decision, task_id: 'another-task' }), /approval_mismatch/);
  await assert.rejects(f.session.decideBound({ ...decision, expires_at: decision.expires_at + 1 }), /approval_mismatch/);
  await f.session.decideBound(decision);
  const receipt = await f.session.execute('original-op');
  assert.deepEqual(receipt, { operation_id: 'original-op', state: 'input_acknowledged', input_acknowledged: true,
    effect_verified: false, automatic_replay: false });
  assert.equal(f.invocations.length, 1);
  await assert.rejects(f.session.execute('original-op'), /not_approved/);
  await assert.rejects(f.session.request(f.request, f.lease.lease_id), /observe_only/);
});
test('fresh observations invalidate old element refs and pending decisions', async () => {
  const f = await fixture();
  const ticket = await f.session.request(f.request, f.lease.lease_id);
  const next = await f.session.inspect();
  assert.notEqual(next.elements[0].element_ref, f.request.element_ref);
  await assert.rejects(f.session.decide(ticket.approval_id, ticket.nonce, 'approve'), /expired_or_stale/);
  await assert.rejects(f.session.request({ ...f.request, operation_id: 'other-op' }, f.lease.lease_id), /stale_element_ref/);
  assert.equal(f.invocations.length, 0);
});
test('real owner activity releases exclusive screen and cancels pre-admission grants', async () => {
  const f = await fixture();
  assert.throws(() => f.session.acquire('other-task', 1000), /screen_busy/);
  const ticket = await f.session.request(f.request, f.lease.lease_id);
  await f.session.decide(ticket.approval_id, ticket.nonce, 'approve');
  f.session.ownerActivity();
  await assert.rejects(f.session.execute('original-op'), /expired_or_revoked/);
  assert.equal(f.invocations.length, 0);
  assert.equal(f.session.acquire('other-task', 1000).task_id, 'other-task');
});
test('owner input during durable admission prevents provider invocation', async () => {
  const f = await fixture();
  const ticket = await f.session.request(f.request, f.lease.lease_id);
  await f.session.decide(ticket.approval_id, ticket.nonce, 'approve');
  const save = f.host.save;
  f.host.save = async value => { await save(value); if (value.records[0].state === 'attempted') f.session.ownerActivity(); };
  await assert.rejects(f.session.execute('original-op'), /owner_interrupted/);
  assert.equal(f.session.receipt('original-op').state, 'cancelled');
  assert.equal(f.invocations.length, 0);
});
test('uncertain input and cold attempted state preserve unknown without replay', async () => {
  const f = await fixture();
  const ticket = await f.session.request(f.request, f.lease.lease_id);
  await f.session.decide(ticket.approval_id, ticket.nonce, 'approve');
  f.provider.act = async () => { throw new Error('synthetic receipt missing'); };
  assert.equal((await f.session.execute('original-op')).state, 'unknown');
  const cold = structuredClone(f.state); cold.records[0].state = 'attempted';
  const reopened = new ControlSession(cold, 'synthetic-phone', f.host, f.provider);
  await reopened.initialize();
  assert.equal(reopened.receipt('original-op').state, 'unknown');
  await assert.rejects(reopened.execute('original-op'), /not_approved/);
  assert.throws(() => new ControlSession(cold, 'another-phone', f.host, f.provider), /private_state_invalid/);
});
test('denial and expiry never become approval, and capability absence is explicit', async () => {
  const f = await fixture();
  const ticket = await f.session.request(f.request, f.lease.lease_id);
  await f.session.decide(ticket.approval_id, ticket.nonce, 'deny');
  await assert.rejects(f.session.execute('original-op'), /not_approved/);
  const second = await f.session.request({ ...f.request, operation_id: 'second-op' }, f.lease.lease_id);
  f.advance(30001);
  await assert.rejects(f.session.decide(second.approval_id, second.nonce, 'approve'), /expired_or_revoked/);
  const other = await fixture();
  other.provider.availability = () => ({ provider: 'unverified-public-api', inspect: true, act: false,
    human_input_observable: false, reason: 'not_verified' });
  await assert.rejects(other.session.request(other.request, other.lease.lease_id), /provider_unavailable/);
  assert.equal(other.state.records.length, 0);
});
