import { deviceInfo } from '@kit.BasicServicesKit';
import { http } from '@kit.NetworkKit';
import { util } from '@kit.ArkTS';
import { ProviderDispatch, WorkConfig, WorkRecord, WorkSelection, validateWorkBlob, validateWorkConfig } from '../core/WorkProtocol';
import { Blob } from '../core/Protocol';
import { NodeCredential, NodeCredentialStore } from '../security/NodeCredentialStore';
import { controlDigest } from '../control/PhoneControlJSON';
import { ScreenShareFrameSink } from './ScreenShareSession';
import { ScreenShareScope, ScreenShareFrame, ScreenShareFrameAck,
  copyScreenShareScope, sameScreenShareScope, validScreenShareScope } from './ScreenSharePolicy';

interface StreamPolicy { format: string; command: string; duration_ms: number; max_fps: number; }
interface WireBinding {
  primary_session_id: string; session_id: string; stream_id: string; task_id: string; task_epoch: number;
  operation_id: string; capability_id: string; binding_digest: string; expires_at: number;
}
interface WireFrame {
  revision: number; capture_timestamp: number; width: number; height: number;
  bytes: number; sha256: string; base64: string;
}
interface WireArguments extends WireBinding { frame?: WireFrame; }
interface WireAck {
  format: string; stream_id: string; task_id: string; task_epoch: number; operation_id: string;
  capability_id: string; binding_digest: string; expires_at: number; active: boolean;
  acknowledged: boolean; frame_revision: number; frame_sha256: string; bytes: number;
}

// These synchronous predicates read the ORIGINAL trusted WorkEngine/owner
// runtime. They do not issue presence, renew leases, enable capture or approve.
export interface PhoneScreenShareGate {
  valid: (scope: ScreenShareScope) => boolean;
  serviceLive: () => boolean;
}
export interface PhoneScreenShareSinkStatus {
  state: string; error: string; lastFrameRevision: number; expiresAt: number;
}

function requireSink(value: boolean, code: string): void { if (!value) { throw new Error(code); } }
function publicName(value: string): boolean { return typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,199}$/.test(value); }

export class PhoneScreenShareSink implements ScreenShareFrameSink {
  private config: WorkConfig;
  private controlSession: string;
  private record: WorkRecord;
  private dispatch: ProviderDispatch;
  private originalDispatchJson: string;
  private originalReceipt: string;
  private originalInvocation: string;
  private originalAuthorityEpoch: number;
  private localScope: ScreenShareScope;
  private gate: PhoneScreenShareGate;
  private credentials: NodeCredentialStore = new NodeCredentialStore();
  private binding: WireBinding | null = null;
  private policy: StreamPolicy | null = null;
  private state: string = 'idle';
  private error: string = '';
  private generation: number = 0;
  private openAttempted: boolean = false;
  private closeAttempted: boolean = false;
  private listeners: (() => void)[] = [];
  private pollTimer: number = -1;
  private lastContact: number = 0;
  private lastAckAt: number = 0;
  private lastRevision: number = 0;
  private lastTimestamp: number = -1;
  private lastHash: string = '';
  private lastBytes: number = 0;
  private frameBusy: boolean = false;
  private httpBusy: boolean = false;
  private cancelHttp: (() => void) | null = null;
  private cancelDelay: (() => void) | null = null;
  private idlePromise: Promise<void> | null = null;
  private releaseIdle: (() => void) | null = null;

  // Only a trusted adapter passes pinned WorkConfig and the actual durable
  // attempted record. No URL, bearer, workload policy or approval arrives via
  // the frame/scope wire. Capture-local approvalId/nonce are hashed, never sent.
  constructor(config: WorkConfig, controlSession: string, record: WorkRecord,
    scope: ScreenShareScope, gate: PhoneScreenShareGate) {
    validateWorkConfig(config, config.node_id);
    requireSink(deviceInfo.sdkApiVersion >= 23 && publicName(controlSession) && controlSession !== config.session_id,
      'screen_share_sink_pinned_transport_required');
    this.config = { format: config.format, resource_url: config.resource_url, node_id: config.node_id,
      session_id: config.session_id, mesh_id: config.mesh_id, authority_id: config.authority_id };
    this.controlSession = controlSession; this.record = record; this.gate = gate;
    this.localScope = copyScreenShareScope(scope);
    this.originalDispatchJson = record.dispatch_json; this.originalReceipt = record.receipt_id;
    this.originalInvocation = record.invocation_nonce; this.originalAuthorityEpoch = record.authority_epoch;
    try { this.dispatch = JSON.parse(record.dispatch_json) as ProviderDispatch; }
    catch (_) { throw new Error('screen_share_sink_original_dispatch_required'); }
    this.requireOriginal();
  }

  private originalValid(): boolean {
    const row: WorkRecord = this.record;
    const dispatch: ProviderDispatch = this.dispatch;
    const selection: WorkSelection | undefined = dispatch.selection;
    return row.dispatch_json === this.originalDispatchJson && row.operation_id === dispatch.operation_id &&
      row.capability_id === dispatch.capability_id && row.task_id === dispatch.task_id && row.task_epoch === dispatch.task_epoch &&
      row.receipt_id === this.originalReceipt && publicName(row.receipt_id) &&
      row.invocation_nonce === this.originalInvocation && publicName(row.invocation_nonce) &&
      row.authority_epoch === this.originalAuthorityEpoch && Number.isSafeInteger(row.authority_epoch) &&
      Number.isSafeInteger(dispatch.epoch) && dispatch.epoch > 0 && row.authority_epoch >= dispatch.epoch + 2 &&
      row.state === 'attempted' && row.result_json === '' && row.result_reference === '' &&
      dispatch.provider === 'node:' + this.config.node_id && dispatch.capability_id === this.config.node_id + ':ui-control-v1' &&
      Number.isFinite(dispatch.deadline) && (dispatch.deadline as number) * 1000 > Date.now() &&
      selection !== undefined && selection.action === 'inspect' && selection.capability_id === dispatch.capability_id &&
      Number.isSafeInteger(selection.epoch) && selection.epoch > 0 &&
      selection.workload !== null && Object.keys(selection.workload).join('|') === 'input' &&
      this.localScope.taskId === row.task_id && this.localScope.taskEpoch === row.task_epoch &&
      this.localScope.operationId === row.operation_id && validScreenShareScope(this.localScope, Date.now());
  }

  private requireOriginal(): void {
    let valid: boolean = false;
    try { valid = this.originalValid(); } catch (_) {}
    requireSink(valid && publicName(this.localScope.taskId) && publicName(this.localScope.operationId),
      'screen_share_sink_original_dispatch_required');
  }

  private authorised(): boolean {
    try {
      return this.originalValid() && this.gate.valid(copyScreenShareScope(this.localScope)) && this.gate.serviceLive();
    } catch (_) { return false; }
  }

  private requireCurrent(generation: number): void {
    requireSink(generation === this.generation && this.authorised() && this.state !== 'closed' && this.state !== 'unknown' &&
      (this.binding === null || this.binding.expires_at * 1000 > Date.now()), 'screen_share_sink_scope_lost');
  }

  async open(): Promise<void> {
    requireSink(!this.openAttempted && this.state === 'idle', 'screen_share_sink_open_already_attempted');
    this.openAttempted = true; this.state = 'opening';
    const generation: number = this.generation;
    try {
      this.requireCurrent(generation);
      const input: Blob | undefined = this.dispatch.selection?.workload.input;
      requireSink(input !== undefined, 'screen_share_sink_stream_workload_required');
      validateWorkBlob(input as Blob);
      const codec: util.Base64Helper = new util.Base64Helper();
      const bytes: Uint8Array = codec.decodeSync((input as Blob).base64);
      requireSink(bytes.length > 0 && bytes.length <= 4096 && bytes.length === input?.bytes &&
        codec.encodeToStringSync(bytes) === input?.base64 && await controlDigest(bytes) === input?.sha256,
      'screen_share_sink_stream_workload_required');
      const policy: StreamPolicy = JSON.parse(new util.TextDecoder('utf-8', { fatal: true }).decodeToString(bytes)) as StreamPolicy;
      requireSink(Object.keys(policy).sort().join('|') === 'command|duration_ms|format|max_fps' &&
        policy.format === 'amita-phone-screen-share/1' && policy.command === 'screen_share' &&
        Number.isSafeInteger(policy.duration_ms) && policy.duration_ms >= 1000 && policy.duration_ms <= 300000 &&
        Number.isSafeInteger(policy.max_fps) && policy.max_fps >= 1 && policy.max_fps <= 2,
      'screen_share_sink_stream_workload_required');
      this.policy = policy;
      const expires: number = Math.min(this.localScope.expiresAt, (this.dispatch.deadline as number) * 1000,
        Date.now() + policy.duration_ms) / 1000;
      const digest: string = await controlDigest(new util.TextEncoder().encodeInto(JSON.stringify([
        'amita-phone-screen-share-binding/1', this.config.node_id, this.config.mesh_id, this.config.authority_id,
        this.config.session_id, this.controlSession, this.originalReceipt, this.originalInvocation,
        input?.sha256, expires, copyScreenShareScope(this.localScope)])));
      this.requireCurrent(generation);
      this.binding = { primary_session_id: this.config.session_id, session_id: this.controlSession,
        stream_id: 'stream-' + digest.slice(0, 40), task_id: this.record.task_id, task_epoch: this.record.task_epoch,
        operation_id: this.record.operation_id, capability_id: this.record.capability_id,
        binding_digest: digest, expires_at: expires };
      const reply: WireAck = await this.post('mesh_screen_share_open', this.arguments(), generation);
      this.verify(reply, false, 0, '', 0);
      this.requireCurrent(generation);
      this.state = 'attached'; this.lastContact = Date.now();
      this.pollTimer = setInterval((): void => { this.poll(); }, 1000);
    } catch (_) {
      this.fail('screen_share_sink_open_unconfirmed');
      throw new Error('screen_share_sink_open_unconfirmed');
    }
  }

  private arguments(frame?: WireFrame): WireArguments {
    const binding: WireBinding = this.binding as WireBinding;
    const result: WireArguments = { primary_session_id: binding.primary_session_id, session_id: binding.session_id,
      stream_id: binding.stream_id, task_id: binding.task_id, task_epoch: binding.task_epoch,
      operation_id: binding.operation_id, capability_id: binding.capability_id,
      binding_digest: binding.binding_digest, expires_at: binding.expires_at };
    if (frame !== undefined) { result.frame = frame; }
    return result;
  }

  private verify(reply: WireAck, acknowledged: boolean, revision: number, hash: string, bytes: number,
    active: boolean = true): void {
    const binding: WireBinding = this.binding as WireBinding;
    requireSink(reply !== null && typeof reply === 'object' && !Array.isArray(reply) &&
      Object.keys(reply).sort().join('|') ===
      'acknowledged|active|binding_digest|bytes|capability_id|expires_at|format|frame_revision|frame_sha256|operation_id|stream_id|task_epoch|task_id' &&
      reply.format === 'amita-phone-screen-share-ack/1' && reply.stream_id === binding.stream_id &&
      reply.task_id === binding.task_id && reply.task_epoch === binding.task_epoch && reply.operation_id === binding.operation_id &&
      reply.capability_id === binding.capability_id && reply.binding_digest === binding.binding_digest &&
      reply.expires_at === binding.expires_at && reply.active === active && reply.acknowledged === acknowledged &&
      reply.frame_revision === revision && reply.frame_sha256 === hash && reply.bytes === bytes,
    'screen_share_sink_ack_binding_unconfirmed');
  }

  isLive(scope: ScreenShareScope): boolean {
    return sameScreenShareScope(scope, this.localScope) && this.state === 'attached' && this.authorised() &&
      this.binding !== null && Date.now() < this.binding.expires_at * 1000 && Date.now() - this.lastContact < 25000;
  }

  onDisconnect(listener: () => void): () => void {
    requireSink(this.listeners.length < 4, 'screen_share_sink_listener_budget');
    this.listeners.push(listener);
    if (this.state === 'closed' || this.state === 'unknown') { listener(); }
    return (): void => { this.listeners = this.listeners.filter((item: () => void): boolean => item !== listener); };
  }

  private async delay(generation: number): Promise<void> {
    const wait: number = Math.max(0, this.lastAckAt + Math.ceil(1000 / (this.policy as StreamPolicy).max_fps) - Date.now());
    if (wait > 0) {
      await new Promise<void>((resolve, reject): void => {
        const timer: number = setTimeout((): void => { this.cancelDelay = null; resolve(); }, wait);
        this.cancelDelay = (): void => { clearTimeout(timer); this.cancelDelay = null; reject(new Error('screen_share_sink_canceled')); };
      });
    }
    this.requireCurrent(generation);
  }

  async sendFrame(scope: ScreenShareScope, frame: ScreenShareFrame): Promise<ScreenShareFrameAck> {
    requireSink(this.isLive(scope) && !this.frameBusy, 'screen_share_sink_not_live_or_busy');
    this.frameBusy = true;
    const generation: number = this.generation;
    try {
      requireSink(frame.bytes instanceof Uint8Array && frame.bytes.length >= 4 && frame.bytes.length <= 42000 && frame.bytes[0] === 255 &&
        frame.bytes[1] === 216 && frame.bytes[2] === 255 && frame.bytes[frame.bytes.length - 2] === 255 &&
        frame.bytes[frame.bytes.length - 1] === 217 && Number.isSafeInteger(frame.revision) &&
        frame.revision > this.lastRevision && Number.isFinite(frame.timestamp) && frame.timestamp >= 0 &&
        frame.timestamp >= this.lastTimestamp && Number.isSafeInteger(frame.width) && frame.width > 0 && frame.width <= 640 &&
        Number.isSafeInteger(frame.height) && frame.height > 0 && frame.height <= 640, 'screen_share_sink_frame_invalid');
      const bytes: Uint8Array = new Uint8Array(frame.bytes);
      const revision: number = frame.revision;
      const timestamp: number = frame.timestamp;
      const payload: WireFrame = { revision: revision, capture_timestamp: timestamp, width: frame.width, height: frame.height,
        bytes: bytes.length, sha256: await controlDigest(bytes), base64: new util.Base64Helper().encodeToStringSync(bytes) };
      await this.delay(generation);
      const pending: Promise<void> | null = this.idlePromise;
      if (pending !== null) { await pending; }
      this.requireCurrent(generation);
      const reply: WireAck = await this.post('mesh_screen_share_frame', this.arguments(payload), generation);
      this.verify(reply, true, revision, payload.sha256, bytes.length);
      this.requireCurrent(generation);
      this.lastRevision = revision; this.lastTimestamp = timestamp; this.lastHash = payload.sha256; this.lastBytes = bytes.length;
      this.lastAckAt = Date.now(); this.lastContact = this.lastAckAt;
      // Constructed from captured LOCAL scope only after exact remote ACK.
      return { scope: copyScreenShareScope(this.localScope), frameRevision: revision, bytes: bytes.length, acknowledged: true };
    } catch (_) {
      this.fail('screen_share_sink_frame_ack_unknown');
      throw new Error('screen_share_sink_frame_ack_unknown');
    } finally { this.frameBusy = false; }
  }

  private async poll(): Promise<void> {
    if (this.state !== 'attached') { return; }
    if (!this.isLive(this.localScope)) { this.fail('screen_share_sink_scope_or_service_lost'); return; }
    if (this.httpBusy || this.frameBusy || Date.now() - this.lastContact < 10000) { return; }
    const generation: number = this.generation;
    try {
      const reply: WireAck = await this.post('mesh_screen_share_status', this.arguments(), generation);
      this.verify(reply, this.lastRevision > 0, this.lastRevision, this.lastHash, this.lastBytes);
      this.requireCurrent(generation); this.lastContact = Date.now();
    } catch (_) { this.fail('screen_share_sink_status_unknown'); }
  }

  private async post(action: string, args: WireArguments, generation: number, closing: boolean = false): Promise<WireAck> {
    requireSink(!this.httpBusy, 'screen_share_sink_transport_busy');
    const body: string = JSON.stringify({ action: action, arguments: args });
    requireSink(new util.TextEncoder().encodeInto(body).length <= 65536, 'screen_share_sink_json_budget');
    this.httpBusy = true;
    this.idlePromise = new Promise<void>((resolve): void => { this.releaseIdle = resolve; });
    try {
      return await new Promise<WireAck>((resolve, reject): void => {
        let finished: boolean = false;
        let request: http.HttpRequest | null = null;
        const destroy = (): void => { if (request !== null) { try { request.destroy(); } catch (_) {} } };
        const cancel = (): void => {
          if (finished) { return; }
          finished = true; clearTimeout(timer); destroy(); reject(new Error('screen_share_sink_transport_unknown'));
        };
        this.cancelHttp = cancel;
        const timer: number = setTimeout(cancel, 8000);
        const exchange = async (): Promise<WireAck> => {
          const credential: NodeCredential | null = await this.credentials.load();
          requireSink(!finished && generation === this.generation && (closing || this.authorised()), 'screen_share_sink_scope_lost');
          if (!closing) { this.requireCurrent(generation); }
          requireSink(credential !== null && credential.nodeId === this.config.node_id && credential.scope !== undefined &&
            credential.scope.includes('phone:node') && (credential.expiresAt === undefined || credential.expiresAt * 1000 > Date.now()),
          'screen_share_sink_node_credential_required');
          request = http.createHttp();
          const response: http.HttpResponse = await request.request(this.config.resource_url, {
            method: http.RequestMethod.POST, usingProtocol: http.HttpProtocol.HTTP1_1,
            header: { 'Authorization': 'Bearer ' + (credential as NodeCredential).token,
              'Content-Type': 'application/json', 'Accept': 'application/json' },
            extraData: body, expectDataType: http.HttpDataType.STRING, usingCache: false, maxRedirects: 0,
            connectTimeout: 5000, readTimeout: 5000, maxLimit: 65536 });
          requireSink(!finished && generation === this.generation && response.responseCode === 200 &&
            typeof response.result === 'string' && new util.TextEncoder().encodeInto(response.result as string).length <= 65536,
          'screen_share_sink_authority_unconfirmed');
          return JSON.parse(response.result as string) as WireAck;
        };
        exchange().then((reply: WireAck): void => {
          if (finished) { return; } finished = true; resolve(reply);
        }).catch((_error: Error): void => {
          if (finished) { return; } finished = true; reject(new Error('screen_share_sink_transport_unknown'));
        }).finally((): void => { clearTimeout(timer); destroy(); if (this.cancelHttp === cancel) { this.cancelHttp = null; } });
      });
    } finally {
      this.httpBusy = false;
      if (this.releaseIdle !== null) { this.releaseIdle(); }
      this.releaseIdle = null; this.idlePromise = null;
    }
  }

  private fail(code: string): void {
    if (this.state === 'unknown' || this.state === 'closed') { return; }
    this.state = 'unknown'; this.error = code; this.generation += 1; this.cancel();
    const listeners: (() => void)[] = this.listeners.slice();
    for (const listener of listeners) { try { listener(); } catch (_) {} }
  }

  private cancel(): void {
    clearInterval(this.pollTimer); this.pollTimer = -1;
    if (this.cancelDelay !== null) { this.cancelDelay(); }
    if (this.cancelHttp !== null) { this.cancelHttp(); this.cancelHttp = null; }
  }

  detach(scope: ScreenShareScope): void {
    requireSink(sameScreenShareScope(scope, this.localScope), 'screen_share_sink_scope_mismatch');
    if (this.state === 'closed') { return; }
    this.state = 'closed'; this.generation += 1; this.cancel();
    for (const listener of this.listeners.slice()) { try { listener(); } catch (_) {} }
  }

  // Root awaits this ONCE during explicit session finalization. detach() alone
  // cancels local traffic; it never hides an unknown close or creates retries.
  async close(): Promise<void> {
    requireSink(!this.closeAttempted, 'screen_share_sink_close_already_attempted');
    this.closeAttempted = true;
    this.detach(this.localScope);
    if (this.binding === null) { return; }
    const generation: number = this.generation;
    try {
      const pending: Promise<void> | null = this.idlePromise;
      if (pending !== null) { await pending; }
      const reply: WireAck = await this.post('mesh_screen_share_close', this.arguments(), generation, true);
      this.verify(reply, false, 0, '', 0, false);
    } catch (_) {
      this.error = 'screen_share_sink_close_unknown';
      throw new Error('screen_share_sink_close_unknown');
    }
  }

  status(): PhoneScreenShareSinkStatus {
    return { state: this.state, error: this.error, lastFrameRevision: this.lastRevision,
      expiresAt: this.binding === null ? 0 : this.binding.expires_at * 1000 };
  }
}
