import assert from 'node:assert/strict';
import { registerHooks } from 'node:module';
import test from 'node:test';
registerHooks({ resolve(specifier, context, next) {
  return next(['./Protocol', '../core/WorkProtocol'].includes(specifier) ? specifier + '.ts' : specifier, context);
} });
const { validateRuntimeConfig } = await import('../entry/src/main/ets/update/RuntimeConfig.ts');
const { validateDistributionLink } = await import('../entry/src/main/ets/upgrade/DistributionConfig.ts');

test('runtime consumers permit a signed route value within original identity and reject added authority', () => {
  const original = { format:'mesh-phone-client/1', node_id:'synthetic-phone', mesh_id:'mesh', authority_id:'source',
    session_id:'original-session', resource_url:'https://old.example.test/phone/node' };
  const config = { ...original, format:'amita-trusted-runtime/1', resource_url:'https://new.example.test/phone/node', liveview_event:'PROGRESS' };
  const before = structuredClone(original);
  const value = validateRuntimeConfig(config, original, 3);
  assert.equal(value.work.resource_url, config.resource_url);
  assert.equal(value.liveviewEvent, 'PROGRESS'); assert.deepEqual(original, before);
  for (const extra of [{enabled:true}, {screen_capture:true}, {token:'untrusted'}, {permissions:[]},
    {node_id:'foreign'}, {session_id:'foreign'}, {mesh_id:'foreign'}, {authority_id:'foreign'},
    {resource_url:'http://bad.example.test/phone/node'}, {resource_url:'https://new.example.test/v1/tasks'}]) {
    assert.throws(() => validateRuntimeConfig({...config,...extra}, original, 3));
  }
});

test('native invitation and device links remain fixed fields, same bundle and HTTPS without installation claims', () => {
  const config = {format:'amita-native-distribution-link/1', bundle_name:'synthetic.bundle',
    channel:'agc-invitation-test', url:'https://approved.example.test/original-link', expires_at:200};
  validateDistributionLink(config, 'synthetic.bundle', 100);
  validateDistributionLink({...config,channel:'agc-designated-device'}, 'synthetic.bundle', 100);
  for (const extra of [{channel:'silent-hap'}, {command:'install'}, {bundle_name:'other.bundle'},
    {url:'file:///private/code.hap'}, {url:'http://insecure.test/install'}, {expires_at:99}]) {
    assert.throws(() => validateDistributionLink({...config,...extra}, 'synthetic.bundle', 100));
  }
});
