import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { stripTypeScriptTypes } from 'node:module';
import { createHash } from 'node:crypto';
import * as policy from './ScreenSharePolicy.ts';

const digest = async bytes => createHash('sha256').update(bytes).digest('hex');
const rawJpeg = () => new Uint8Array([255, 216, 255, 0, 255, 217]);
const frame = revision => ({ bytes: rawJpeg(), timestamp: 1234 + revision, width: 640, height: 320, revision });
async function settle() { for (let turn = 0; turn < 20; turn++) await Promise.resolve(); }

// Actual sink code, fake Asset/HTTPS boundaries, fake clock. No credentials,
// native capture, network/socket, Source DB, SDK or device is accessed here.
function fixture() {
  let now = 1000000, active = true, service = true, nextTimer = 0, failFrame = false, wrongAck = false;
  const timers = new Map(), requests = [], latest = new Map(); let destroys = 0, credentialLoads = 0;
  class Encoder { encodeInto(value) { return new TextEncoder().encode(value); } }
  class Decoder { decodeToString(value) { return new TextDecoder('utf-8', { fatal: true }).decode(value); } }
  class Base64 { encodeToStringSync(bytes) { return Buffer.from(bytes).toString('base64'); }
    decodeSync(raw) { return new Uint8Array(Buffer.from(raw, 'base64')); } }
  class Credentials { async load() { credentialLoads++; return { nodeId: 'phone', token: 'ASSET_ONLY_SYNTHETIC_TOKEN', scope: ['phone:node'] }; } }
  const config = { format: 'mesh-phone-client/1', resource_url: 'https://original.example.test/phone/node',
    node_id: 'phone', session_id: 'actual-primary', mesh_id: 'mesh', authority_id: 'authority' };
  const workload = Buffer.from(JSON.stringify({ format: 'amita-phone-screen-share/1', command: 'screen_share',
    duration_ms: 60000, max_fps: 2 }));
  const dispatch = { operation_id: 'original-op', capability_id: 'phone:ui-control-v1', provider: 'node:phone',
    state: 'pending', epoch: 1, receipt_id: null, task_id: 'original-task', task_epoch: 7, deadline: 1060,
    selection: { capability_id: 'phone:ui-control-v1', epoch: 2, action: 'inspect', scope: { original: true },
      workload: { input: { bytes: workload.length, sha256: createHash('sha256').update(workload).digest('hex'),
        base64: workload.toString('base64') } } } };
  const record = { operation_id: dispatch.operation_id, capability_id: dispatch.capability_id,
    task_id: dispatch.task_id, task_epoch: 7, dispatch_json: JSON.stringify(dispatch), receipt_id: 'original-provider-receipt',
    state: 'attempted', authority_epoch: 3, invocation_nonce: 'LOCAL_INVOCATION_DO_NOT_SEND',
    result_json: '', result_reference: '', last_remote_state: 'running', error: '', permission_context: 'LOCAL_PERMISSION' };
  const localScope = { sessionId: 'LOCAL_SESSION_DO_NOT_SEND', taskId: 'original-task', taskEpoch: 7,
    operationId: 'original-op', approvalId: 'LOCAL_APPROVAL_DO_NOT_SEND', nonce: 'LOCAL_NONCE_DO_NOT_SEND', revision: 1, expiresAt: 1060000 };
  const gate = { valid: () => active, serviceLive: () => service };
  const http = { RequestMethod: { POST: 'POST' }, HttpProtocol: { HTTP1_1: 0 }, HttpDataType: { STRING: 1 },
    createHttp: () => ({ destroy: () => { destroys++; }, request: async (url, options) => {
      const envelope = JSON.parse(options.extraData), args = envelope.arguments;
      requests.push({ url, options, envelope, at: now });
      if (envelope.action === 'mesh_screen_share_frame' && failFrame) throw new Error('private daemon body');
      if (envelope.action === 'mesh_screen_share_frame') latest.set(args.stream_id, args.frame);
      if (envelope.action === 'mesh_screen_share_close') latest.delete(args.stream_id);
      const item = latest.get(args.stream_id);
      const reply = { format: 'amita-phone-screen-share-ack/1', stream_id: args.stream_id, task_id: args.task_id,
        task_epoch: args.task_epoch, operation_id: args.operation_id, capability_id: args.capability_id,
        binding_digest: wrongAck ? '0'.repeat(64) : args.binding_digest, expires_at: args.expires_at,
        active: envelope.action !== 'mesh_screen_share_close', acknowledged: Boolean(item),
        frame_revision: item?.revision ?? 0, frame_sha256: item?.sha256 ?? '', bytes: item?.bytes ?? 0 };
      return { responseCode: 200, result: JSON.stringify(reply) };
    } }) };
  const source = stripTypeScriptTypes(readFileSync(new URL('./PhoneScreenShareSink.ets', import.meta.url), 'utf8')
    .replace(/^import[\s\S]*?;\r?\n/gm, ''), { mode: 'strip' }).replace(/^export /gm, '');
  const exported = Object.keys(policy);
  const timer = (callback, delay, repeat = false) => { const id = ++nextTimer; timers.set(id, { callback, at: now + delay, delay, repeat }); return id; };
  const clear = id => timers.delete(id);
  const Sink = new Function(...exported, 'deviceInfo', 'http', 'util', 'NodeCredentialStore', 'controlDigest',
    'validateWorkConfig', 'validateWorkBlob', 'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date',
    source + '\nreturn PhoneScreenShareSink;')(...exported.map(name => policy[name]), { sdkApiVersion: 26 }, http,
    { TextEncoder: Encoder, TextDecoder: Decoder, Base64Helper: Base64 }, Credentials, digest,
    value => assert.equal(value.resource_url, config.resource_url), value => assert.equal(typeof value.base64, 'string'),
    (callback, delay) => timer(callback, delay), clear, (callback, delay) => timer(callback, delay, true), clear, { now: () => now });
  const sink = new Sink(config, 'actual-control', record, localScope, gate);
  return { sink, record, localScope, requests, timers,
    revoke: () => { active = false; }, serviceOff: () => { service = false; },
    failFrames: () => { failFrame = true; }, wrongAcks: () => { wrongAck = true; },
    destroys: () => destroys, credentialLoads: () => credentialLoads,
    advance: async milliseconds => {
      now += milliseconds;
      for (const [id, item] of [...timers]) {
        if (item.at <= now) { if (item.repeat) item.at = now + item.delay; else timers.delete(id); item.callback(); }
      }
      await settle();
    } };
}

test('real verified frame ACK uses pinned Asset HTTPS route and keeps local approval/invocation data off wire', async () => {
  const f = fixture(); assert.equal(f.requests.length, 0); await f.sink.open();
  assert.equal(f.sink.status().lastAcknowledgedSha256, '');
  assert.equal(f.sink.isLive(f.localScope), true);
  const original = frame(1), expected = Buffer.from(original.bytes);
  const pending = f.sink.sendFrame(f.localScope, original); original.bytes.fill(0);
  const ack = await pending;
  assert.deepEqual(ack.scope, f.localScope); assert.equal(ack.frameRevision, 1); assert.equal(ack.acknowledged, true);
  const posted = f.requests[1];
  assert.equal(posted.url, 'https://original.example.test/phone/node');
  assert.equal(posted.options.header.Authorization, 'Bearer ASSET_ONLY_SYNTHETIC_TOKEN');
  assert.equal(posted.options.maxRedirects, 0); assert.equal(posted.options.usingProtocol, 0);
  assert.equal(posted.options.usingCache, false); assert.equal(posted.options.maxLimit, 65536);
  assert.deepEqual(Buffer.from(posted.envelope.arguments.frame.base64, 'base64'), expected);
  assert.equal(f.sink.status().lastAcknowledgedSha256, posted.envelope.arguments.frame.sha256);
  for (const request of f.requests) {
    assert.equal(request.options.extraData.includes('LOCAL_'), false);
    assert.equal(request.options.extraData.includes('approvalId'), false);
    assert.equal(request.options.extraData.includes('nonce'), false);
    assert.equal(Buffer.byteLength(request.options.extraData) <= 65536, true);
  }
  await f.sink.close(); await settle();
  assert.equal(f.requests.at(-1).envelope.action, 'mesh_screen_share_close');
  assert.equal(f.timers.size, 0); assert.equal(f.destroys() >= f.requests.length, true);
});

test('rate wait precedes next POST and revocation cancels delayed frame instead of returning a fake ACK', async () => {
  const f = fixture(); await f.sink.open(); await f.sink.sendFrame(f.localScope, frame(1));
  let resolved = false;
  const next = f.sink.sendFrame(f.localScope, frame(2)).then(value => { resolved = true; return value; });
  await settle(); assert.equal(resolved, false); assert.equal(f.requests.length, 2);
  await f.advance(499); assert.equal(f.requests.length, 2);
  await f.advance(1); assert.equal((await next).frameRevision, 2);
  const delayed = f.sink.sendFrame(f.localScope, frame(3));
  const rejected = assert.rejects(delayed, /frame_ack_unknown/);
  await settle(); f.revoke(); f.sink.detach(f.localScope); await rejected;
  assert.equal(f.requests.length, 3); assert.equal(f.sink.isLive(f.localScope), false);
  assert.equal(f.timers.size, 0);
});

test('unknown or mismatched frame ACK disconnects once and never retries original bytes', async () => {
  for (const wrong of [false, true]) {
    const f = fixture(); await f.sink.open(); let disconnected = 0;
    f.sink.onDisconnect(() => { disconnected++; });
    if (wrong) f.wrongAcks(); else f.failFrames();
    await assert.rejects(f.sink.sendFrame(f.localScope, frame(1)), /frame_ack_unknown/);
    const count = f.requests.length;
    await f.advance(30000);
    assert.equal(f.requests.length, count); assert.equal(disconnected, 1);
    assert.equal(f.sink.isLive(f.localScope), false);
    await assert.rejects(f.sink.open(), /already_attempted/);
    assert.equal(f.sink.status().error, 'screen_share_sink_frame_ack_unknown');
  }
});

test('static capture uses authenticated status only; original record withdrawal stops it', async () => {
  const f = fixture(); await f.sink.open(); await f.sink.sendFrame(f.localScope, frame(1));
  await f.advance(10000);
  assert.equal(f.requests.at(-1).envelope.action, 'mesh_screen_share_status');
  assert.equal(f.requests.filter(request => request.envelope.action === 'mesh_screen_share_frame').length, 1);
  assert.equal(f.sink.isLive(f.localScope), true);
  f.record.state = 'unknown'; await f.advance(1000);
  assert.equal(f.sink.isLive(f.localScope), false);
  assert.equal(f.sink.status().error, 'screen_share_sink_scope_or_service_lost');
  assert.equal(f.timers.size, 0);
});
