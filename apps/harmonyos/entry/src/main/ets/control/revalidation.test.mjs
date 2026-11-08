import assert from 'node:assert/strict';
import test from 'node:test';
import { ControlSession, freshControlState } from './ControlSession.ts';
import { RuntimeApprovalGate } from './RuntimeApprovalGate.ts';
import { CONTROL_BUNDLE, CONTROL_DECISION_EVENT, decodeBackgroundDecision } from './BackgroundDecisionProtocol.ts';

async function fixture() {
  let serial = 0, reads = 0, calls = 0, prepared = true, now = 10000;
  const state = freshControlState('synthetic-phone');
  const saved = [];
  const host = { now: () => now, id: () => 'synthetic-' + ++serial,
    save: async value => { saved.push(structuredClone(value)); } };
  const view = { bundle_name: 'synthetic.app', window_id: 'window-1', view_token: 'native-1', width: 100, height: 200,
    truncated: false, elements: [{ key: 'element-1', role: 'Button', label: 'Original', sensitive: false, enabled: true,
      actions: ['tap', 'set_text'], bounds: { left: 0, top: 0, width: 50, height: 50 }, state_fingerprint: 'a'.repeat(64) }] };
  const provider = { availability: () => ({ provider: 'synthetic', inspect: true, act: true,
    human_input_observable: true, reason: '' }),
    inspect: async () => ({ ...structuredClone(view), view_token: 'native-' + ++reads }),
    act: async invocation => {
      assert.equal(saved.at(-1).records[0].state, 'attempted');
      assert.equal(invocation.view_token, 'native-2'); calls++; return true;
    } };
  const session = new ControlSession(state, 'synthetic-phone', host, provider);
  await session.initialize(); session.setOwnerEnabled(true);
  const lease = session.acquire('original-task', 20000), observed = await session.inspect();
  const request = { operation_id: 'original-op', task_id: 'original-task', action: 'tap',
    element_ref: observed.elements[0].element_ref, text: '' };
  const ticket = await session.request(request, lease.lease_id);
  const decision = { approval_id: ticket.approval_id, nonce: ticket.nonce, operation_id: ticket.operation_id,
    task_id: ticket.task_id, revision: ticket.revision, expires_at: ticket.expires_at, decision: 'approve' };
  return { session, state, host, saved, provider, view, request, ticket, decision,
    sourcePrepared: () => prepared, stopSource: () => { prepared = false; }, expire: () => { now = 30001; },
    reads: () => reads, calls: () => calls };
}
test('OS publisher identity cannot be replaced by payload identity or a system publisher', () => {
  const decision = { approval_id: 'ticket', nonce: 'nonce', operation_id: 'original-op', task_id: 'original-task',
    revision: 2, expires_at: 30000, decision: 'approve' };
  const raw = JSON.stringify(decision);
  assert.deepEqual(decodeBackgroundDecision(CONTROL_BUNDLE, CONTROL_DECISION_EVENT, raw), decision);
  for (const publisher of ['', 'system', 'com.other.app']) {
    assert.throws(() => decodeBackgroundDecision(publisher, CONTROL_DECISION_EVENT, raw), /untrusted_background_decision/);
  }
  assert.throws(() => decodeBackgroundDecision(CONTROL_BUNDLE, 'other-event', raw), /untrusted_background_decision/);
  assert.throws(() => decodeBackgroundDecision(CONTROL_BUNDLE, CONTROL_DECISION_EVENT,
    JSON.stringify({ ...decision, publisherBundleName: CONTROL_BUNDLE })), /untrusted_background_decision/);
});
test('explicit decision rereads identical complete view and grants a new local fence without changing original context', async () => {
  const f = await fixture(), original = structuredClone(f.state.records[0]);
  f.session.ownerActivity();
  assert.equal(f.session.approvalCurrent('original-op'), false);
  await assert.rejects(f.session.decideBound(f.decision), /expired_or_revoked/); // Default strict path remains strict.
  await f.session.decideAfterRevalidation(f.decision, f.sourcePrepared);
  assert.equal(f.reads(), 2); assert.equal(f.session.approvalCurrent('original-op'), true);
  for (const name of ['operation_id', 'task_id', 'request_json', 'approval_id', 'nonce', 'revision', 'expires_at']) {
    assert.equal(f.state.records[0][name], original[name]);
  }
  const result = await f.session.execute('original-op');
  assert.equal(result.state, 'input_acknowledged'); assert.equal(result.effect_verified, false); assert.equal(f.calls(), 1);
  await assert.rejects(f.session.decideAfterRevalidation(f.decision, f.sourcePrepared), /original_pending_candidate_required/);
});
test('changed window, geometry, semantic state or a truncated view cannot renew the original ticket', async () => {
  const changes = [v => { v.window_id = 'another-window'; }, v => { v.elements[0].bounds.left = 1; },
    v => { v.elements[0].state_fingerprint = 'b'.repeat(64); }, v => { v.truncated = true; }];
  for (const change of changes) {
    const f = await fixture(); f.session.ownerActivity(); change(f.view);
    await assert.rejects(f.session.decideAfterRevalidation(f.decision, f.sourcePrepared), /original_view_revalidation_failed/);
    assert.equal(f.calls(), 0); assert.equal(f.session.approvalCurrent('original-op'), false);
    assert.equal(f.state.records[0].state, 'awaiting_approval');
  }
});
test('Source not prepared, expired ticket, cold state and unknown operation never gain a renewed grant', async () => {
  const stopped = await fixture(); stopped.stopSource();
  await assert.rejects(stopped.session.decideAfterRevalidation(stopped.decision, stopped.sourcePrepared), /revalidation_unavailable/);
  assert.equal(stopped.reads(), 1);
  const expired = await fixture(); expired.expire();
  await assert.rejects(expired.session.decideAfterRevalidation(expired.decision, expired.sourcePrepared), /pending_candidate_required/);
  const cold = await fixture();
  const reopened = new ControlSession(structuredClone(cold.state), 'synthetic-phone', cold.host, cold.provider);
  await reopened.initialize(); reopened.setOwnerEnabled(true);
  await assert.rejects(reopened.decideAfterRevalidation(cold.decision, cold.sourcePrepared), /pending_candidate_required/);
  const unknown = await fixture(); unknown.state.records[0].state = 'unknown';
  await assert.rejects(unknown.session.decideAfterRevalidation(unknown.decision, unknown.sourcePrepared), /pending_candidate_required/);
  assert.equal(cold.calls() + unknown.calls(), 0);
});
test('human activity during re-read or durable renewal revokes the new permission', async () => {
  const reading = await fixture(); reading.session.ownerActivity();
  const inspect = reading.provider.inspect;
  reading.provider.inspect = async () => { const v = await inspect(); reading.session.ownerActivity(); return v; };
  await assert.rejects(reading.session.decideAfterRevalidation(reading.decision, reading.sourcePrepared), /view_unavailable/);
  const committing = await fixture(); committing.session.ownerActivity();
  const save = committing.host.save;
  committing.host.save = async value => { await save(value); if (value.records[0].state === 'approved') committing.session.ownerActivity(); };
  await assert.rejects(committing.session.decideAfterRevalidation(committing.decision, committing.sourcePrepared), /revalidation_failed/);
  assert.equal(committing.session.approvalCurrent('original-op'), false);
  assert.equal(reading.calls() + committing.calls(), 0);
});
test('dormant pending retains its original deadline but cannot complete without an actual new proof fence', async () => {
  const f = await fixture(), gate = new RuntimeApprovalGate(() => 10000);
  const permit = { ticket: f.ticket, dispatch_json: 'original', input_json: '{}', input_hash: 'a'.repeat(64),
    owner_grant: 'original-grant', permission_context: 'original-permission', action_kind: 'action',
    fence: { valid: true, human_input_epoch: 0 }, state: 'awaiting_approval', hardware_armed: false };
  const waiting = gate.begin(permit); gate.invalidateForRevalidation();
  assert.equal(gate.current(), permit); assert.equal(permit.ticket.expires_at, f.ticket.expires_at);
  assert.equal(gate.allowed('original', 'original-grant'), false); assert.equal(gate.matches(f.decision), true);
  assert.throws(() => gate.complete(true), /fresh_revalidated_fence_required/);
  gate.complete(false); assert.equal(await waiting, false);
});
