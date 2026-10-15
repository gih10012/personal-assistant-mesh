import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import test from 'node:test';
import {
  beginOperation, canonicalChallenge, canonicalEnrollment, coldResume, deviceState, exactByteView,
  report, validateChallenge, validateEnrollment, validateHello, validateJoinUrl
} from '../entry/src/main/ets/core/Protocol.ts';

const digest = v => createHash('sha256').update(v).digest('hex');
export const enrollment = {
  format: 'mesh-enrollment/1', mesh_id: 'synthetic-mesh', node_id: 'harmony-fixture', owner_binding: 'synthetic-owner',
  repo_pin: { repository: 'https://github.com/gih10012/personal-assistant-mesh', commit: 'd5e3e1f3130cc66719ce2ac444b79fdbd9c0237c' },
  runtime: { kind: 'interactive', expires_at: null, state_ref: 'private:phone-state' },
  join: { peer_id: 'laptop', authority_id: 'laptop', route_ref: 'private:approved-route',
    credential_ref: 'private:scoped-reference', trust_ref: 'private:owner-trust' },
  continuation: { project_id: '鸿蒙🙂', session_ref: 'private:original-session', state_ref: 'private:原检查点' }
};
export function challenge() {
  return { format: 'mesh-enrollment-challenge/1', operation_id: 'original-op', mesh_id: enrollment.mesh_id,
    node_id: enrollment.node_id, owner_binding: enrollment.owner_binding,
    enrollment_sha256: digest(canonicalEnrollment(enrollment)), continuation: enrollment.continuation,
    nonce: 'a'.repeat(64), created_at: 1000, expires_at: 1060,
    capability_id: 'harmony-fixture:bytes-v1', action: 'challenge.bytes.repeat', revision: 1,
    input: { bytes: 3, sha256: digest('abc'), base64: 'YWJj' }, expected: { bytes: 9, sha256: digest('abcabcabc') },
    ttl_seconds: 60, proxy: null };
}
test('same enrollment contract, interactive lifecycle and real source pin', () => {
  validateEnrollment(enrollment, enrollment.node_id);
  assert.throws(() => validateEnrollment({ ...enrollment, runtime: { ...enrollment.runtime, kind: 'persistent' } }, enrollment.node_id));
  assert.throws(() => validateEnrollment({ ...enrollment, repo_pin: { ...enrollment.repo_pin, commit: '0'.repeat(40) } }, enrollment.node_id));
});
test('owner, node, original continuation and expiry cannot be replaced by challenge', () => {
  const value = challenge(); validateChallenge(value, enrollment, value.enrollment_sha256, 1001);
  for (const changed of [{ ...value, owner_binding: 'another-owner' }, { ...value, node_id: 'another-node' },
    { ...value, continuation: { ...value.continuation, session_ref: 'new-session' } }, { ...value, expires_at: 1001 },
    { ...value, expires_at: 9999 },
    { ...value, proxy: { source_device: 'unpaired-headset' } }]) {
    assert.throws(() => validateChallenge(changed, enrollment, value.enrollment_sha256, 1001));
  }
});
test('never accepts arbitrary shell, all-files or GUI operations', () => {
  for (const action of ['shell.execute', 'file.read.any', 'phone.gui.tap', 'bluetooth.connect']) {
    const value = { ...challenge(), action };
    assert.throws(() => validateChallenge(value, enrollment, value.enrollment_sha256, 1001));
  }
  assert.equal(report(enrollment, false).native_features_restricted, false);
});
test('full artifact budget rejects, never truncates larger output', () => {
  const value = challenge(); value.expected.bytes = 65537;
  assert.throws(() => validateChallenge(value, enrollment, value.enrollment_sha256, 1001));
});
test('attempted persists as unknown on cold resume and same ID cannot rerun', () => {
  const value = challenge(), hash = digest(canonicalChallenge(value)), rows = [];
  beginOperation(rows, value, hash, 1001); const cold = JSON.parse(JSON.stringify(rows)); coldResume(cold);
  assert.equal(cold[0].state, 'unknown');
  assert.throws(() => beginOperation(cold, value, hash, 1002), /read_only/);
  assert.throws(() => beginOperation(cold, { ...value, nonce: 'b'.repeat(64) }, hash, 1002), /changed/);
});
test('complete provider result survives cold reopen without running an effect', () => {
  const value = challenge(), rows = [];
  const row = beginOperation(rows, value, digest(canonicalChallenge(value)), 1001);
  row.state = 'result_reported'; row.result_json = JSON.stringify({ original_complete_private_result: 'unchanged' });
  const cold = JSON.parse(JSON.stringify(rows)); coldResume(cold); assert.deepEqual(cold, rows);
  assert.throws(() => beginOperation(cold, value, row.request_sha256, 1002), /read_only/);
});
test('one unrelated new task may proceed while old original remains unknown', () => {
  const value = challenge(), rows = [];
  beginOperation(rows, value, digest(canonicalChallenge(value)), 1001); coldResume(rows);
  const next = { ...value, operation_id: 'next-independent-op', nonce: 'b'.repeat(64) };
  beginOperation(rows, next, digest(canonicalChallenge(next)), 1002);
  assert.equal(rows[0].state, 'unknown'); assert.equal(rows[1].operation_id, 'next-independent-op');
});
test('peer must match authenticated join identity, not account labels in content', () => {
  const hello = { protocol: 'mesh-a2a/1', node: 'laptop', authority: 'laptop', a2a_idempotent_receive: true };
  validateHello(hello, enrollment);
  assert.throws(() => validateHello({ ...hello, authority: 'openai-codex-cloud' }, enrollment));
  assert.throws(() => validateHello({ ...hello, a2a_idempotent_receive: false }, enrollment));
});
test('join only uses explicit HTTPS origin, no credentials/path/redirection URL', () => {
  validateJoinUrl('https://mesh.example.test:443');
  for (const value of ['http://mesh.example.test', 'https://owner:secret@mesh.example.test',
    'https://mesh.example.test/other', 'https://mesh.example.test?redirect=other', 'https://mesh.example.test:65536']) {
    assert.throws(() => validateJoinUrl(value));
  }
});
test('dynamic foreground/permission/file grant conditions never imply device permission', () => {
  assert.equal(deviceState(enrollment.node_id, true, true, true).capabilities[1].available, true);
  assert.equal(deviceState(enrollment.node_id, false, true, true).state, 'SUSPENDED');
  assert.ok(deviceState(enrollment.node_id, false, true, true).capabilities.every(c => !c.available));
  assert.equal(deviceState(enrollment.node_id, true, true, false).capabilities[1].available, false);
  assert.equal(deviceState(enrollment.node_id, true, true, false).gui_adapter, 'not_verified');
});
test('canonical ASCII bytes preserve supplementary Unicode for original Python reference', () => {
  assert.match(canonicalEnrollment(enrollment), /\\ud83d\\ude42/);
  assert.equal(/[\u0080-\uffff]/.test(canonicalChallenge(challenge())), false);
});
test('native digest input helper excludes allocator slab bytes around a UTF8 buffer view', () => {
  const slab = Buffer.allocUnsafe(128); slab.fill(0x7f);
  const text = '鸿蒙🙂'; const utf8 = Buffer.from(text, 'utf8'); utf8.copy(slab, 17);
  const value = slab.subarray(17, 17 + utf8.length);
  const selected = exactByteView(value.buffer, value.byteOffset, value.byteLength);
  assert.equal(digest(selected), digest(utf8)); assert.equal(selected.length, utf8.length);
  assert.notEqual(digest(new Uint8Array(slab.buffer)), digest(selected));
  assert.throws(() => exactByteView(value.buffer, -1, value.byteLength), /byte_range/);
});
