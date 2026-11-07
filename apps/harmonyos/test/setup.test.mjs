import assert from 'node:assert/strict';
import test from 'node:test';
import { MAX_SETUP_BYTES, parseOwnerSetup } from '../entry/src/main/ets/core/SetupProtocol.ts';

const enrollment = () => ({ format: 'amita-phone-owner-setup/1', node_id: 'synthetic-phone', phase: 'enrollment',
  enrollment_json: '{"original":"enrollment"}', relay_json: '{"original":"relay"}',
  relay_token: 'synthetic-token-'.repeat(3), work_bootstrap_json: '', update_trust_json: '' });
const member = () => ({ ...enrollment(), phase: 'member', enrollment_json: '', relay_json: '', relay_token: '',
  work_bootstrap_json: '{"original":"bootstrap"}', update_trust_json: '' });
const parse = value => {
  const raw = JSON.stringify(value);
  return parseOwnerSetup(raw, 'synthetic-phone', Buffer.byteLength(raw));
};

test('owner-local setup accepts both phases and preserves original opaque protocol inputs', () => {
  assert.deepEqual(parse(enrollment()), enrollment());
  assert.deepEqual(parse(member()), member());
  const value = { ...member(), update_trust_json: '{"original":"trust"}' };
  assert.equal(parse(value).update_trust_json, value.update_trust_json);
});
test('strict envelope rejects another node, paths, commands, missing fields and non-string inputs', () => {
  const missing = enrollment(); delete missing.relay_json;
  for (const value of [null, [], {}, missing, { ...enrollment(), node_id: 'another-phone' },
    { ...enrollment(), format: 'other/1' }, { ...enrollment(), path: '/private/elsewhere' },
    { ...enrollment(), command: 'run' }, { ...enrollment(), relay_json: {} },
    { ...member(), update_trust_json: null }, { ...member(), work_bootstrap_json: false }]) {
    assert.throws(() => parse(value), /^Error: mesh_owner_setup_invalid$/);
  }
});
test('phase separation cannot import enrollment credentials as member or silently skip a required binding', () => {
  for (const value of [{ ...enrollment(), phase: 'other' }, { ...enrollment(), enrollment_json: '' },
    { ...enrollment(), relay_json: '' }, { ...enrollment(), relay_token: 'short' },
    { ...enrollment(), relay_token: 'x'.repeat(32) + '\n' },
    { ...enrollment(), work_bootstrap_json: '{}' }, { ...enrollment(), update_trust_json: '{}' },
    { ...member(), relay_token: 'x'.repeat(32) }, { ...member(), enrollment_json: '{}' },
    { ...member(), relay_json: '{}' }, { ...member(), work_bootstrap_json: '' }]) {
    assert.throws(() => parse(value), /^Error: mesh_owner_setup_invalid$/);
  }
});
test('the actual file byte budget is enforced, including UTF-8, and errors never repeat secret input', () => {
  const raw = JSON.stringify({ ...member(), work_bootstrap_json: '私'.repeat(23000) });
  assert.ok(raw.length < MAX_SETUP_BYTES && Buffer.byteLength(raw) > MAX_SETUP_BYTES);
  assert.throws(() => parseOwnerSetup(raw, 'synthetic-phone', Buffer.byteLength(raw)), /mesh_owner_setup_invalid/);
  assert.throws(() => parseOwnerSetup('{"secret-token":"private', 'synthetic-phone', 24),
    /^Error: mesh_owner_setup_invalid$/);
  const valid = JSON.stringify(member());
  for (const size of [0, -1, 1.5, NaN, MAX_SETUP_BYTES + 1]) {
    assert.throws(() => parseOwnerSetup(valid, 'synthetic-phone', size), /mesh_owner_setup_invalid/);
  }
});
