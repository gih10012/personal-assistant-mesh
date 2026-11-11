import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { stripTypeScriptTypes } from 'node:module';
import * as protocol from './SelfHdcProtocol.ts';

// Run the actual controller/client logic with in-memory preferences and socket
// stubs. No HDC/device/socket connection, platform keys or screenshot are used.
function source(file) {
  return stripTypeScriptTypes(readFileSync(new URL(file, import.meta.url), 'utf8')
    .replace(/^import[\s\S]*?;\r?\n/gm, ''), { mode: 'strip' }).replace(/^export /gm, '');
}

function controllerFixture() {
  const saved = new Map(), puts = [], clients = [], removed = [];
  let flush = () => Promise.resolve();
  class Client {
    current = { state: 'idle', error: '', authenticated: false };
    calls = [];
    next = undefined;
    constructor() { clients.push(this); }
    status() { return { ...this.current }; }
    async connect(port) {
      this.calls.push(port);
      this.current = this.next ?? { state: 'ready', error: '', authenticated: true, stage: 'ready' };
      return this.status();
    }
    close() { this.current = { state: 'closed', error: '', authenticated: false }; }
  }
  class Driver {
    async identity() { return 'uid=2000(shell)'; }
    async layout() { return '{"synthetic":"local tree"}'; }
  }
  const preferences = { getPreferencesSync: (_context, options) => {
    assert.equal(options.name, 'amita_selfhdc_ui_v1');
    return { getSync: (key, fallback) => saved.get(key) ?? fallback,
      putSync: (key, value) => { puts.push([key, value]); saved.set(key, value); },
      flush: () => flush() };
  } };
  const code = source('./SelfHdcController.ets');
  const factory = new Function('SelfHdcClient', 'SelfHdcDriver', 'preferences', 'fileIo', 'util', code +
    '\nreturn { SelfHdcController, selfHdcPort, selfHdcDiagnostic, selfHdcStage };');
  return { ...factory(Client, Driver, preferences, { unlinkSync: p => removed.push(p) }, {}),
    saved, puts, clients, removed, setFlush: callback => { flush = callback; } };
}

test('only a valid manual port is remembered; foreground loss clears results and never reconnects', async () => {
  const fixture = controllerFixture(), controller = new fixture.SelfHdcController({ cacheDir: '/synthetic' });
  assert.equal(controller.savedPort(), '');
  assert.equal(fixture.clients[0].calls.length, 0);
  controller.setForeground(true);
  assert.equal((await controller.connect('00000')).diagnosticCode, 'selfhdc_port_invalid');
  assert.equal(fixture.puts.length, 0);
  assert.equal((await controller.connect('41083')).authenticated, true);
  await controller.probe('identity'); await controller.probe('layout');
  controller.imagePath = '/synthetic/preview.jpeg';
  controller.view.imageUri = 'file:///synthetic/preview.jpeg';
  controller.setForeground(false);
  const hidden = controller.snapshot();
  assert.equal(hidden.authenticated, false); assert.equal(hidden.identity, '');
  assert.equal(hidden.layout, ''); assert.equal(hidden.imageUri, '');
  assert.deepEqual(fixture.removed, ['/synthetic/preview.jpeg']);
  controller.setForeground(true);
  assert.deepEqual(fixture.clients[0].calls, [41083]);
  const reopened = new fixture.SelfHdcController({});
  assert.equal(reopened.savedPort(), '41083');
  assert.equal(reopened.snapshot().authenticated, false);
  assert.deepEqual(fixture.puts, [['last_port', '41083']]);
  assert.deepEqual([...fixture.saved.keys()], ['last_port']);
});

test('cancel during preference flush prevents late connection and does not restore busy or consent', async () => {
  const fixture = controllerFixture(), controller = new fixture.SelfHdcController({});
  let completeFlush;
  fixture.setFlush(() => new Promise(resolve => { completeFlush = resolve; }));
  controller.setForeground(true);
  const connecting = controller.connect('41083');
  controller.setForeground(false);
  completeFlush();
  const result = await connecting;
  assert.equal(result.busy, false); assert.equal(result.authenticated, false);
  assert.deepEqual(fixture.clients[0].calls, []);
});

test('local snapshots expose expiry and clear stale observations without network activity', async () => {
  const fixture = controllerFixture(), controller = new fixture.SelfHdcController({});
  controller.setForeground(true); await controller.connect('41083'); await controller.probe('identity');
  fixture.clients[0].current = { state: 'error', error: 'selfhdc_session_expired', authenticated: false, stage: 'ready' };
  const expired = controller.snapshot();
  assert.equal(expired.state, 'expired'); assert.equal(expired.identity, '');
  assert.equal(expired.authenticated, false); assert.equal(expired.diagnosticCode, 'selfhdc_session_expired');
  assert.equal(controller.savedPort(), '41083');
  assert.deepEqual(fixture.clients[0].calls, [41083]);
});

test('diagnostics preserve actual phase/finite SDK code and never reflect arbitrary error strings', async () => {
  const fixture = controllerFixture(), controller = new fixture.SelfHdcController({});
  controller.setForeground(true);
  fixture.clients[0].next = { state: 'error', authenticated: false, error: 'selfhdc_socket_error',
    stage: 'socket_connect', nativeCode: 2301101 };
  const failed = await controller.connect('35361');
  assert.equal(failed.diagnosticStage, '连接本机调试端口');
  assert.equal(failed.diagnosticCode, 'selfhdc_socket_error · SDK 2301101');
  assert.match(failed.message, /当前端口/);
  fixture.clients[0].next = { state: 'error', authenticated: false,
    error: 'private-challenge-do-not-display', stage: 'private-payload', nativeCode: NaN };
  const rejected = await controller.connect('41083');
  assert.equal(rejected.diagnosticCode, 'selfhdc_unknown_failure');
  assert.equal(rejected.diagnosticStage, '本机调试');
  assert.equal(JSON.stringify(rejected).includes('private-'), false);
  assert.equal(fixture.selfHdcPort('65536'), '');
  assert.equal(fixture.selfHdcPort('1;id'), '');
  assert.equal(fixture.selfHdcPort('00001'), '1');
});

test('actual client preserves bind/connect failure phase and bounded SDK number', async () => {
  const code = source('./SelfHdcClient.ets');
  const exported = Object.keys(protocol);
  for (const failingPhase of ['socket_bind', 'socket_connect']) {
    const endpoint = { on() {}, off() {}, close: () => Promise.resolve(),
      bind: async () => { if (failingPhase === 'socket_bind') throw { code: 201, message: 'private socket text' }; },
      connect: async () => { throw { code: 2301101, message: 'private socket text' }; } };
    const Identity = { load: async () => ({}), randomBytes: async n => new Uint8Array(n) };
    const Client = new Function(...exported, 'socket', 'SelfHdcIdentity', code + '\nreturn SelfHdcClient;')(
      ...exported.map(name => protocol[name]), { constructTCPSocketInstance: () => endpoint }, Identity);
    const client = new Client({});
    const failed = await client.connect(41083, 1000);
    assert.equal(failed.stage, failingPhase);
    assert.equal(failed.nativeCode, failingPhase === 'socket_bind' ? 201 : 2301101);
    assert.equal(failed.error, 'selfhdc_connection_or_identity_failed');
    assert.equal(JSON.stringify(failed).includes('private socket text'), false);
    client.close();
  }
});
