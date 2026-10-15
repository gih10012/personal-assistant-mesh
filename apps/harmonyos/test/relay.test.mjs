import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import test from 'node:test';
import {
  acceptRelayChallenge, beginOperation, beginRelayChallenge, beginRelayResult, canonicalChallenge,
  canonicalEnrollment, canonicalRelayBinding, canonicalResult, coldResume, coldResumeRelays,
  newRelayRecord, observeRelayStatus, relayChallengeState, relayResultState, requireManualChallengeUnclaimed, validateRelayClientConfig,
  validateRelayResponse, validateRelayUrl, validateResultEnvelope
} from '../entry/src/main/ets/core/Protocol.ts';

const digest = v => createHash('sha256').update(v).digest('hex');
function fixture() {
  const enrollment = { format: 'mesh-enrollment/1', mesh_id: 'synthetic-mesh', node_id: 'synthetic-phone',
    owner_binding: 'synthetic-owner', repo_pin: { repository: 'https://github.com/example/mesh', commit: 'a'.repeat(40) },
    runtime: { kind: 'interactive', expires_at: null, state_ref: 'private:phone-state' },
    join: { peer_id: 'synthetic-peer', authority_id: 'synthetic-authority', route_ref: 'private:original-join',
      credential_ref: 'private:scoped-reference', trust_ref: 'private:owner-root' },
    continuation: { project_id: '鸿蒙🙂', session_ref: 'native:original', state_ref: 'private:原检查点' } };
  const enrollmentHash = digest(canonicalEnrollment(enrollment));
  const challenge = { format: 'mesh-enrollment-challenge/1', operation_id: 'original-op', mesh_id: enrollment.mesh_id,
    node_id: enrollment.node_id, owner_binding: enrollment.owner_binding, enrollment_sha256: enrollmentHash,
    continuation: enrollment.continuation, nonce: 'a'.repeat(64), created_at: 1000, expires_at: 1060,
    capability_id: 'synthetic-phone:bytes-v1', action: 'challenge.bytes.repeat', revision: 1,
    input: { bytes: 3, sha256: digest('abc'), base64: 'YWJj' }, expected: { bytes: 9, sha256: digest('abcabcabc') },
    ttl_seconds: 60, proxy: null };
  const binding = { mesh_id: enrollment.mesh_id, node_id: enrollment.node_id, owner_binding: enrollment.owner_binding,
    enrollment_sha256: enrollmentHash, operation_id: challenge.operation_id, request_sha256: digest(canonicalChallenge(challenge)),
    nonce: challenge.nonce, expires_at: challenge.expires_at, task_id: 'original-task', task_epoch: 1,
    entry_request_id: 'original-owner-request' };
  const config = { format: 'mesh-enrollment-relay-client/1', resource_url: 'https://mesh.example.test/enrollment/node', binding };
  const row = newRelayRecord(config);
  const result = { format: 'mesh-enrollment-result/1', operation_id: binding.operation_id, request_sha256: binding.request_sha256,
    nonce: binding.nonce, result: { bytes: 9, sha256: digest('abcabcabc'), base64: Buffer.from('abcabcabc').toString('base64') },
    continuation: enrollment.continuation, proxy: null };
  function reply(action = 'challenge', executable = true) {
    const value = { format: 'mesh-enrollment-relay-response/1', binding: structuredClone(binding), action,
      status: { format: 'mesh-enrollment-status/1', mesh_id: binding.mesh_id, node_id: binding.node_id,
        runtime_kind: 'interactive', operation_id: binding.operation_id, request_sha256: binding.request_sha256,
        expires_at: binding.expires_at, state: action === 'challenge' ? 'attempted' : 'result_reported',
        result_sha256: action === 'challenge' ? null : digest(canonicalResult(result)),
        managed_use_authorized: false, native_execution_attested: false, automatic_replay: false },
      may_execute_original_challenge: action === 'challenge' ? executable : false,
      automatic_retry: false, automatic_replay: false, relay_performed_verification: false,
      relay_performed_registration: false, managed_use_authorized: false, native_execution_attested: false,
      native_features_restricted: false };
    if (action === 'challenge') { value.challenge = structuredClone(challenge); }
    if (action === 'result') { value.original_result_retained = true; }
    return value;
  }
  return { enrollment, enrollmentHash, challenge, binding, config, row, result, reply };
}
test('relay config is exact original owner/device binding without token or new URL authority', () => {
  const f = fixture(); validateRelayClientConfig(f.config, f.enrollment, f.enrollmentHash);
  for (const key of ['mesh_id', 'node_id', 'owner_binding', 'enrollment_sha256']) {
    const changed = structuredClone(f.config); changed.binding[key] = 'b'.repeat(64);
    assert.throws(() => validateRelayClientConfig(changed, f.enrollment, f.enrollmentHash));
  }
  assert.throws(() => validateRelayClientConfig({ ...f.config, token: 'DO_NOT_PERSIST' }, f.enrollment, f.enrollmentHash));
  assert.throws(() => validateRelayClientConfig({ ...f.config, binding: { ...f.binding, task_epoch: true } }, f.enrollment, f.enrollmentHash));
});
test('relay URL is HTTPS443 exact path, never general API, redirect, credential or alternate port', () => {
  for (const value of ['https://192.0.2.10/enrollment/node', 'https://mesh.example.test:443/enrollment/node']) {
    validateRelayUrl(value);
  }
  for (const value of ['http://mesh.example.test/enrollment/node', 'https://mesh.example.test:8443/enrollment/node',
    'https://owner:secret@mesh.example.test/enrollment/node', 'https://mesh.example.test/v1/tasks',
    'https://mesh.example.test/enrollment/node?redirect=x', 'https://mesh.example.test/enrollment/node/',
    'https://mesh.example.test/other/enrollment/node', 'https://mesh.example.test/%65nrollment/node']) {
    assert.throws(() => validateRelayUrl(value));
  }
});
test('literal executable grant plus complete original challenge allows one local durable intent', () => {
  const f = fixture(); beginRelayChallenge(f.row);
  assert.equal(f.row.challenge_state, 'attempted'); // This snapshot is flushed before network.
  acceptRelayChallenge(f.row, f.reply(), f.enrollment, f.enrollmentHash, f.binding.request_sha256, 1001);
  assert.equal(f.row.challenge_state, 'granted');
  const rows = []; const local = beginOperation(rows, f.challenge, f.binding.request_sha256, 1001);
  f.row.challenge_state = 'consumed'; // Same durable snapshot is flushed before the native effect.
  assert.equal(local.state, 'attempted');
  assert.throws(() => beginRelayChallenge(f.row), /observe_only/);
  assert.throws(() => beginOperation(rows, f.challenge, f.binding.request_sha256, 1002), /read_only/);
});
test('false grant is original observation even after expiry, never a local execution permission', () => {
  const f = fixture(); beginRelayChallenge(f.row);
  acceptRelayChallenge(f.row, f.reply('challenge', false), f.enrollment, f.enrollmentHash, f.binding.request_sha256, 9999);
  assert.equal(f.row.challenge_state, 'observed');
  assert.throws(() => beginRelayChallenge(f.row), /observe_only/);
  assert.throws(() => requireManualChallengeUnclaimed([f.row], f.binding.operation_id), /observe_only/);
});
test('truthy strings, stale executable permission and response self-verification are rejected', () => {
  for (const mutation of [v => { v.may_execute_original_challenge = 'true'; },
    v => { v.managed_use_authorized = true; }, v => { v.relay_performed_verification = true; },
    v => { v.status.state = 'unknown'; }, v => { v.status.native_execution_attested = true; }]) {
    const f = fixture(); beginRelayChallenge(f.row); const value = f.reply(); mutation(value);
    assert.throws(() => acceptRelayChallenge(f.row, value, f.enrollment, f.enrollmentHash, f.binding.request_sha256, 1001));
    assert.equal(f.row.challenge_state, 'attempted');
  }
  const f = fixture(); beginRelayChallenge(f.row);
  assert.throws(() => acceptRelayChallenge(f.row, f.reply(), f.enrollment, f.enrollmentHash, f.binding.request_sha256, 1060));
});
test('all original response binding fields, nonce and complete request hash are fenced', () => {
  const seed = fixture();
  for (const key of Object.keys(seed.binding)) {
    const f = fixture(); beginRelayChallenge(f.row); const changed = f.reply();
    changed.binding[key] = typeof changed.binding[key] === 'number' ? changed.binding[key] + 1 : 'b'.repeat(64);
    assert.throws(() => acceptRelayChallenge(f.row, changed, f.enrollment, f.enrollmentHash, f.binding.request_sha256, 1001));
  }
  for (const mutation of [v => { v.challenge.nonce = 'b'.repeat(64); },
    v => { v.challenge.continuation.session_ref = 'replacement-session'; },
    v => { v.challenge.operation_id = 'replacement-op'; }]) {
    const f = fixture(); beginRelayChallenge(f.row); const changed = f.reply(); mutation(changed);
    assert.throws(() => acceptRelayChallenge(f.row, changed, f.enrollment, f.enrollmentHash,
      digest(canonicalChallenge(changed.challenge)), 1001));
  }
});
test('cold loss of challenge ACK or received grant stays unknown and cannot use manual import to revive', () => {
  for (const state of ['attempted', 'granted']) {
    const f = fixture(); f.row.challenge_state = state;
    const rows = JSON.parse(JSON.stringify([f.row])); coldResumeRelays(rows);
    assert.equal(rows[0].challenge_state, 'unknown');
    assert.throws(() => beginRelayChallenge(rows[0]), /observe_only/);
    assert.throws(() => requireManualChallengeUnclaimed(rows, f.binding.operation_id), /observe_only/);
    requireManualChallengeUnclaimed(rows, 'unrelated-original-operation');
  }
});
test('complete original result envelope and ASCII hash keep Python canonical identity', () => {
  const f = fixture(); validateResultEnvelope(f.result, f.binding, f.enrollment);
  assert.deepEqual(JSON.parse(canonicalResult(f.result)), f.result);
  assert.match(canonicalResult(f.result), /\\ud83d\\ude42/);
  assert.equal(/[\u0080-\uffff]/.test(canonicalResult(f.result)), false);
  for (const key of ['operation_id', 'request_sha256', 'nonce']) {
    assert.throws(() => validateResultEnvelope({ ...f.result, [key]: 'b'.repeat(64) }, f.binding, f.enrollment));
  }
  assert.throws(() => validateResultEnvelope({ ...f.result, proxy: {} }, f.binding, f.enrollment));
  assert.throws(() => validateResultEnvelope({ ...f.result, result: { ...f.result.result, bytes: 65537 } }, f.binding, f.enrollment));
});
test('result POST attempted/ACK loss is observation-only, same bytes never authorize a second POST', () => {
  const f = fixture(); const hash = digest(canonicalResult(f.result)); beginRelayResult(f.row, hash);
  const rows = JSON.parse(JSON.stringify([f.row])); coldResumeRelays(rows);
  assert.equal(rows[0].result_state, 'unknown'); assert.equal(rows[0].result_sha256, hash);
  assert.throws(() => beginRelayResult(rows[0], hash), /observe_only/);
  observeRelayStatus(rows[0], f.reply('status'), 'status');
  assert.equal(rows[0].result_state, 'acknowledged');
  assert.throws(() => beginRelayResult(rows[0], hash), /observe_only/);
});
test('native state rereads observe the same record after transition and cold-resume helper mutations', () => {
  const f = fixture();
  assert.equal(relayChallengeState(f.row), 'not_requested');
  beginRelayChallenge(f.row); assert.equal(relayChallengeState(f.row), 'attempted');
  acceptRelayChallenge(f.row, f.reply(), f.enrollment, f.enrollmentHash, f.binding.request_sha256, 1001);
  assert.equal(relayChallengeState(f.row), 'granted');
  assert.equal(relayResultState(f.row), 'not_submitted');
  beginRelayResult(f.row, digest(canonicalResult(f.result)));
  assert.equal(relayResultState(f.row), 'attempted');
  coldResumeRelays([f.row]);
  assert.equal(relayChallengeState(f.row), 'unknown');
  assert.equal(relayResultState(f.row), 'unknown');
  observeRelayStatus(f.row, f.reply('status'), 'status');
  assert.equal(relayResultState(f.row), 'acknowledged');
  assert.throws(() => beginRelayResult(f.row, digest(canonicalResult(f.result))), /observe_only/);
  assert.throws(() => beginRelayChallenge(f.row), /observe_only/);
});
test('provider-reported without matching full-envelope hash cannot settle a result ACK', () => {
  const f = fixture(); beginRelayResult(f.row, digest(canonicalResult(f.result)));
  const absent = f.reply('status'); absent.status.result_sha256 = null;
  observeRelayStatus(f.row, absent, 'status'); assert.equal(f.row.result_state, 'attempted');
  const conflict = f.reply('status'); conflict.status.result_sha256 = 'b'.repeat(64);
  assert.throws(() => observeRelayStatus(f.row, conflict, 'status'), /conflict/);
  assert.equal(f.row.result_state, 'unknown');
});
test('remote existing result never counts as permission to execute or force a new result upload', () => {
  const f = fixture(); observeRelayStatus(f.row, f.reply('status'), 'status');
  assert.equal(f.row.result_state, 'remote_reported');
  assert.throws(() => beginRelayResult(f.row, digest(canonicalResult(f.result))), /observe_only/);
  const unexpected = f.reply('status'); unexpected.may_execute_original_challenge = true;
  assert.throws(() => validateRelayResponse(unexpected, f.binding, 'status'));
});
test('reopen preserves native original complete result and relay IDs, no credentials in checkpoints', () => {
  const f = fixture(), local = [];
  const result = beginOperation(local, f.challenge, f.binding.request_sha256, 1001);
  result.state = 'result_reported'; result.result_json = JSON.stringify(f.result);
  beginRelayResult(f.row, digest(canonicalResult(f.result)));
  const cold = JSON.parse(JSON.stringify({ records: local, relays: [f.row] }));
  coldResume(cold.records); coldResumeRelays(cold.relays);
  assert.equal(cold.records[0].result_json, result.result_json);
  assert.equal(cold.relays[0].binding.nonce, f.binding.nonce);
  assert.equal(cold.relays[0].result_state, 'unknown');
  assert.deepEqual(Object.keys(cold.relays[0]).sort(),
    ['binding', 'challenge_json', 'challenge_state', 'resource_url', 'result_sha256', 'result_state', 'status_json']);
  assert.equal(canonicalRelayBinding(cold.relays[0].binding), canonicalRelayBinding(f.binding));
});
