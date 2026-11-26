import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { stripTypeScriptTypes } from 'node:module';
import { createHash } from 'node:crypto';
import * as protocol from '../core/Protocol.ts';
import { decodeScreenShareInput, sameScreenShareScope, sameScreenShareTarget, validScreenShareLaunchScope,
  validScreenShareTarget } from './ScreenSharePolicy.ts';

const digest = async bytes => createHash('sha256').update(bytes).digest('hex');
const requireValue = protocol.requireValue;
const workCode = stripTypeScriptTypes(readFileSync(new URL('../core/WorkProtocol.ts', import.meta.url), 'utf8')
  .replace(/^import[\s\S]*?;\r?\n/gm, ''), { mode: 'strip' }).replace(/^export /gm, '');
const work = new Function('MAX_BYTES', 'requireValue', 'validateJoinUrl', workCode +
  '\nreturn { WorkEngine, freshWorkSnapshot, coldResumeWork, validateWorkBlob, validateWorkConfig, validateWorkDispatch };')(
  protocol.MAX_BYTES, requireValue, protocol.validateJoinUrl);
const blob = async value => {
  const bytes = Buffer.from(JSON.stringify(value));
  return { bytes: bytes.length, base64: bytes.toString('base64'), sha256: await digest(bytes) };
};
async function settle() { for (let turn = 0; turn < 100; turn++) await Promise.resolve(); }
async function until(predicate) {
  for (let turn = 0; turn < 10 && !predicate(); turn++) await settle();
  assert.equal(Boolean(predicate()), true, 'actual runtime did not reach the expected boundary');
}

// Execute the actual shipped runtime and original WorkEngine. Only platform
// preferences/Asset/HTTPS/media-owner boundaries are synthetic. No SDK, phone,
// screen, real node, Source DB, network or background service is accessed.
async function fixture({ target, launchEnabled = target !== undefined, standing, locked = false } = {}) {
  let now = 1000000, foreground = true, serial = 0, nextTimer = 0, managedStarts = 0, sourceCloses = 0;
  let online = false;
  let stopPromise = null, acceptRelease = null, holdAccept = false, closeRelease = null;
  let closingAllowed = false;
  const stores = new Map(), writes = [], requests = [], timers = new Map(), launches = [];
  let launchResolve, launchReject, observedRevision = 0;
  const config = { format: 'mesh-phone-client/1', node_id: 'phone', session_id: 'original-primary',
    resource_url: 'https://original.example.test/phone/node', mesh_id: 'mesh', authority_id: 'authority' };
  const input = await blob(target ? { format: 'amita-phone-screen-share/2', command: 'screen_share_open_app',
    duration_ms: 60000, max_fps: 1, target } :
    { format: 'amita-phone-screen-share/1', command: 'screen_share', duration_ms: 60000, max_fps: 1 });
  const original = { operation_id: 'original-operation', capability_id: 'phone:ui-control-v1', provider: 'node:phone',
    state: 'pending', epoch: 1, receipt_id: null, task_id: 'original-task', task_epoch: 7, deadline: 1090,
    selection: { capability_id: 'phone:ui-control-v1', epoch: 1, action: target ? 'action' : 'inspect',
      scope: target ? { task_id: 'original-task', task_epoch: 7, purpose: 'screen_share_open_app', target: { ...target } } :
        { purpose: 'Synthetic read-only task' },
      workload: { input } } };
  let remote = structuredClone(original);
  let media = { state: 'idle', captureActive: false, backgroundActive: false, acknowledgedFrames: 0,
    lastFrameRevision: 0, lastCaptureTimestamp: -1, lastAckObservedAt: 0, error: '', nativeCode: 0,
    cleanupUnknown: false, lastAcknowledgedSha256: '' };
  let authorization = null, installedScope = null, gate = null;
  const prefs = name => {
    if (!stores.has(name)) stores.set(name, new Map());
    return { getSync: (key, fallback) => stores.get(name).has(key) ? stores.get(name).get(key) : fallback,
      putSync: (key, value) => { stores.get(name).set(key, value); writes.push({ name, key, value }); },
      flush: async () => {} };
  };
  prefs('mesh_node_v1').putSync('snapshot', JSON.stringify({ node_id: 'phone' }));
  prefs('mesh_work_v1').putSync('snapshot', JSON.stringify({ ...work.freshWorkSnapshot('phone'), config }));
  class Encoder { encodeInto(text) { return new TextEncoder().encode(text); } }
  class Decoder { decodeToString(bytes) { return new TextDecoder('utf-8', { fatal: true }).decode(bytes); } }
  class Base64 { decodeSync(text) { return new Uint8Array(Buffer.from(text, 'base64')); }
    encodeToStringSync(bytes) { return Buffer.from(bytes).toString('base64'); } }
  class Credentials { async load() { return { nodeId: 'phone', token: 'SYNTHETIC_ASSET_TOKEN_DO_NOT_PERSIST'.padEnd(48, '_'),
    scope: ['phone:node'], expiresAt: 100000 }; } }
  class Trusted { async load() { return null; } }
  const timer = (callback, delay, repeat = false) => {
    const id = ++nextTimer; timers.set(id, { callback, at: now + delay, delay, repeat }); return id;
  };
  const clear = id => timers.delete(id);
  const owner = {
    attach: () => {}, status: () => ({ ...media }), sourceCloseUnconfirmed: () => false,
    liveFrameRevision: scope => authorization?.valid(scope) && media.captureActive && media.backgroundActive &&
      media.acknowledgedFrames > 0 ? observedRevision : 0,
    foregroundChanged: () => {},
    startManaged: async (primary, session, record, scope, auth, liveGate) => {
      managedStarts++;
      assert.deepEqual(primary, config); assert.equal(session, 'phone:screen-share-v1');
      assert.equal(record.state, 'attempted'); assert.equal(record.authority_epoch, 3);
      assert.equal(record.result_json, ''); assert.notEqual(record.invocation_nonce, '');
      assert.equal(remote.state, 'running'); assert.equal(scope.taskEpoch, 7);
      const saved = JSON.parse(stores.get('amita_screen_share_runtime_v1').get('work_snapshot'));
      assert.equal(saved.records[0].state, 'attempted'); assert.equal(saved.records[0].invocation_nonce, record.invocation_nonce);
      authorization = auth; installedScope = scope; gate = liveGate;
      assert.equal(auth.valid(scope), true); assert.equal(gate.serviceLive(), true);
      auth.onRevoked(() => { owner.stop(); });
      media = { ...media, state: 'awaiting_capture_consent' };
    },
    stop: () => {
      if (stopPromise) return stopPromise;
      if (media.state === 'idle') return Promise.resolve();
      media = { ...media, state: 'stopping', captureActive: false, backgroundActive: false,
        error: 'screen_share_owner_stopped' };
      stopPromise = (async () => {
        if (!closingAllowed) await new Promise(resolve => { closeRelease = resolve; });
        sourceCloses++;
        media = { ...media, state: 'stopped' };
      })();
      return stopPromise;
    }
  };
  const view = online => ({ format: 'mesh-interactive-node/1', mesh_id: 'mesh', execution_verified: false,
    native_tools_restricted: false, session: { node_id: 'phone', session_id: 'phone:screen-share-v1',
      lifecycle: 'screen-share', installed_adapter: { enabled: true, screen_share_enabled: true,
        capability_id: 'phone:ui-control-v1', epoch: 1, execution_verification: 'declared_only',
        screen_share_launch_enabled: launchEnabled, screen_share_standing_allowed: standing },
      network: { state: online ? 'online' : 'offline', expires_at: online ? now / 1000 + 30 : now / 1000,
        presence_ttl_seconds: 30 } } });
  const http = { RequestMethod: { POST: 'POST' }, HttpProtocol: { HTTP1_1: 0 }, HttpDataType: { STRING: 1 },
    createHttp: () => ({ destroy: () => {}, request: async (url, options) => {
      assert.equal(url, config.resource_url); assert.equal(options.maxRedirects, 0);
      assert.equal(options.header.Authorization.startsWith('Bearer SYNTHETIC_ASSET_TOKEN'), true);
      const envelope = JSON.parse(options.extraData); requests.push({ ...envelope, at: now });
      const args = envelope.arguments; let response;
      if (envelope.action === 'mesh_connect' || envelope.action === 'mesh_node_update') {
        online = args.online; response = view(online);
      }
      else if (envelope.action === 'mesh_discover') response = view(online);
      else if (envelope.action === 'mesh_work_pull') response = { format: 'mesh-interactive-node/1', mesh_id: 'mesh',
        dispatch: remote.state === 'pending' ? [structuredClone(original)] : [] };
      else {
        assert.equal(envelope.action, 'mesh_work_report'); const provider = args.provider;
        let result;
        if (provider.action === 'inspect') result = { operation_id: original.operation_id, task_id: original.task_id,
          task_epoch: 7, plan: { selections: [structuredClone(original.selection)] }, dispatch: [structuredClone(remote)] };
        else if (provider.action === 'accept') {
          if (holdAccept) await new Promise(resolve => { acceptRelease = resolve; });
          assert.equal(remote.state, 'pending');
          remote = { ...remote, state: 'accepted', epoch: remote.epoch + 1, receipt_id: provider.arguments.receipt_id };
          result = structuredClone(remote);
        } else if (provider.action === 'start') {
          assert.equal(remote.state, 'accepted');
          remote = { ...remote, state: 'running', epoch: remote.epoch + 1, execute_once: true }; result = structuredClone(remote);
        } else if (provider.action === 'result') {
          assert.equal(sourceCloses, 1, 'Source close must finish before the original result is sent');
          const resultBlob = provider.arguments.result;
          const summary = JSON.parse(Buffer.from(resultBlob.base64, 'base64').toString());
          assert.equal(summary.format, target ? 'amita-phone-screen-share-result/2' : 'amita-phone-screen-share-result/1');
          assert.equal(summary.readonly, !target);
          assert.equal(summary.source_closed, true); assert.equal(summary.capture_quiescent, true);
          assert.equal(summary.last_ack_sha256, 'a'.repeat(64));
          assert.equal(summary.acknowledged_frames, target ? 3 : 1);
          if (target) {
            assert.deepEqual(summary.target, target); assert.equal(summary.launch_system_acknowledged, true);
            assert.equal(summary.launch_baseline_frame_revision, 2); assert.equal(summary.later_frame_revision, 3);
          }
          assert.equal(JSON.stringify(summary).includes('base64'), false);
          result = { result_reference: 'phone-result:' + resultBlob.sha256, sha256: resultBlob.sha256, bytes: resultBlob.bytes };
        } else if (provider.action === 'settle') {
          remote = { ...remote, state: 'completed', epoch: remote.epoch + 1,
            settlement: { outcome: 'completed', evidence: provider.arguments.evidence } }; result = structuredClone(remote);
        } else if (provider.action === 'unknown') {
          remote = { ...remote, state: 'unknown', epoch: remote.epoch + 1 }; result = structuredClone(remote);
        } else assert.fail('unexpected provider action');
        response = { format: 'mesh-interactive-node/1', mesh_id: 'mesh', provider: result };
      }
      return { responseCode: 200, result: JSON.stringify(response) };
    } }) };
  const code = stripTypeScriptTypes(readFileSync(new URL('./ScreenShareRuntime.ets', import.meta.url), 'utf8')
    .replace(/^import[\s\S]*?;\r?\n/gm, ''), { mode: 'strip' }).replace(/^export /gm, '');
  const exported = Object.keys(work);
  const load = () => new Function(...exported, 'requireValue', 'preferences', 'deviceInfo', 'screenLock', 'http', 'util',
    'NodeCredentialStore', 'TrustedRuntimeConfig', 'controlBlob', 'controlDigest', 'ScreenShareOwner',
    'decodeScreenShareInput', 'sameScreenShareScope', 'sameScreenShareTarget', 'validScreenShareLaunchScope',
    'validScreenShareTarget', 'AppStorage', 'Date', 'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval',
    code + '\nreturn ScreenShareRuntime;')(...exported.map(name => work[name]), requireValue,
    { removePreferencesFromCacheSync: () => {}, getPreferencesSync: (_context, options) => prefs(options.name) },
    { sdkApiVersion: 26 }, { isScreenLocked: async () => {
      if (locked instanceof Error) throw locked;
      return locked;
    } }, http, { TextEncoder: Encoder, TextDecoder: Decoder, Base64Helper: Base64,
      generateRandomUUID: () => 'synthetic-' + (++serial) }, Credentials, Trusted, blob, digest, owner,
    decodeScreenShareInput, sameScreenShareScope, sameScreenShareTarget, validScreenShareLaunchScope, validScreenShareTarget,
    { get: key => key === 'meshForeground' ? foreground : undefined }, { now: () => now },
    (callback, delay) => timer(callback, delay), clear, (callback, delay) => timer(callback, delay, true), clear);
  const context = { startAbility: want => {
    assert.equal(foreground, true);
    const runtimeStore = stores.get('amita_screen_share_runtime_v1');
    assert.equal(JSON.parse(runtimeStore.get('work_snapshot')).records[0].state, 'attempted');
    assert.equal(JSON.parse(runtimeStore.get('approvals'))[0].launchStatus, 'attempted');
    launches.push(want);
    return new Promise((resolve, reject) => { launchResolve = resolve; launchReject = reject; });
  } };
  let Runtime = load(), runtime = await Runtime.create(context);
  const advance = async milliseconds => {
    now += milliseconds;
    for (const [id, item] of [...timers]) {
      if (item.at <= now) {
        if (item.repeat) item.at = now + item.delay; else timers.delete(id);
        item.callback();
      }
    }
    await settle();
  };
  return { stores, writes, requests, original, owner, advance, launches, now: () => now,
    runtime: () => runtime, records: () => runtime.engine.snapshot.records,
    setForeground: value => { foreground = value; runtime.foregroundChanged(); },
    setLocked: value => { locked = value; },
    setStanding: value => { standing = value; },
    start: async () => { await runtime.receive(); await until(() => runtime.pending() !== null); return runtime.pending(); },
    markActualFrameAndTicket: (revision = 1) => { observedRevision = revision;
      media = { ...media, state: 'sharing', captureActive: true, backgroundActive: true,
      acknowledgedFrames: revision, lastFrameRevision: revision, lastCaptureTimestamp: 1234.5, lastAckObservedAt: now,
      lastAcknowledgedSha256: 'a'.repeat(64) }; },
    markInFlightFrame: revision => { observedRevision = revision; },
    resolveLaunch: () => launchResolve(), rejectLaunch: () => launchReject(new Error('synthetic refusal')),
    markCaptureWithoutTicket: () => { media = { ...media, state: 'capture_started_waiting_frame_ack', captureActive: true }; },
    allowClose: () => { closingAllowed = true; closeRelease?.(); },
    starts: () => managedStarts, closes: () => sourceCloses,
    holdAccept: () => { holdAccept = true; }, acceptWaiting: () => acceptRelease !== null,
    releaseAccept: () => { holdAccept = false; acceptRelease?.(); },
    reload: async () => { timers.clear(); Runtime = load(); runtime = await Runtime.create(context); return runtime; }
  };
}

test('unlocked foreground discovers the standing grant and consumes only the durable original read-only approval', async () => {
  const f = await fixture({ standing: true });
  f.setForeground(true);
  await until(() => f.starts() === 1);
  assert.equal(f.requests[0].action, 'mesh_discover');
  assert.equal(f.requests.filter(request => request.action === 'mesh_connect').length, 1);
  assert.equal(f.runtime().pending(), null);
  assert.equal(f.runtime().status().captureConsentPending, true);
  assert.equal(f.runtime().status().acknowledgedFrames, 0);
  const approval = JSON.parse(f.stores.get('amita_screen_share_runtime_v1').get('approvals'))[0];
  assert.equal(approval.state, 'consumed');
  assert.equal(approval.standing, true);
  assert.equal(approval.operationId, f.original.operation_id);
  assert.equal(approval.inputHash, f.original.selection.workload.input.sha256);
  assert.equal(JSON.stringify(f.requests).includes(approval.approvalId), false);
  assert.equal(JSON.stringify(f.requests).includes(approval.nonce), false);
});

test('locked and unobservable windows never advertise standing reception; unlock resumes it automatically', async () => {
  const f = await fixture({ standing: true, locked: true });
  f.setForeground(true); await settle();
  assert.equal(f.runtime().status().screenState, 'locked');
  assert.equal(f.requests.length, 0);
  f.setLocked(new Error('system state unavailable')); await f.advance(10000);
  assert.equal(f.runtime().status().screenState, 'unavailable');
  assert.equal(f.requests.length, 0);
  f.setLocked(false); await f.advance(10000);
  await until(() => f.starts() === 1);
});

test('absent or revoked Source standing grant cannot auto-start capture or leave reception online', async () => {
  const legacy = await fixture();
  legacy.setForeground(true); await settle();
  assert.deepEqual(legacy.requests.map(item => item.action), ['mesh_discover']);
  assert.equal(legacy.runtime().status().receiving, false);
  assert.equal(legacy.starts(), 0);

  const f = await fixture({ standing: true });
  f.holdAccept(); f.setForeground(true);
  await until(f.acceptWaiting);
  f.setStanding(false); await f.advance(10000);
  f.releaseAccept();
  await until(() => f.runtime().status().standingAllowed === false);
  assert.equal(f.starts(), 0);
  assert.equal(f.runtime().pending(), null);
  assert.equal(f.runtime().status().receiving, false);
  await until(() => f.requests.some(item => item.action === 'mesh_node_update' && !item.arguments.online));
});

test('the original owner stop remains paused across foreground changes and restarts', async () => {
  const f = await fixture({ standing: true });
  f.setForeground(true); await until(() => f.starts() === 1);
  f.allowClose(); await f.runtime().stop();
  await f.reload(); f.setForeground(true); await settle();
  assert.equal(f.runtime().status().receptionPaused, true);
  assert.equal(f.runtime().status().receiving, false);
  assert.equal(f.starts(), 1);
  await f.runtime().receive();
  assert.equal(f.runtime().status().receptionPaused, false);
  assert.equal(f.runtime().status().receiving, true);
  assert.equal(f.starts(), 1);
});

test('standing mode leaves a launch with no owner input monitor unadmitted', async () => {
  const f = await fixture({ standing: true, target: { bundle_name: 'example.app', ability_name: 'MainAbility' } });
  f.setForeground(true);
  await until(() => f.runtime().status().error === 'screen_share_launch_input_monitor_unavailable');
  assert.equal(f.runtime().pending(), null);
  assert.equal(f.starts(), 0);
  assert.equal(f.launches.length, 0);
  assert.deepEqual(f.records(), []);
});

test('durable exact owner approval admits the original WorkEngine; first frame never settles; final close precedes result', async () => {
  const f = await fixture(), pending = await f.start();
  assert.equal(f.records()[0].state, 'prepared'); assert.equal(f.starts(), 0);
  const durableApproval = JSON.parse(f.stores.get('amita_screen_share_runtime_v1').get('approvals'))[0];
  assert.equal(durableApproval.state, 'pending'); assert.equal(durableApproval.inputHash, pending.inputHash);
  assert.equal(f.requests.some(item => item.arguments.provider?.action === 'accept'), false);
  await assert.rejects(f.runtime().decide({ ...pending, nonce: 'wrong-local-nonce' }, true), /decision_mismatch/);
  await f.runtime().decide(pending, true); await until(() => f.starts() === 1);
  assert.equal(f.records()[0].state, 'attempted');
  f.markActualFrameAndTicket(); await f.advance(250);
  assert.equal(f.records()[0].state, 'attempted'); assert.equal(f.records()[0].result_json, '');
  const stopping = f.runtime().stop(); await settle();
  assert.equal(f.records()[0].result_json, ''); assert.equal(f.closes(), 0);
  f.allowClose(); await stopping; await f.advance(250); await until(() => f.records()[0].state === 'settled');
  assert.equal(f.closes(), 1); assert.equal(f.records()[0].operation_id, 'original-operation');
  assert.equal(f.records()[0].task_epoch, 7);
  assert.equal(JSON.stringify(f.requests).includes(pending.nonce), false);
  assert.equal(JSON.stringify(f.requests).includes(pending.approvalId), false);
});

test('10-second presence pulse remains independent while execute awaits, only with real foreground/media eligibility; no task renewal', async () => {
  const f = await fixture(), pending = await f.start();
  await f.runtime().decide(pending, true); await until(() => f.starts() === 1);
  f.markActualFrameAndTicket(); f.setForeground(false); await settle();
  const before = f.requests.filter(item => item.action === 'mesh_node_update' && item.arguments.online).length;
  await f.advance(10000); await f.advance(10000);
  assert.equal(f.records()[0].state, 'attempted');
  assert.equal(f.requests.filter(item => item.action === 'mesh_node_update' && item.arguments.online).length > before, true);
  assert.equal(f.original.deadline, 1090);
  assert.equal(f.requests.some(item => /renew|lease/i.test(item.action)), false);
  f.allowClose(); const stopping = f.runtime().stop(); await stopping; await f.advance(250);
  await until(() => f.records()[0].state === 'result_pending');
  assert.equal(f.records()[0].result_json.length > 0, true);
  const stoppedAt = f.now(); await f.advance(10000);
  assert.equal(f.requests.some(item => item.at > stoppedAt && item.arguments.online === true), false);
  f.setForeground(true); await until(() => f.records()[0].state === 'settled');
  assert.equal(f.starts(), 1, 'foreground metadata reunion must not restart capture');
});

test('foreground loss during admission revokes a spent local approval before any capture or start dispatch', async () => {
  const f = await fixture(), pending = await f.start(); f.holdAccept();
  await f.runtime().decide(pending, true); await until(() => f.acceptWaiting());
  f.setForeground(false); f.releaseAccept(); await until(() => f.records()[0].state === 'unknown');
  assert.equal(f.starts(), 0);
  assert.equal(f.requests.some(item => item.arguments.provider?.action === 'start'), false);
  assert.equal(JSON.parse(f.stores.get('amita_screen_share_runtime_v1').get('approvals'))[0].state, 'revoked');
});

test('cold attempted stream becomes unknown and the original record is never replayed', async () => {
  const f = await fixture(), pending = await f.start();
  await f.runtime().decide(pending, true); await until(() => f.starts() === 1);
  const receipt = f.records()[0].receipt_id, invocation = f.records()[0].invocation_nonce;
  await f.reload(); assert.equal(f.records()[0].state, 'unknown'); assert.equal(f.runtime().pending(), null);
  await f.runtime().receive(); await settle();
  assert.equal(f.starts(), 1); assert.equal(f.records()[0].receipt_id, receipt);
  assert.equal(f.records()[0].invocation_nonce, invocation); assert.equal(f.records()[0].result_json, '');
  assert.equal(f.requests.filter(item => item.arguments.provider?.action === 'start').length, 1);
});

const appTarget = { bundle_name: 'com.tencent.wechat', ability_name: 'EntryAbility' };

test('launch uses only the approved target after real frame and ticket, then independently requires a later frame ACK', async () => {
  const f = await fixture({ target: appTarget }), pending = await f.start();
  assert.deepEqual(pending.target, appTarget);
  await assert.rejects(f.runtime().decide({ ...pending, target: { ...appTarget, ability_name: 'Different' } }, true),
    /decision_mismatch/);
  await f.runtime().decide(pending, true); await until(() => f.starts() === 1);
  f.markCaptureWithoutTicket(); await f.advance(250); assert.deepEqual(f.launches, []);
  f.markActualFrameAndTicket(); await f.advance(250);
  assert.deepEqual(f.launches, [{ bundleName: appTarget.bundle_name, abilityName: appTarget.ability_name }]);
  f.markInFlightFrame(2); f.setForeground(false); f.resolveLaunch(); await settle();
  assert.equal(f.runtime().status().launchStatus, 'system_acknowledged');
  f.markActualFrameAndTicket(2); await f.advance(250);
  assert.equal(f.runtime().status().laterFrameAcknowledged, false, 'an already in-flight frame cannot supply later proof');
  f.markActualFrameAndTicket(3); await f.advance(250);
  assert.equal(f.runtime().status().laterFrameAcknowledged, true);
  assert.equal(f.records()[0].result_json, '', 'even the later frame does not settle capture');
  f.setForeground(true); f.allowClose(); await f.runtime().stop(); await f.advance(250);
  await until(() => f.records()[0].state === 'settled');
  assert.equal(f.launches.length, 1);
});

test('a disabled adapter and a declined on-device launch request never admit the launch', async () => {
  const disabled = await fixture({ target: appTarget, launchEnabled: false });
  await disabled.runtime().receive(); await settle();
  assert.equal(disabled.runtime().pending(), null); assert.equal(disabled.records().length, 0);
  const declined = await fixture({ target: appTarget }), pending = await declined.start();
  await declined.runtime().decide(pending, false); await settle();
  assert.equal(declined.starts(), 0); assert.equal(declined.launches.length, 0);
  assert.equal(declined.requests.some(item => item.arguments.provider?.action === 'accept'), false);
});

test('restart never invokes an attempted app launch again', async () => {
  const f = await fixture({ target: appTarget }), pending = await f.start();
  await f.runtime().decide(pending, true); await until(() => f.starts() === 1);
  f.markActualFrameAndTicket(); await f.advance(250); assert.equal(f.launches.length, 1);
  await f.reload(); await f.runtime().receive(); await settle();
  assert.equal(f.records()[0].state, 'unknown'); assert.equal(f.runtime().pending(), null);
  assert.equal(f.starts(), 1); assert.equal(f.launches.length, 1);
});
