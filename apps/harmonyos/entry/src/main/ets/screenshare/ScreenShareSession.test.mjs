import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { stripTypeScriptTypes } from 'node:module';
import * as policyModule from './ScreenSharePolicy.ts';

const scope = () => ({ sessionId: 'synthetic-session', taskId: 'original-task', operationId: 'original-operation',
  approvalId: 'original-approval', nonce: 'synthetic-nonce', revision: 3, expiresAt: Date.now() + 300000 });
const frame = (revision = 1) => ({ bytes: new Uint8Array([255, 216, 255, 0, 255, 217]),
  width: 640, height: 320, timestamp: 1234.5 + revision, revision });
const ack = (binding, item) => ({ scope: { ...binding }, frameRevision: item.revision,
  bytes: item.bytes.length, acknowledged: true });
async function settle() { for (let turn = 0; turn < 12; turn++) await Promise.resolve(); }

// Actual session source, mocked platform/capture/sink boundaries. No device,
// native recording, network, HDC, SDK build or real background task is started.
function fixture() {
  const sources = [], backgroundStarts = [], backgroundStops = [], callbacks = new Map();
  const timers = new Map(); let nextTimer = 0;
  let foreground = true, authorised = true, connected = true, detached = 0, hdcClosed = 0;
  let revoked, disconnected, startTicket = async () => ({ continuousTaskId: 77 });
  let sendFrame = async (binding, item) => ack(binding, item);
  class Native {
    stops = 0;
    constructor() { sources.push(this); }
    start(options, onState, onFrame) { this.options = options; this.onState = onState; this.onFrame = onFrame; }
    stop() { this.stops++; }
  }
  class Request { isModeSupported() { return true; } }
  const backgroundTaskManager = {
    ContinuousTaskRequest: Request,
    BackgroundTaskMode: { MODE_AV_PLAYBACK_AND_RECORD: 12 },
    BackgroundTaskSubmode: { SUBMODE_SCREEN_RECORD_NORMAL_NOTIFICATION: 7 },
    on: (name, callback) => callbacks.set(name, callback),
    off: (name, callback) => { if (callbacks.get(name) === callback) callbacks.delete(name); },
    startBackgroundRunning: async (_context, request) => { backgroundStarts.push(request); return startTicket(); },
    stopBackgroundRunning: async (_context, id) => { backgroundStops.push(id); }
  };
  const wantAgent = { getWantAgent: async info => {
    assert.deepEqual(info.wants, [{ bundleName: 'org.personalassistant.mesh', abilityName: 'EntryAbility' }]);
    return {};
  }, OperationType: { START_ABILITY: 1 }, WantAgentFlags: { UPDATE_PRESENT_FLAG: 1, CONSTANT_FLAG: 2 } };
  const code = stripTypeScriptTypes(readFileSync(new URL('./ScreenShareSession.ets', import.meta.url), 'utf8')
    .replace(/^import[\s\S]*?;\r?\n/gm, ''), { mode: 'strip' }).replace(/^export /gm, '');
  const exported = Object.keys(policyModule);
  const timer = callback => { timers.set(++nextTimer, callback); return nextTimer; };
  const clear = id => timers.delete(id);
  const Session = new Function(...exported, 'NativeScreenShare', 'backgroundTaskManager', 'wantAgent',
    'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', code + '\nreturn ScreenShareSession;')(
    ...exported.map(name => policyModule[name]), Native, backgroundTaskManager, wantAgent, timer, clear, timer, clear);
  const authorization = {
    isForeground: () => foreground, valid: () => authorised,
    onRevoked: callback => { revoked = callback; return () => { revoked = undefined; }; },
    closeHdc: () => { hdcClosed++; }
  };
  const sink = { isLive: () => connected,
    onDisconnect: callback => { disconnected = callback; return () => { disconnected = undefined; }; },
    sendFrame: (binding, item) => sendFrame(binding, item), detach: () => { detached++; } };
  const session = new Session(), binding = scope();
  return { session, binding, authorization, sink, sources, backgroundStarts, backgroundStops, callbacks, timers,
    start: () => session.start({}, binding, authorization, sink),
    started: () => sources[0].onState({ state: 'started', errorCode: 0 }),
    frame: item => sources[0].onFrame(item), setForeground: value => { foreground = value; session.foregroundChanged(); },
    disconnect: () => { connected = false; disconnected?.(); },
    revoke: () => { authorised = false; revoked?.(); },
    setTicket: callback => { startTicket = callback; }, setSend: callback => { sendFrame = callback; },
    hdcClosed: () => hdcClosed, detached: () => detached };
}

test('policy requires actual capture, exact frame ACK and OS ticket; static screens stay valid', () => {
  const binding = scope(), policy = new policyModule.ScreenSharePolicy(binding, Date.now()), item = frame();
  assert.equal(policy.canRequestBackground(true, true, Date.now()), false);
  assert.throws(() => policy.acceptFrame(item, true, true, Date.now()), /capture_not_active/);
  policy.started(); policy.acceptFrame(item, true, true, Date.now());
  assert.equal(policy.canRequestBackground(true, true, Date.now()), false);
  assert.throws(() => policy.acknowledge(item, { ...ack(binding, item),
    scope: { ...binding, operationId: 'wrong-operation' } }, Date.now()), /ack_unconfirmed/);
  policy.acknowledge(item, ack(binding, item), Date.now());
  assert.equal(policy.canRequestBackground(true, true, Date.now()), true);
  assert.equal(policy.canRemainBackground(true, true, Date.now()), false);
  policy.backgroundStarted(77);
  assert.equal(policy.canRemainBackground(true, true, binding.expiresAt - 1), true);
  assert.equal(policy.canRemainBackground(true, true, binding.expiresAt), false);
  assert.equal(policy.canRemainBackground(false, true, Date.now()), false);
});

test('session acquires main12/sub7 only after actual frame ACK and releases its exact lease on sink loss', async () => {
  const f = fixture(); f.start();
  assert.equal(f.session.status().state, 'awaiting_capture_consent');
  assert.equal(f.backgroundStarts.length, 0);
  f.sources[0].onState({ state: 'start_requested', errorCode: 0 });
  f.frame(frame()); await settle();
  assert.equal(f.backgroundStarts.length, 0);
  f.started(); assert.equal(f.backgroundStarts.length, 0);
  f.frame(frame()); await settle();
  assert.equal(f.backgroundStarts.length, 1);
  assert.deepEqual(f.backgroundStarts[0].backgroundTaskModes, [12]);
  assert.deepEqual(f.backgroundStarts[0].backgroundTaskSubmodes, [7]);
  assert.equal(f.backgroundStarts[0].combinedTaskNotification, false);
  assert.equal(f.session.status().backgroundActive, true);
  f.setForeground(false); await settle();
  assert.equal(f.session.status().captureActive, true);
  assert.equal(f.session.status().lastCaptureTimestamp, frame().timestamp);
  assert.equal(f.session.status().acknowledgedFrames, 1);
  f.disconnect(); await f.session.stop();
  assert.equal(f.session.status().state, 'stopped');
  assert.deepEqual(f.backgroundStops, [77]);
  assert.equal(f.sources[0].stops, 1); assert.equal(f.hdcClosed(), 1); assert.equal(f.detached(), 1);
  assert.equal(f.callbacks.size, 0); assert.equal(f.timers.size, 0);
});

test('foreground loss before ticket fails closed and late source callbacks cannot restart original scope', async () => {
  const f = fixture(); f.start(); f.started(); f.setForeground(false); await f.session.stop();
  assert.equal(f.session.status().error, 'screen_share_background_ticket_required');
  f.frame(frame()); f.started(); await settle();
  assert.equal(f.backgroundStarts.length, 0); assert.equal(f.session.status().captureActive, false);
  assert.throws(() => f.start(), /original_scope_consumed/);
});

test('revocation during background request releases a late ticket without reactivating capture', async () => {
  const f = fixture(); let ticket;
  f.setTicket(() => new Promise(resolve => { ticket = resolve; }));
  f.start(); f.started(); f.frame(frame()); await settle();
  assert.equal(f.backgroundStarts.length, 1);
  f.revoke();
  assert.equal(f.sources[0].stops, 1);
  ticket({ continuousTaskId: 88 }); await f.session.stop(); await settle();
  assert.deepEqual(f.backgroundStops, [88]);
  assert.equal(f.session.status().state, 'stopped');
  assert.equal(f.session.status().backgroundActive, false);
  assert.equal(f.session.status().cleanupUnknown, false);
});

test('wrong receiver ACK and OS cancellation stop sharing instead of faking qualification', async () => {
  const wrong = fixture();
  wrong.setSend(async (binding, item) => ({ ...ack(binding, item), frameRevision: item.revision + 1 }));
  wrong.start(); wrong.started(); wrong.frame(frame()); await settle(); await wrong.session.stop();
  assert.equal(wrong.backgroundStarts.length, 0);
  assert.equal(wrong.session.status().error, 'screen_share_frame_ack_unconfirmed');
  const canceled = fixture(); canceled.start(); canceled.started(); canceled.frame(frame()); await settle();
  canceled.callbacks.get('continuousTaskCancel')({ id: 77, reason: 0 });
  await canceled.session.stop();
  assert.equal(canceled.session.status().error, 'screen_share_continuous_task_canceled');
  assert.deepEqual(canceled.backgroundStops, [77]);
});
