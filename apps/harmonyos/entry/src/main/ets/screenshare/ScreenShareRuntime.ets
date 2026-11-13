import { common } from '@kit.AbilityKit';
import { preferences } from '@kit.ArkData';
import { deviceInfo } from '@kit.BasicServicesKit';
import { http } from '@kit.NetworkKit';
import { util } from '@kit.ArkTS';
import { Blob, requireValue } from '../core/Protocol';
import { PhoneResponse, ProviderDispatch, SessionView, WorkConfig, WorkEngine, WorkHost,
  WorkRecord, WorkSnapshot, coldResumeWork, freshWorkSnapshot, validateWorkBlob,
  validateWorkConfig, validateWorkDispatch } from '../core/WorkProtocol';
import { NodeCredential, NodeCredentialStore } from '../security/NodeCredentialStore';
import { TrustedRuntimeConfig } from '../update/TrustedRuntimeConfig';
import { controlBlob, controlDigest } from '../control/PhoneControlJSON';
import { ScreenShareOwner } from './ScreenShareOwner';
import { ScreenShareAuthorization, ScreenShareSessionStatus } from './ScreenShareSession';
import { ScreenShareScope, sameScreenShareScope } from './ScreenSharePolicy';
import { PhoneScreenShareGate } from './PhoneScreenShareSink';

interface NodeIdentity { node_id: string; }
interface RuntimeIdentity { format: string; node_id: string; control_session_id: string; }
interface StreamInput { format: string; command: string; duration_ms: number; max_fps: number; }
interface SourceAdapter {
  enabled: boolean; screen_share_enabled: boolean; capability_id: string;
  epoch: number; execution_verification: string;
}
interface SourceView extends SessionView { lifecycle: string; installed_adapter: SourceAdapter; }
interface AckStatus extends ScreenShareSessionStatus { lastAcknowledgedSha256: string; }
interface PurposeScope { purpose?: string; }
interface ControlPresenceArguments { online?: boolean; }

// Only local UI approval metadata. The original dispatch/recipe remains in the
// existing WorkEngine WorkRecord, not in a second task/allocation ledger.
interface LocalApproval {
  format: string; approvalId: string; nonce: string; revision: number; state: string;
  operationId: string; capabilityId: string; taskId: string; taskEpoch: number;
  inputHash: string; capabilityEpoch: number; dispatchEpoch: number;
  requestedDurationMs: number; maxFps: number; purpose: string;
  expiresAt: number; allocationDeadline: number; scopeExpiresAt: number;
  approvedAt: number; permissionContext: string; streamSessionId: string;
  receiptId: string; invocationNonce: string; authorityEpoch: number;
}
export interface ScreenSharePending {
  approvalId: string; nonce: string; revision: number; operationId: string;
  taskId: string; taskEpoch: number; inputHash: string; expiresAt: number;
  allocationDeadline: number; durationMs: number; maxFps: number; purpose: string;
}
export class ScreenShareRuntimeStatus {
  state: string = 'unconfigured';
  error: string = '';
  nodeId: string = '';
  controlSessionId: string = '';
  identityObservation: string = 'declared_app_component';
  receiving: boolean = false;
  sourceInstalled: boolean = false;
  capabilityEpoch: number = 0;
  presenceExpiresAt: number = 0;
  foreground: boolean = false;
  captureActive: boolean = false;
  backgroundActive: boolean = false;
  acknowledgedFrames: number = 0;
  pending: boolean = false;
  executing: boolean = false;
  taskId: string = '';
  taskEpoch: number = 0;
  operationId: string = '';
  unknownRecords: number = 0;
  pendingResults: number = 0;
  settledRecords: number = 0;
  cleanupUnknown: boolean = false;
  sourceCloseUnknown: boolean = false;
}
interface StreamSummary {
  format: string; command: string; readonly: boolean; operation_id: string;
  task_id: string; task_epoch: number; stop_reason: string;
  acknowledged_frames: number; last_frame_revision: number;
  last_ack_sha256: string; source_closed: boolean; capture_quiescent: boolean;
}

function freshPreferences(context: common.Context, name: string): preferences.Preferences {
  preferences.removePreferencesFromCacheSync(context, name);
  return preferences.getPreferencesSync(context, { name: name });
}
function safeCode(value: string): string {
  return /^screen_share_[a-z_]{1,72}$/.test(value) ? value : 'screen_share_runtime_unconfirmed';
}
function sameConfig(left: WorkConfig, right: WorkConfig): boolean {
  return left.node_id === right.node_id && left.session_id === right.session_id &&
    left.mesh_id === right.mesh_id && left.authority_id === right.authority_id &&
    left.resource_url === right.resource_url && left.format === right.format;
}

// Retained by EntryAbility, never by a page. Read-only video sharing is an app
// component with its own DECLARED presence; no accessibility/root/input claims.
export class ScreenShareRuntime implements WorkHost {
  private static instance: ScreenShareRuntime | null = null;
  private static creating: Promise<ScreenShareRuntime> | null = null;
  private static creatingContext: common.UIAbilityContext | null = null;
  private context: common.UIAbilityContext;
  private nodeId: string;
  private identity: RuntimeIdentity;
  private store: preferences.Preferences;
  private credentials: NodeCredentialStore = new NodeCredentialStore();
  private engine: WorkEngine | null = null;
  private primary: WorkConfig | null = null;
  private originalMember: WorkConfig | null = null;
  private approvals: LocalApproval[] = [];
  private pendingApproval: LocalApproval | null = null;
  private executingApproval: LocalApproval | null = null;
  private admissionApproval: LocalApproval | null = null;
  private activeScope: ScreenShareScope | null = null;
  private revokedListeners: (() => void)[] = [];
  private resolveApproval: ((approved: boolean) => void) | null = null;
  private approvalTimer: number = -1;
  private pulseTimer: number = -1;
  private tickTimer: number = -1;
  private enabled: boolean = false;
  private destroyed: boolean = false;
  private connected: boolean = false;
  private presenceAttempted: boolean = false;
  private sourceInstalled: boolean = false;
  private capabilityEpoch: number = 0;
  private presenceExpires: number = 0;
  private credentialExpires: number = 0;
  private pulsing: boolean = false;
  private ticking: boolean = false;
  private reconcilingMetadata: boolean = false;
  private observedUnknown: Set<string> = new Set<string>();
  private deciding: boolean = false;
  private stopping: Promise<void> | null = null;
  private writeTail: Promise<void> = Promise.resolve();
  private transportTail: Promise<void> = Promise.resolve();
  private state: string = 'unconfigured';
  private error: string = '';

  private constructor(context: common.UIAbilityContext, nodeId: string) {
    this.context = context; this.nodeId = nodeId;
    this.store = preferences.getPreferencesSync(context, { name: 'amita_screen_share_runtime_v1' });
    const saved: string = this.store.getSync('identity', '') as string;
    this.identity = saved.length === 0 ? { format: 'amita-screen-share-component/1', node_id: nodeId,
      control_session_id: nodeId + ':screen-share-v1' } : JSON.parse(saved) as RuntimeIdentity;
    requireValue(this.identity.format === 'amita-screen-share-component/1' && this.identity.node_id === nodeId &&
      this.identity.control_session_id === nodeId + ':screen-share-v1', 'screen_share_original_component_required');
    const rawApprovals: string = this.store.getSync('approvals', '[]') as string;
    this.approvals = JSON.parse(rawApprovals) as LocalApproval[];
    requireValue(Array.isArray(this.approvals) && this.approvals.length <= 256, 'screen_share_private_approval_state_invalid');
    for (const approval of this.approvals) {
      requireValue(approval.format === 'amita-local-screen-share-approval/1' &&
        typeof approval.approvalId === 'string' && typeof approval.nonce === 'string' &&
        /^[A-Za-z0-9-]{1,128}$/.test(approval.approvalId) && /^[A-Za-z0-9-]{1,128}$/.test(approval.nonce) &&
        approval.capabilityId === nodeId + ':ui-control-v1' && typeof approval.taskId === 'string' &&
        typeof approval.operationId === 'string' && approval.taskId.length > 0 && approval.taskId.length <= 256 &&
        approval.operationId.length > 0 && approval.operationId.length <= 256 &&
        /^[a-f0-9]{64}$/.test(approval.inputHash) && Number.isSafeInteger(approval.taskEpoch) && approval.taskEpoch > 0 &&
        Number.isSafeInteger(approval.capabilityEpoch) && approval.capabilityEpoch > 0 &&
        Number.isSafeInteger(approval.dispatchEpoch) && approval.dispatchEpoch > 0 &&
        Number.isSafeInteger(approval.expiresAt) && Number.isSafeInteger(approval.allocationDeadline) &&
        Number.isSafeInteger(approval.scopeExpiresAt) && Number.isSafeInteger(approval.approvedAt) &&
        typeof approval.permissionContext === 'string' && approval.permissionContext.length <= 512 &&
        typeof approval.receiptId === 'string' && typeof approval.invocationNonce === 'string' &&
        approval.receiptId.length <= 256 && approval.invocationNonce.length <= 256,
      'screen_share_private_approval_state_invalid');
      if (!['closed', 'denied', 'revoked', 'unknown'].includes(approval.state)) { approval.state = 'unknown'; }
    }
  }

  static current(): ScreenShareRuntime | null { return ScreenShareRuntime.instance; }

  static async create(context: common.UIAbilityContext): Promise<ScreenShareRuntime> {
    if (ScreenShareRuntime.instance !== null) {
      requireValue(ScreenShareRuntime.instance.context === context, 'screen_share_component_context_changed');
      return ScreenShareRuntime.instance;
    }
    if (ScreenShareRuntime.creating !== null) {
      requireValue(ScreenShareRuntime.creatingContext === context, 'screen_share_component_context_changed');
      return await ScreenShareRuntime.creating;
    }
    ScreenShareRuntime.creatingContext = context;
    ScreenShareRuntime.creating = ScreenShareRuntime.createOriginal(context);
    try { return await ScreenShareRuntime.creating; }
    finally { ScreenShareRuntime.creating = null; ScreenShareRuntime.creatingContext = null; }
  }

  private static async createOriginal(context: common.UIAbilityContext): Promise<ScreenShareRuntime> {
    const nodeStore = freshPreferences(context, 'mesh_node_v1');
    const identity = JSON.parse(nodeStore.getSync('snapshot', '') as string) as NodeIdentity;
    requireValue(typeof identity.node_id === 'string' && /^[A-Za-z0-9._:-]{1,160}$/.test(identity.node_id),
      'screen_share_original_member_required');
    const runtime = new ScreenShareRuntime(context, identity.node_id);
    await runtime.persist('identity', JSON.stringify(runtime.identity));
    await runtime.persistApprovals();
    await runtime.configure();
    ScreenShareOwner.attach(context);
    ScreenShareRuntime.instance = runtime;
    return runtime;
  }

  private async persist(key: string, value: string): Promise<void> {
    const previous = this.writeTail;
    let release: () => void = (): void => {};
    this.writeTail = new Promise<void>((resolve): void => { release = resolve; });
    await previous;
    try { this.store.putSync(key, value); await this.store.flush(); }
    finally { release(); }
  }
  private persistApprovals(): Promise<void> { return this.persist('approvals', JSON.stringify(this.approvals)); }
  async save(snapshot: WorkSnapshot): Promise<void> { await this.persist('work_snapshot', JSON.stringify(snapshot)); }
  id(): string { return util.generateRandomUUID(); }
  now(): number { return Math.floor(Date.now() / 1000); }
  private foreground(): boolean { return !this.destroyed && AppStorage.get<boolean>('meshForeground') === true; }

  private memberCurrent(): boolean {
    try {
      if (this.originalMember === null) { return false; }
      const store = freshPreferences(this.context, 'mesh_work_v1');
      const member = JSON.parse(store.getSync('snapshot', '') as string) as WorkSnapshot;
      return member.node_id === this.nodeId && member.config !== null && sameConfig(member.config, this.originalMember);
    } catch (_) { return false; }
  }

  private async configure(): Promise<void> {
    const memberStore = freshPreferences(this.context, 'mesh_work_v1');
    const member = JSON.parse(memberStore.getSync('snapshot', '') as string) as WorkSnapshot;
    requireValue(member.node_id === this.nodeId && member.config !== null, 'screen_share_original_member_configuration_required');
    const original = member.config as WorkConfig;
    validateWorkConfig(original, this.nodeId);
    const trusted = await new TrustedRuntimeConfig(this.context).load(original);
    const primary = trusted === null ? original : trusted.work;
    validateWorkConfig(primary, this.nodeId);
    requireValue(primary.session_id !== this.identity.control_session_id, 'screen_share_distinct_component_required');
    if (this.primary !== null && !sameConfig(this.primary, primary)) {
      requireValue(this.executingApproval === null && this.pendingApproval === null,
        'screen_share_route_changed_during_original_request');
      this.connected = false; this.sourceInstalled = false; this.capabilityEpoch = 0;
    }
    this.originalMember = { format: original.format, resource_url: original.resource_url, node_id: original.node_id,
      session_id: original.session_id, mesh_id: original.mesh_id, authority_id: original.authority_id };
    this.primary = { format: primary.format, resource_url: primary.resource_url, node_id: primary.node_id,
      session_id: primary.session_id, mesh_id: primary.mesh_id, authority_id: primary.authority_id };
    const control: WorkConfig = { format: primary.format, resource_url: primary.resource_url, node_id: primary.node_id,
      session_id: this.identity.control_session_id, mesh_id: primary.mesh_id, authority_id: primary.authority_id };
    if (this.engine === null) {
      const raw = this.store.getSync('work_snapshot', '') as string;
      const snapshot = raw.length === 0 ? freshWorkSnapshot(this.nodeId) : JSON.parse(raw) as WorkSnapshot;
      coldResumeWork(snapshot, this.nodeId);
      requireValue(snapshot.config === null || snapshot.config.node_id === control.node_id &&
        snapshot.config.session_id === control.session_id && snapshot.config.mesh_id === control.mesh_id &&
        snapshot.config.authority_id === control.authority_id, 'screen_share_original_component_changed');
      snapshot.config = control; this.engine = new WorkEngine(snapshot, this); await this.save(snapshot);
    } else {
      this.engine.snapshot.config = control; await this.save(this.engine.snapshot);
    }
  }

  private row(operationId: string, capabilityId: string): WorkRecord | undefined {
    return this.engine === null ? undefined : this.engine.snapshot.records.find((value: WorkRecord): boolean =>
      value.operation_id === operationId && value.capability_id === capabilityId);
  }
  private approvalFor(dispatch: ProviderDispatch): LocalApproval | undefined {
    return this.approvals.find((value: LocalApproval): boolean => value.operationId === dispatch.operation_id &&
      value.capabilityId === dispatch.capability_id && value.taskId === dispatch.task_id && value.taskEpoch === dispatch.task_epoch);
  }
  private originalBound(approval: LocalApproval, dispatch: ProviderDispatch): boolean {
    const row = this.row(approval.operationId, approval.capabilityId);
    return row !== undefined && row.dispatch_json === JSON.stringify(dispatch) &&
      row.task_id === approval.taskId && row.task_epoch === approval.taskEpoch &&
      dispatch.epoch === approval.dispatchEpoch && dispatch.selection?.epoch === approval.capabilityEpoch &&
      dispatch.selection?.workload.input?.sha256 === approval.inputHash &&
      dispatch.selection?.action === 'inspect' && dispatch.provider === 'node:' + this.nodeId;
  }
  private prepared(approval: LocalApproval): boolean {
    const row = this.row(approval.operationId, approval.capabilityId);
    if (row === undefined) { return false; }
    try {
      return this.originalBound(approval, JSON.parse(row.dispatch_json) as ProviderDispatch) && row.state === 'prepared' &&
        row.authority_epoch === approval.dispatchEpoch && row.invocation_nonce === '' && row.result_json === '';
    } catch (_) { return false; }
  }

  private streamEligible(): boolean {
    const status = ScreenShareOwner.status();
    return this.activeScope !== null && this.executingApproval !== null &&
      this.scopeValid(this.activeScope) && status.captureActive && status.backgroundActive && !status.cleanupUnknown;
  }
  private eligible(): boolean {
    const media = ScreenShareOwner.status();
    return !this.destroyed && this.enabled && !media.cleanupUnknown && !ScreenShareOwner.sourceCloseUnconfirmed() &&
      this.memberCurrent() && (this.foreground() || this.streamEligible());
  }
  private sourceLive(): boolean {
    return this.sourceInstalled && this.connected && this.presenceExpires * 1000 > Date.now() &&
      this.credentialExpires * 1000 > Date.now() && this.eligible();
  }

  // An explicit owner action enables request reception only. It is never a
  // stored/global permit for screen capture; every request needs decide().
  async receive(): Promise<void> {
    requireValue(this.foreground() && !this.destroyed && this.pendingApproval === null &&
      this.admissionApproval === null && this.executingApproval === null && this.stopping === null,
    'screen_share_owner_foreground_and_quiescent_runtime_required');
    this.enabled = true;
    this.observedUnknown.clear();
    try {
      await this.configure(); this.state = 'connecting'; this.error = '';
      if (this.pulseTimer === -1) { this.pulseTimer = setInterval((): void => { this.pulse(); }, 10000); }
      if (this.tickTimer === -1) { this.tickTimer = setInterval((): void => { this.tick(); }, 5000); }
      await this.pulse(); this.tick();
    } catch (_) { this.error = 'screen_share_configuration_or_source_unconfirmed'; this.state = 'unconfigured'; }
  }

  private observe(response: PhoneResponse): void {
    requireValue(this.primary !== null && response.format === 'mesh-interactive-node/1' &&
      response.mesh_id === this.primary.mesh_id && response.execution_verified !== true &&
      response.native_tools_restricted !== true && response.session !== undefined,
    'screen_share_source_view_required');
    const view = response.session as SourceView;
    const adapter = view.installed_adapter;
    requireValue(view.node_id === this.nodeId && view.session_id === this.identity.control_session_id &&
      view.lifecycle === 'screen-share' && adapter !== undefined && adapter.enabled === true &&
      adapter.screen_share_enabled === true && adapter.capability_id === this.nodeId + ':ui-control-v1' &&
      adapter.execution_verification === 'declared_only' && Number.isSafeInteger(adapter.epoch) && adapter.epoch > 0 &&
      view.network !== undefined && Number.isFinite(view.network.expires_at), 'screen_share_source_adapter_not_installed');
    if (this.capabilityEpoch > 0 && this.capabilityEpoch !== adapter.epoch) { this.revoke('screen_share_capability_epoch_changed'); }
    this.sourceInstalled = true; this.capabilityEpoch = adapter.epoch;
    this.presenceExpires = view.network.expires_at;
    this.state = view.network.state === 'online' ? 'receiving' : 'offline';
  }

  private async pulse(): Promise<void> {
    if (this.pulsing || this.engine === null || this.primary === null || this.destroyed) { return; }
    this.pulsing = true;
    try {
      // Independent from execute()/tick() busy state. Only actual foreground or
      // live media+scope+OS ticket can renew this component's control presence.
      const online = this.eligible();
      if (!online && (this.pendingApproval !== null || this.admissionApproval !== null || this.executingApproval !== null)) {
        this.revoke(!this.enabled ? 'screen_share_owner_stopped' : 'screen_share_background_ticket_required');
      }
      const capabilities: string[] = online ? [this.nodeId + ':ui-control-v1'] : [];
      if (!online && this.engine.snapshot.pending_control !== null && this.engine.snapshot.pending_control.online) {
        // Control presence is not a business effect. Abandon its stale ONLINE
        // intent instead of replaying it after the OS window has disappeared.
        const oldControl = this.engine.snapshot.pending_control;
        await this.persist('withdrawn_presence:' + oldControl.control_id, JSON.stringify(oldControl));
        this.engine.snapshot.pending_control = null; await this.save(this.engine.snapshot);
      }
      if (!this.connected && !online && !this.presenceAttempted) { return; }
      this.presenceAttempted = true;
      const response = !online ? await this.engine.control(false, []) : this.connected ?
        await this.engine.control(true, capabilities) : await this.engine.connect(capabilities);
      this.observe(response); this.connected = online;
      if (!online) { this.presenceAttempted = false; }
      if (!this.eligible() && online) {
        this.revoke('screen_share_foreground_or_scope_lost');
        this.observe(await this.engine.control(false, [])); this.connected = false;
        this.presenceAttempted = false;
      }
    } catch (_) {
      this.error = 'screen_share_source_presence_unconfirmed'; this.state = 'unconfigured';
      this.sourceInstalled = false; this.connected = false;
      this.revoke('screen_share_source_presence_unconfirmed');
    } finally { this.pulsing = false; }
  }

  private async tick(): Promise<void> {
    if (this.ticking || this.reconcilingMetadata || !this.foreground() || !this.enabled || this.engine === null || !this.sourceLive()) { return; }
    this.ticking = true;
    try {
      // Cold records are observed/reported through the existing engine only;
      // neither reconcile nor a repeated pending dispatch can invoke capture.
      const records = this.engine.snapshot.records.slice().sort((left: WorkRecord, right: WorkRecord): number =>
        (left.result_json.length > 0 ? 0 : 1) - (right.result_json.length > 0 ? 0 : 1));
      for (let index = 0, count = 0; index < records.length && count < 2; index++) {
        const row = records[index]; const identity = row.operation_id + '|' + row.capability_id;
        if (row.state === 'settled' || (row.result_json.length === 0 && this.observedUnknown.has(identity))) { continue; }
        if (row.result_json.length === 0) { this.observedUnknown.add(identity); }
        await this.engine.reconcile(row); count++;
      }
      if (!this.foreground() || !this.sourceLive()) { return; }
      const pending = await this.engine.pull();
      for (const dispatch of pending) {
        if (!this.foreground() || !this.sourceLive()) { break; }
        const existing = this.row(dispatch.operation_id, dispatch.capability_id);
        if (existing !== undefined) { continue; }
        if (dispatch.selection?.action !== 'inspect') { continue; }
        try {
          const result = await this.engine.run(dispatch);
          const approval = this.approvalFor(dispatch);
          if (approval !== undefined && approval.state === 'denied') { this.error = 'screen_share_owner_denied'; }
          else if (result.state === 'unknown') { this.error = 'screen_share_original_stream_outcome_unknown'; }
        }
        finally { this.admissionApproval = null; }
        break;
      }
    } catch (_) { this.error = 'screen_share_original_request_unconfirmed'; }
    finally {
      this.ticking = false;
      if (this.executingApproval === null && this.pendingApproval === null) {
        this.state = !this.enabled ? 'stopped' : this.sourceInstalled ? 'receiving' : 'unconfigured';
      }
    }
  }

  private async decode(dispatch: ProviderDispatch): Promise<StreamInput> {
    validateWorkDispatch(dispatch, this.nodeId);
    requireValue(dispatch.capability_id === this.nodeId + ':ui-control-v1' && dispatch.selection?.action === 'inspect' &&
      dispatch.selection?.epoch === this.capabilityEpoch, 'screen_share_read_only_original_request_required');
    const input = dispatch.selection?.workload.input as Blob; validateWorkBlob(input);
    const codec = new util.Base64Helper(); const bytes = codec.decodeSync(input.base64);
    requireValue(bytes.length > 0 && bytes.length <= 4096 && bytes.length === input.bytes &&
      codec.encodeToStringSync(bytes) === input.base64 && await controlDigest(bytes) === input.sha256,
    'screen_share_original_input_hash_required');
    const body = JSON.parse(new util.TextDecoder('utf-8', { fatal: true }).decodeToString(bytes)) as StreamInput;
    requireValue(body !== null && typeof body === 'object' && !Array.isArray(body) &&
      Object.keys(body).sort().join('|') === 'command|duration_ms|format|max_fps' &&
      body.format === 'amita-phone-screen-share/1' && body.command === 'screen_share' &&
      Number.isSafeInteger(body.duration_ms) && body.duration_ms >= 1000 && body.duration_ms <= 300000 &&
      Number.isSafeInteger(body.max_fps) && body.max_fps >= 1 && body.max_fps <= 2,
    'screen_share_dedicated_read_only_stream_required');
    return body;
  }

  async preflight(dispatch: ProviderDispatch): Promise<void> {
    requireValue(this.foreground() && this.sourceLive() && this.pendingApproval === null && this.executingApproval === null,
      'screen_share_owner_and_live_source_required');
    const body = await this.decode(dispatch);
    requireValue(this.foreground() && this.sourceLive() && this.approvals.length < 256 &&
      this.approvalFor(dispatch) === undefined, 'screen_share_original_approval_required');
    const purpose = dispatch.selection?.scope as PurposeScope;
    const approval: LocalApproval = { format: 'amita-local-screen-share-approval/1',
      approvalId: 'share-approval-' + this.id(), nonce: this.id(), revision: 0, state: 'pending',
      operationId: dispatch.operation_id, capabilityId: dispatch.capability_id, taskId: dispatch.task_id as string,
      taskEpoch: dispatch.task_epoch as number, inputHash: (dispatch.selection?.workload.input as Blob).sha256,
      capabilityEpoch: this.capabilityEpoch, dispatchEpoch: dispatch.epoch,
      requestedDurationMs: body.duration_ms, maxFps: body.max_fps,
      purpose: typeof purpose.purpose === 'string' ? purpose.purpose.replace(/[\x00-\x1f\x7f]/g, ' ').slice(0, 240) :
        '供原任务只读查看当前屏幕；不会代为点击或输入',
      expiresAt: Math.floor(Math.min(Date.now() + 30000, (dispatch.deadline as number) * 1000 - 10000)),
      allocationDeadline: Math.floor((dispatch.deadline as number) * 1000), scopeExpiresAt: 0,
      approvedAt: 0, permissionContext: '', streamSessionId: '', receiptId: '', invocationNonce: '', authorityEpoch: 0 };
    requireValue(approval.expiresAt > Date.now() && this.prepared(approval), 'screen_share_prepared_original_deadline_required');
    this.approvals.push(approval); this.pendingApproval = approval; this.admissionApproval = approval;
    await this.persistApprovals();
    requireValue(approval.state === 'pending' && this.foreground() && this.sourceLive() && this.prepared(approval),
      'screen_share_pending_scope_lost');
    this.state = 'awaiting_owner';
    const approved = await new Promise<boolean>((resolve): void => {
      this.resolveApproval = resolve;
      this.approvalTimer = setTimeout((): void => { this.revoke('screen_share_local_approval_expired'); },
        Math.max(1, approval.expiresAt - Date.now()));
    });
    requireValue(approved && this.allowed(dispatch), 'screen_share_owner_rejected_expired_or_stale');
  }

  permission(dispatch: ProviderDispatch): string {
    const approval = this.approvalFor(dispatch);
    return approval !== undefined && this.originalBound(approval, dispatch) ? approval.permissionContext : '';
  }
  allowed(dispatch: ProviderDispatch, permissionContext?: string): boolean {
    const approval = this.approvalFor(dispatch);
    return approval !== undefined && ['approved', 'consumed'].includes(approval.state) && this.originalBound(approval, dispatch) &&
      approval.permissionContext.length > 0 && (permissionContext === undefined || permissionContext === approval.permissionContext) &&
      approval.scopeExpiresAt > Date.now() && approval.capabilityEpoch === this.capabilityEpoch &&
      approval.allocationDeadline > Date.now() && this.sourceLive() && this.foreground();
  }

  async decide(value: ScreenSharePending, approve: boolean): Promise<void> {
    const approval = this.pendingApproval;
    if (approval === null) { throw new Error('screen_share_pending_local_decision_mismatch'); }
    requireValue(!this.deciding && this.resolveApproval !== null && approval !== null && approval.state === 'pending' && this.foreground() &&
      this.sourceLive() && this.prepared(approval) && approval.expiresAt > Date.now() &&
      value.approvalId === approval.approvalId && value.nonce === approval.nonce && value.revision === approval.revision &&
      value.operationId === approval.operationId && value.taskId === approval.taskId && value.taskEpoch === approval.taskEpoch &&
      value.inputHash === approval.inputHash && value.expiresAt === approval.expiresAt,
    'screen_share_pending_local_decision_mismatch');
    this.deciding = true;
    try {
      approval.state = approve ? 'approving' : 'denied';
      await this.persistApprovals();
      requireValue(approval.state === (approve ? 'approving' : 'denied') && this.foreground() &&
        this.sourceLive() && this.prepared(approval) && approval.expiresAt > Date.now(), 'screen_share_decision_interrupted');
      if (approve) {
        approval.approvedAt = Date.now();
        approval.scopeExpiresAt = Math.floor(Math.min(Date.now() + approval.requestedDurationMs,
          approval.allocationDeadline - 5000));
        requireValue(approval.scopeExpiresAt > Date.now(), 'screen_share_original_allocation_expired');
        approval.streamSessionId = 'screen-share-' + this.id();
        approval.permissionContext = ['screen-share', approval.approvalId, approval.nonce, approval.inputHash,
          String(approval.taskEpoch), String(approval.capabilityEpoch)].join('|');
        approval.state = 'approved';
        await this.persistApprovals();
        requireValue(approval.state === 'approved' && this.foreground() && this.sourceLive() && this.prepared(approval),
          'screen_share_decision_interrupted');
      }
      clearTimeout(this.approvalTimer); this.approvalTimer = -1;
      const resolve = this.resolveApproval; this.resolveApproval = null; this.pendingApproval = null;
      if (resolve !== null) { resolve(approve); }
    } catch (_) {
      this.revoke('screen_share_local_decision_unconfirmed');
      throw new Error('screen_share_local_decision_unconfirmed');
    } finally { this.deciding = false; }
  }

  private scopeValid(scope: ScreenShareScope): boolean {
    const approval = this.executingApproval;
    const row = approval === null ? undefined : this.row(approval.operationId, approval.capabilityId);
    return this.activeScope !== null && sameScreenShareScope(scope, this.activeScope) && approval !== null &&
      approval.state === 'consumed' && row !== undefined && row.state === 'attempted' && row.result_json === '' &&
      row.task_id === approval.taskId && row.task_epoch === approval.taskEpoch &&
      row.receipt_id === approval.receiptId && row.invocation_nonce === approval.invocationNonce &&
      row.authority_epoch === approval.authorityEpoch && row.permission_context === approval.permissionContext &&
      approval.capabilityEpoch === this.capabilityEpoch && approval.scopeExpiresAt > Date.now() &&
      approval.allocationDeadline > Date.now() && this.sourceInstalled && this.connected &&
      this.presenceExpires * 1000 > Date.now() && this.credentialExpires * 1000 > Date.now() &&
      this.enabled && !this.destroyed && this.memberCurrent();
  }

  async execute(dispatch: ProviderDispatch): Promise<Blob> {
    requireValue(this.allowed(dispatch) && this.primary !== null, 'screen_share_original_local_approval_required');
    const approval = this.approvalFor(dispatch) as LocalApproval;
    const row = this.row(dispatch.operation_id, dispatch.capability_id) as WorkRecord;
    requireValue(row !== undefined && row.state === 'attempted' && row.invocation_nonce.length > 0 &&
      row.authority_epoch === dispatch.epoch + 2 && row.result_json === '', 'screen_share_actual_attempted_record_required');
    approval.state = 'consumed'; approval.receiptId = row.receipt_id; approval.invocationNonce = row.invocation_nonce;
    approval.authorityEpoch = row.authority_epoch; this.executingApproval = approval;
    this.activeScope = { sessionId: approval.streamSessionId, taskId: row.task_id, taskEpoch: row.task_epoch,
      operationId: row.operation_id, approvalId: approval.approvalId, nonce: approval.nonce,
      revision: approval.revision, expiresAt: approval.scopeExpiresAt };
    await this.persistApprovals();
    const scope = this.activeScope as ScreenShareScope;
    const authorization: ScreenShareAuthorization = {
      isForeground: (): boolean => this.foreground(),
      valid: (value: ScreenShareScope): boolean => this.scopeValid(value),
      onRevoked: (listener: () => void): (() => void) => {
        requireValue(this.revokedListeners.length < 4, 'screen_share_revocation_listener_limit');
        this.revokedListeners.push(listener);
        return (): void => { this.revokedListeners = this.revokedListeners.filter((entry: () => void): boolean => entry !== listener); };
      },
      // Read-only AVScreenCapture never owns or opens the independent selfHDC session.
      closeHdc: (_value: ScreenShareScope): void => {}
    };
    const gate: PhoneScreenShareGate = {
      valid: (value: ScreenShareScope): boolean => this.scopeValid(value),
      serviceLive: (): boolean => this.eligible() && this.scopeValid(scope)
    };
    this.state = 'starting_capture';
    try {
      requireValue(this.foreground() && this.scopeValid(scope), 'screen_share_original_scope_lost');
      await ScreenShareOwner.startManaged(this.primary as WorkConfig, this.identity.control_session_id,
        row, scope, authorization, gate);
      while (true) {
        const media = ScreenShareOwner.status();
        if (media.state === 'stopped' || media.state === 'stopped_cleanup_unknown') { break; }
        if (!this.scopeValid(scope) || (!this.foreground() && !(media.captureActive && media.backgroundActive))) {
          this.revoke('screen_share_original_scope_or_background_lost');
          await ScreenShareOwner.stop(); break;
        }
        this.state = media.backgroundActive ? 'sharing' : 'awaiting_capture_or_frame_ack';
        await new Promise<void>((resolve): void => { setTimeout(resolve, 250); });
      }
      // This waits for the OS stop AND the original Source close ACK. The first
      // frame is never a WorkEngine completion, result or settlement.
      await ScreenShareOwner.stop();
      const final = ScreenShareOwner.status() as AckStatus;
      requireValue(!final.cleanupUnknown && !ScreenShareOwner.sourceCloseUnconfirmed() &&
        !final.captureActive && !final.backgroundActive && final.acknowledgedFrames > 0 &&
        /^[a-f0-9]{64}$/.test(final.lastAcknowledgedSha256), 'screen_share_final_close_or_ack_unconfirmed');
      const summary: StreamSummary = { format: 'amita-phone-screen-share-result/1', command: 'screen_share', readonly: true,
        operation_id: row.operation_id, task_id: row.task_id, task_epoch: row.task_epoch,
        stop_reason: safeCode(final.error || 'screen_share_owner_stopped'),
        acknowledged_frames: final.acknowledgedFrames, last_frame_revision: final.lastFrameRevision,
        last_ack_sha256: final.lastAcknowledgedSha256, source_closed: true, capture_quiescent: true };
      approval.state = 'closed'; await this.persistApprovals();
      return await controlBlob(summary);
    } catch (_) {
      approval.state = 'unknown'; await this.persistApprovals();
      try { await ScreenShareOwner.stop(); } catch (_) {}
      throw new Error('screen_share_original_stream_outcome_unknown');
    } finally {
      this.executingApproval = null; this.activeScope = null; this.revokedListeners = [];
    }
  }

  reportAllowed(dispatch: ProviderDispatch, permissionContext: string): boolean {
    const approval = this.approvalFor(dispatch);
    const row = this.row(dispatch.operation_id, dispatch.capability_id);
    // A finished, quiescent metadata result may be reported on foreground
    // reunion without restarting capture or restoring a spent owner approval.
    return this.foreground() && this.memberCurrent() && approval !== undefined && approval.state === 'closed' &&
      row !== undefined && row.result_json.length > 0 && row.dispatch_json === JSON.stringify(dispatch) &&
      row.permission_context === permissionContext && approval.permissionContext === permissionContext &&
      approval.receiptId === row.receipt_id && approval.invocationNonce === row.invocation_nonce && this.metadataOnly(row);
  }

  private metadataOnly(row: WorkRecord): boolean {
    try {
      const blob = JSON.parse(row.result_json) as Blob; validateWorkBlob(blob);
      const codec = new util.Base64Helper(); const bytes = codec.decodeSync(blob.base64);
      if (bytes.length !== blob.bytes || bytes.length <= 0 || bytes.length > 4096 ||
        codec.encodeToStringSync(bytes) !== blob.base64) { return false; }
      const value = JSON.parse(new util.TextDecoder('utf-8', { fatal: true }).decodeToString(bytes)) as StreamSummary;
      return Object.keys(value).sort().join('|') ===
        'acknowledged_frames|capture_quiescent|command|format|last_ack_sha256|last_frame_revision|operation_id|readonly|source_closed|stop_reason|task_epoch|task_id' &&
        value.format === 'amita-phone-screen-share-result/1' && value.command === 'screen_share' && value.readonly === true &&
        value.operation_id === row.operation_id && value.task_id === row.task_id && value.task_epoch === row.task_epoch &&
        value.source_closed === true && value.capture_quiescent === true && /^[a-f0-9]{64}$/.test(value.last_ack_sha256) &&
        Number.isSafeInteger(value.acknowledged_frames) && value.acknowledged_frames > 0 &&
        Number.isSafeInteger(value.last_frame_revision) && value.last_frame_revision >= value.acknowledged_frames &&
        /^screen_share_[a-z_]{1,72}$/.test(value.stop_reason);
    } catch (_) { return false; }
  }

  private async reconcileMetadata(): Promise<void> {
    if (this.reconcilingMetadata || this.ticking || !this.foreground() || this.engine === null) { return; }
    this.reconcilingMetadata = true;
    try {
      for (let index = 0, count = 0; index < this.engine.snapshot.records.length && count < 2 && this.foreground(); index++) {
        const row = this.engine.snapshot.records[index];
        if (row.result_json.length > 0 && row.state !== 'settled') {
          await this.engine.reconcile(row); count++;
        }
      }
    } finally { this.reconcilingMetadata = false; }
  }

  async exchange(action: string, args: Object): Promise<PhoneResponse> {
    const previous = this.transportTail;
    let release: () => void = (): void => {};
    this.transportTail = new Promise<void>((resolve): void => { release = resolve; });
    await previous;
    try {
      requireValue(!this.destroyed && this.engine !== null && this.engine.snapshot.config !== null &&
        deviceInfo.sdkApiVersion >= 23 && (this.foreground() || this.streamEligible() || action === 'mesh_node_update'),
      'screen_share_actual_execution_window_required');
      const controlAction = action === 'mesh_connect' || action === 'mesh_node_update';
      const presence = args as ControlPresenceArguments;
      requireValue(!controlAction || presence.online !== true || this.eligible(), 'screen_share_ineligible_presence_not_renewed');
      const config = (this.engine as WorkEngine).snapshot.config as WorkConfig; validateWorkConfig(config, this.nodeId);
      const credential: NodeCredential | null = await this.credentials.load();
      requireValue(credential !== null && credential.nodeId === this.nodeId && credential.scope !== undefined &&
        credential.scope.includes('phone:node') && (credential.expiresAt === undefined || credential.expiresAt > this.now()),
      'screen_share_original_asset_credential_required');
      const principal = credential as NodeCredential;
      this.credentialExpires = principal.expiresAt === undefined ? this.now() + 30 : principal.expiresAt;
      requireValue(!controlAction || presence.online !== true || this.eligible(), 'screen_share_ineligible_presence_not_renewed');
      const body = JSON.stringify({ action: action, arguments: args });
      requireValue(new util.TextEncoder().encodeInto(body).length <= 100000, 'screen_share_transport_budget');
      const request = http.createHttp();
      try {
        const response = await request.request(config.resource_url, {
          method: http.RequestMethod.POST, usingProtocol: http.HttpProtocol.HTTP1_1,
          header: { 'Authorization': 'Bearer ' + principal.token, 'Content-Type': 'application/json', 'Accept': 'application/json' },
          extraData: body, expectDataType: http.HttpDataType.STRING, usingCache: false, maxRedirects: 0,
          connectTimeout: 5000, readTimeout: 5000, maxLimit: 1048576 });
        requireValue(response.responseCode === 200 && typeof response.result === 'string' &&
          new util.TextEncoder().encodeInto(response.result as string).length <= 1048576,
        'screen_share_source_transport_unconfirmed');
        return JSON.parse(response.result as string) as PhoneResponse;
      } finally { request.destroy(); }
    } finally { release(); }
  }

  private revoke(reason: string): void {
    this.error = safeCode(reason);
    const approval = this.pendingApproval !== null ? this.pendingApproval :
      this.executingApproval !== null ? this.executingApproval : this.admissionApproval;
    if (approval !== null && !['closed', 'denied', 'unknown', 'revoked'].includes(approval.state)) {
      approval.state = 'revoked'; this.persistApprovals().catch((): void => {});
    }
    clearTimeout(this.approvalTimer); this.approvalTimer = -1;
    const resolve = this.resolveApproval; this.resolveApproval = null; this.pendingApproval = null;
    if (resolve !== null) { resolve(false); }
    for (const listener of this.revokedListeners.slice()) { try { listener(); } catch (_) {} }
    if (this.executingApproval !== null) { ScreenShareOwner.stop().catch((): void => {}); }
  }

  foregroundChanged(): void {
    ScreenShareOwner.foregroundChanged();
    if (!this.foreground() && !this.streamEligible()) { this.revoke('screen_share_background_ticket_required'); }
    this.pulse();
    if (this.foreground()) {
      this.observedUnknown.clear();
      this.reconcileMetadata().catch((): void => { this.error = 'screen_share_original_metadata_receipt_unconfirmed'; });
      this.tick();
    }
  }

  stop(): Promise<void> {
    if (this.stopping !== null) { return this.stopping; }
    this.enabled = false; this.revoke('screen_share_owner_stopped');
    clearInterval(this.pulseTimer); clearInterval(this.tickTimer);
    this.pulseTimer = -1; this.tickTimer = -1;
    this.stopping = (async (): Promise<void> => {
      try { await ScreenShareOwner.stop(); }
      finally { await this.pulse(); this.stopping = null; }
    })();
    return this.stopping;
  }

  async destroy(): Promise<void> {
    try { await this.stop(); }
    finally {
      this.destroyed = true; clearInterval(this.pulseTimer); clearInterval(this.tickTimer);
      this.pulseTimer = -1; this.tickTimer = -1;
      if (ScreenShareRuntime.instance === this) { ScreenShareRuntime.instance = null; }
    }
  }

  pending(): ScreenSharePending | null {
    const value = this.pendingApproval;
    if (value === null || value.state !== 'pending' || this.resolveApproval === null) { return null; }
    return { approvalId: value.approvalId, nonce: value.nonce, revision: value.revision,
      operationId: value.operationId, taskId: value.taskId, taskEpoch: value.taskEpoch,
      inputHash: value.inputHash, expiresAt: value.expiresAt, allocationDeadline: value.allocationDeadline,
      durationMs: value.requestedDurationMs, maxFps: value.maxFps, purpose: value.purpose };
  }

  status(): ScreenShareRuntimeStatus {
    const value = new ScreenShareRuntimeStatus();
    const media = ScreenShareOwner.status();
    value.state = this.state; value.error = this.error; value.nodeId = this.nodeId;
    value.controlSessionId = this.identity.control_session_id; value.receiving = this.enabled;
    value.sourceInstalled = this.sourceInstalled; value.capabilityEpoch = this.capabilityEpoch;
    value.presenceExpiresAt = this.presenceExpires * 1000; value.foreground = this.foreground();
    value.captureActive = media.captureActive; value.backgroundActive = media.backgroundActive;
    value.cleanupUnknown = media.cleanupUnknown; value.sourceCloseUnknown = ScreenShareOwner.sourceCloseUnconfirmed();
    value.acknowledgedFrames = media.acknowledgedFrames; value.pending = this.pending() !== null;
    value.executing = this.executingApproval !== null;
    const original = this.pendingApproval === null ? this.executingApproval : this.pendingApproval;
    if (original !== null) {
      value.taskId = original.taskId; value.taskEpoch = original.taskEpoch; value.operationId = original.operationId;
    }
    const records = this.engine === null ? [] : this.engine.snapshot.records;
    value.unknownRecords = records.filter((row: WorkRecord): boolean => row.state === 'unknown').length;
    value.pendingResults = records.filter((row: WorkRecord): boolean => row.result_json.length > 0 && row.state !== 'settled').length;
    value.settledRecords = records.filter((row: WorkRecord): boolean => row.state === 'settled').length;
    return value;
  }
}
