import assert from 'node:assert/strict';
import test from 'node:test';
import { MAX_ARTIFACT_BYTES, MAX_UPDATE_BYTES, validateTrust, validateManifest } from '../entry/src/main/ets/update/Manifest.ts';

const hash = 'a'.repeat(64);
const trust = { format: 'amita-update-trust/1', node_id: 'fixture-phone', enabled: true, key_id: 'fixture-key',
  manifest_url: 'https://fixture.invalid/v1/harmony/updates/manifest', public_key_spki_base64: 'YWJj' };
const artifact = (path = 'skills/example.md', size = 12) => ({ path, kind: 'skill', size, sha256: hash,
  url: 'https://fixture.invalid/v1/harmony/updates/artifacts/' + hash });
const manifest = () => ({ format: 'amita-data-manifest/1', version: 2, min_shell_version: 1000000, artifacts: [artifact()] });

test('trusted bootstrap binds exact node and HTTPS manifest route', () => {
  validateTrust(trust, 'fixture-phone');
  for (const change of [{ node_id: 'another' }, { manifest_url: 'http://fixture.invalid/v1/harmony/updates/manifest' },
    { manifest_url: 'https://user@fixture.invalid/v1/harmony/updates/manifest' },
    { manifest_url: trust.manifest_url + '?redirect=1' }, { key_id: '../key' }]) {
    assert.throws(() => validateTrust({ ...trust, ...change }, 'fixture-phone'));
  }
});
test('signed data cannot request a native code install or escape the private data namespace', () => {
  for (const path of ['../outside', 'skills/../outside', '/absolute', 'skills//file', 'skills/./file', 'file\\outside',
    'upgrade.hap', 'module.abc', 'native.so', 'script.js', 'skill.ets', 'remote.wasm']) {
    assert.throws(() => validateManifest({ ...manifest(), artifacts: [artifact(path)] }, trust, 1));
  }
  validateManifest(manifest(), trust, 1);
});
test('artifact URLs stay on the pinned origin and content address', () => {
  for (const url of ['https://elsewhere.invalid/v1/harmony/updates/artifacts/' + hash,
    'https://fixture.invalid/v1/harmony/updates/artifacts/' + 'b'.repeat(64),
    artifact().url + '?redirect=1']) {
    assert.throws(() => validateManifest({ ...manifest(), artifacts: [{ ...artifact(), url }] }, trust, 1));
  }
});
test('explicit route replacement preserves signed installed bytes without authorizing old-origin downloads', () => {
  const replaced = { ...trust, manifest_url: 'https://replacement.invalid/v1/harmony/updates/manifest' };
  validateTrust(replaced, trust.node_id);
  assert.throws(() => validateManifest(manifest(), replaced, 2));
  validateManifest(manifest(), replaced, 0, true);
  const corrupt = { ...manifest(), artifacts: [{ ...artifact(), url: artifact().url + '?unexpected' }] };
  assert.throws(() => validateManifest(corrupt, replaced, 0, true));
});
test('shell compatibility, monotonic versions and file/byte limits are enforced before downloading', () => {
  for (const change of [{ version: 1 }, { version: 1.5 }, { min_shell_version: 1000001 }, { min_shell_version: 0 },
    { artifacts: [artifact('a', MAX_ARTIFACT_BYTES + 1)] }, { artifacts: [artifact('a', -1)] },
    { artifacts: [artifact('same'), artifact('same')] },
    { artifacts: Array.from({ length: 33 }, (_, i) => artifact('file' + i, 0)) },
    { artifacts: [artifact('a', MAX_ARTIFACT_BYTES), artifact('b', MAX_ARTIFACT_BYTES), artifact('c', 1)] }]) {
    assert.throws(() => validateManifest({ ...manifest(), ...change }, trust, 2));
  }
  const atLimit = { ...manifest(), artifacts: [artifact('a', MAX_ARTIFACT_BYTES), artifact('b', MAX_ARTIFACT_BYTES)] };
  assert.equal(atLimit.artifacts.reduce((sum, item) => sum + item.size, 0), MAX_UPDATE_BYTES);
  validateManifest(atLimit, trust, 2);
});
