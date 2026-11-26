import { common, Want } from '@kit.AbilityKit';
import { preferences } from '@kit.ArkData';
import { deviceInfo, screenLock } from '@kit.BasicServicesKit';
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
import { ScreenShareInput, ScreenShareScope, ScreenShareTarget, decodeScreenShareInput, sameScreenShareScope,
  sameScreenShareTarget, validScreenShareLaunchScope, validScreenShareTarget } from './ScreenSharePolicy';
import { PhoneScreenShareGate } from './PhoneScreenShareSink';

interface NodeIdentity { node_id: string; }
interface RuntimeIdentity { format: string; node_id: string; control_session_id: string; }
interface SourceAdapter {
  enabled: boolean; screen_share_enabled: boolean; capability_id: string;
  screen_share_launch_enabled?: boolean;
  screen_share_standing_allowed?: boolean;
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
  target?: ScreenShareTarget; launchStatus?: string; launchAckBaseline?: number;
  standing?: boolean;
}
export interface ScreenSharePending {
  approvalId: string; nonce: string; revision: number; operationId: string;
  taskId: string; taskEpoch: number; inputHash: string; expiresAt: number;
  allocationDeadline: number; durationMs: number; maxFps: number; purpose: string;
  target?: ScreenShareTarget;
}
export class ScreenShareRuntimeStatus {
  state: string = 'unconfigured';
  error: string = '';
  nodeId: string = '';
  controlSessionId: string = '';
  identityObservation: string = 'declared_app_component';
  receiving: boolean = false;
  sourceInstalled: boolean = false;
  standingAllowed: boolean = false;
  receptionPaused: boolean = false;
  screenState: string = 'unchecked';
  captureConsentPending: boolean = false;
  capabilityEpoch: number = 0;
  presenceExpiresAt: number = 0;
  foreground: boolean = false;
  captureActive: boolean = false;
  backgroundActive: boolean = false;
  acknowledgedFrames: number = 0;
  pending: boolean = false;
  executing: boolean = false;
  launchStatus: string = '';
  laterFrameAcknowledged: boolean = false;
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
  target?: ScreenShareTarget; launch_system_acknowledged?: boolean;
  launch_baseline_frame_revision?: number; later_frame_revision?: number;
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

// Retained by EntryAbility, never by a page. Capture and the owner's optional
// app launch use this component's separate declared presence.
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
  private manualReceiving: boolean = false;
  private receptionPaused: boolean = false;
  private destroyed: boolean = false;
  private connected: boolean = false;
  private presenceAttempted: boolean = false;
  private sourceInstalled: boolean = false;
  private sourceLaunchEnabled: boolean = false;
  private sourceStandingAllowed: boolean = false;
  private screenState: string = 'unchecked';
  private screenCheck: Promise<void> | null = null;
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
    this.receptionPaused = this.store.getSync('reception_paused', 'false') === 'true';
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
        approval.receiptId.length <= 256 && approval.invocationNonce.length <= 256 &&
        (approval.target === undefined || validScreenShareTarget(approval.target)) &&
        (approval.standing === undefined || typeof approval.standing === 'boolean') &&
        (approval.standing !== true || approval.target === undefined),
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

  private observeScreenState(): Promise<void> {
    if (this.screenCheck === null) {
      this.screenCheck = this.readScreenState().finally((): void => { this.screenCheck = null; });
    }
    return this.screenCheck;
  }

  private async readScreenState(): Promise<void> {
    try {
      const locked = await screenLock.isScreenLocked();
      this.screenState = locked === false ? 'unlocked' : locked === true ? 'locked' : 'unavailable';
    } catch (_) { this.screenState = 'unavailable'; }
  }

  private standingReady(): boolean {
    return this.sourceStandingAllowed && !this.receptionPaused && this.screenState === 'unlocked';
  }

  private receptionRequested(): boolean {
    return !this.receptionPaused && (this.manualReceiving || this.sourceStandingAllowed);
  }

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
      this.connected = false; this.sourceInstalled = false; this.sourceLaunchEnabled = false;
      this.sourceStandingAllowed = false; this.capabilityEpoch = 0;
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
      dispatch.selection?.workload.input?.sha256 === approval.inputHash && dispatch.provider === 'node:' + this.nodeId &&
      (approval.target === undefined ? dispatch.selection?.action === 'inspect' : dispatch.selection?.action === 'action' &&
        validScreenShareLaunchScope(dispatch.selection.scope, approval.target, approval.taskId, approval.taskEpoch));
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
    return !this.destroyed && this.stopping === null && this.receptionRequested() && !media.cleanupUnknown &&
      !ScreenShareOwner.sourceCloseUnconfirmed() && this.memberCurrent() &&
      ((this.foreground() && (this.sourceStandingAllowed ? this.standingReady() : this.manualReceiving)) || this.streamEligible());
  }
  private sourceLive(): boolean {
    return this.sourceInstalled && this.connected && this.presenceExpires * 1000 > Date.now() &&
      this.credentialExpires * 1000 > Date.now() && this.eligible();
  }

  private startPolling(): void {
    if (this.pulseTimer === -1) { this.pulseTimer = setInterval((): void => { this.refreshReceiver(); }, 10000); }
    if (this.tickTimer === -1) { this.tickTimer = setInterval((): void => { this.tick(); }, 5000); }
  }

  private async refreshReceiver(): Promise<void> { await this.pulse(); this.tick(); }

  // An explicit gesture resumes a locally paused receiver or enables legacy
  // manual reception. Standing consent is supplied solely by the original Source.
  async receive(): Promise<void> {
    requireValue(this.foreground() && !this.destroyed && this.pendingApproval === null &&
      this.admissionApproval === null && this.executingApproval === null && this.stopping === null,
    'screen_share_owner_foreground_and_quiescent_runtime_required');
    this.observedUnknown.clear();
    try {
      await this.configure();
      if (this.receptionPaused) { await this.persist('reception_paused', 'false'); this.receptionPaused = false; }
      this.manualReceiving = !this.sourceStandingAllowed;
      this.state = 'connecting'; this.error = '';
      this.startPolling(); await this.refreshReceiver();
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
    this.sourceLaunchEnabled = adapter.screen_share_launch_enabled === true;
    const newStandingAllowed = adapter.screen_share_standing_allowed === true;
    if (this.sourceStandingAllowed !== newStandingAllowed &&
      (this.pendingApproval !== null || this.admissionApproval !== null || this.executingApproval !== null)) {
      this.revoke('screen_share_standing_permission_changed');
    }
    this.sourceStandingAllowed = newStandingAllowed;
    if (newStandingAllowed) { this.manualReceiving = false; }
    const approval = this.executingApproval ?? this.admissionApproval;
    if (approval?.target !== undefined && !this.sourceLaunchEnabled) { this.revoke('screen_share_launch_adapter_disabled'); }
    this.sourceInstalled = true; this.capabilityEpoch = adapter.epoch;
    this.presenceExpires = view.network.expires_at;
    this.state = view.network.state === 'online' && this.receptionRequested() ? 'receiving' : 'offline';
  }

  private async pulse(): Promise<void> {
    if (this.pulsing || this.engine === null || this.primary === null || this.destroyed) { return; }
    this.pulsing = true;
    try {
      if (!this.receptionPaused && (this.foreground() || this.executingApproval?.standing === true)) {
        await this.observeScreenState();
      }
      if (!this.manualReceiving && !this.receptionPaused && this.foreground() && this.screenState === 'unlocked' &&
        (!this.sourceInstalled || !this.sourceStandingAllowed)) {
        // Discover the owner-installed adapter without advertising an executable capability.
        this.observe(await this.engine.discover());
      }
      // Independent from execute()/tick() busy state. Only actual foreground or
      // live media+scope+OS ticket can renew this component's control presence.
      const online = this.eligible();
      if (!online && (this.pendingApproval !== null || this.admissionApproval !== null || this.executingApproval !== null)) {
        this.revoke(!this.receptionRequested() ? 'screen_share_owner_stopped' :
          this.sourceStandingAllowed && this.screenState !== 'unlocked' ? 'screen_share_unlocked_window_required' :
          'screen_share_background_ticket_required');
      }
      const capabilities: string[] = online ? [this.nodeId + ':ui-control-v1'] : [];
      if ((!online || !this.connected) && this.engine.snapshot.pending_control !== null && this.engine.snapshot.pending_control.online) {
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
      if (this.error === 'screen_share_source_presence_unconfirmed') { this.error = ''; }
      if (!online) { this.presenceAttempted = false; }
      if (!this.eligible() && online) {
        this.revoke('screen_share_foreground_or_scope_lost');
        this.observe(await this.engine.control(false, [])); this.connected = false;
        this.presenceAttempted = false;
      }
    } catch (_) {
      this.error = 'screen_share_source_presence_unconfirmed'; this.state = 'unconfigured';
      this.sourceInstalled = false; this.sourceLaunchEnabled = false;
      this.sourceStandingAllowed = false; this.connected = false;
      this.revoke('screen_share_source_presence_unconfirmed');
    } finally { this.pulsing = false; }
  }

  private async tick(): Promise<void> {
    if (this.ticking || this.reconcilingMetadata || !this.foreground() || this.engine === null || !this.sourceLive()) { return; }
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
        if (dispatch.selection?.action !== 'inspect') {
          if (dispatch.selection?.action !== 'action' || !this.sourceLaunchEnabled) { continue; }
          // Other UI action recipes belong to a different runtime.
          try { await this.decode(dispatch); } catch (_) { continue; }
          if (this.sourceStandingAllowed) {
            this.error = 'screen_share_launch_input_monitor_unavailable'; continue;
          }
        }
        this.error = '';
        try {
          const result = await this.engine.run(dispatch);
          const approval = this.approvalFor(dispatch);
          if (approval !== undefined && approval.state === 'denied') { this.error = 'screen_share_owner_denied'; }
          else if (result.state === 'unknown' && this.error === '' && approval?.launchStatus !== 'system_rejected') {
            this.error = 'screen_share_original_stream_outcome_unknown';
          }
        }
        finally { this.admissionApproval = null; }
        break;
      }
    } catch (_) { this.error = 'screen_share_original_request_unconfirmed'; }
    finally {
      this.ticking = false;
      if (this.executingApproval === null && this.pendingApproval === null) {
        this.state = this.receptionPaused ? 'stopped' : this.sourceLive() ? 'receiving' : 'offline';
      }
    }
  }

  private async decode(dispatch: ProviderDispatch): Promise<ScreenShareInput> {
    validateWorkDispatch(dispatch, this.nodeId);
    const selection = dispatch.selection;
    if (selection === undefined) { throw new Error('screen_share_original_request_required'); }
    requireValue(dispatch.capability_id === this.nodeId + ':ui-control-v1' && selection.epoch === this.capabilityEpoch &&
      (selection.action === 'inspect' || (selection.action === 'action' && this.sourceLaunchEnabled)),
    'screen_share_original_request_required');
    const input = selection.workload.input as Blob; validateWorkBlob(input);
    const codec = new util.Base64Helper(); const bytes = codec.decodeSync(input.base64);
    requireValue(bytes.length > 0 && bytes.length <= 4096 && bytes.length === input.bytes &&
      codec.encodeToStringSync(bytes) === input.base64 && await controlDigest(bytes) === input.sha256,
    'screen_share_original_input_hash_required');
    const body = decodeScreenShareInput(new util.TextDecoder('utf-8', { fatal: true }).decodeToString(bytes), selection.action);
    requireValue(body.target === undefined || (this.sourceLaunchEnabled && validScreenShareLaunchScope(selection.scope,
      body.target, dispatch.task_id as string, dispatch.task_epoch as number)), 'screen_share_original_launch_scope_required');
    return body;
  }

  async preflight(dispatch: ProviderDispatch): Promise<void> {
    requireValue(this.foreground() && this.sourceLive() && this.pendingApproval === null && this.executingApproval === null,
      'screen_share_owner_and_live_source_required');
    const body = await this.decode(dispatch);
    const standing = this.sourceStandingAllowed;
    if (standing) { await this.observeScreenState(); }
    requireValue(this.foreground() && this.sourceLive() && this.approvals.length < 256 &&
      (standing ? this.standingReady() && body.target === undefined : !this.sourceStandingAllowed &&
        (body.target === undefined || this.sourceLaunchEnabled)) &&
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
      approvedAt: 0, permissionContext: '', streamSessionId: '', receiptId: '', invocationNonce: '', authorityEpoch: 0,
      standing: standing };
    if (body.target !== undefined) {
      approval.target = body.target;
      approval.purpose = '共享屏幕，待画面及后台共享就绪后尝试打开以下应用';
    }
    requireValue(approval.expiresAt > Date.now() && this.prepared(approval), 'screen_share_prepared_original_deadline_required');
    this.approvals.push(approval); this.pendingApproval = approval; this.admissionApproval = approval;
    await this.persistApprovals();
    requireValue(approval.state === 'pending' && this.foreground() && this.sourceLive() && this.prepared(approval),
      'screen_share_pending_scope_lost');
    this.state = standing ? 'authorizing_original_capture' : 'awaiting_owner';
    const decision = new Promise<boolean>((resolve): void => {
      this.resolveApproval = resolve;
      this.approvalTimer = setTimeout((): void => { this.revoke('screen_share_local_approval_expired'); },
        Math.max(1, approval.expiresAt - Date.now()));
    });
    if (standing) {
      const exact = this.pendingBinding();
      try {
        requireValue(exact !== null, 'screen_share_pending_scope_lost');
        await this.decideBound(exact as ScreenSharePending, true, true);
      } catch (_) {
        this.revoke(this.screenState === 'unlocked' ? 'screen_share_pending_scope_lost' :
          'screen_share_unlocked_window_required');
      }
    }
    const approved = await decision;
    requireValue(approved && this.allowed(dispatch), 'screen_share_owner_rejected_expired_or_stale');
  }

  permission(dispatch: ProviderDispatch): string {
    const approval = this.approvalFor(dispatch);
    return approval !== undefined && this.originalBound(approval, dispatch) ? approval.permissionContext : '';
  }
  allowed(dispatch: ProviderDispatch, permissionContext?: string): boolean {
    const approval = this.approvalFor(dispatch);
    return approval !== undefined && ['approved', 'consumed'].includes(approval.state) && this.originalBound(approval, dispatch) &&
      (approval.standing === true ? this.standingReady() : !this.sourceStandingAllowed) &&
      (approval.target === undefined || this.sourceLaunchEnabled) &&
      approval.permissionContext.length > 0 && (permissionContext === undefined || permissionContext === approval.permissionContext) &&
      approval.scopeExpiresAt > Date.now() && approval.capabilityEpoch === this.capabilityEpoch &&
      approval.allocationDeadline > Date.now() && this.sourceLive() && this.foreground();
  }

  async decide(value: ScreenSharePending, approve: boolean): Promise<void> {
    await this.decideBound(value, approve, false);
  }

  private async decideBound(value: ScreenSharePending, approve: boolean, standing: boolean): Promise<void> {
    const approval = this.pendingApproval;
    if (approval === null) { throw new Error('screen_share_pending_local_decision_mismatch'); }
    requireValue(!this.deciding && this.resolveApproval !== null && approval !== null && approval.state === 'pending' && this.foreground() &&
      this.sourceLive() && this.prepared(approval) && approval.expiresAt > Date.now() &&
      (standing ? approval.standing === true && this.standingReady() && approval.target === undefined :
        approval.standing !== true && !this.sourceStandingAllowed) &&
      value.approvalId === approval.approvalId && value.nonce === approval.nonce && value.revision === approval.revision &&
      value.operationId === approval.operationId && value.taskId === approval.taskId && value.taskEpoch === approval.taskEpoch &&
      value.inputHash === approval.inputHash && value.expiresAt === approval.expiresAt &&
      sameScreenShareTarget(value.target, approval.target),
    'screen_share_pending_local_decision_mismatch');
    this.deciding = true;
    try {
      approval.state = approve ? 'approving' : 'denied';
      await this.persistApprovals();
      if (standing) { await this.observeScreenState(); }
      requireValue(approval.state === (approve ? 'approving' : 'denied') && this.foreground() &&
        this.sourceLive() && this.prepared(approval) && approval.expiresAt > Date.now() &&
        (!standing || this.standingReady()), 'screen_share_decision_interrupted');
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
      (approval.target === undefined || this.sourceLaunchEnabled) &&
      this.presenceExpires * 1000 > Date.now() && this.credentialExpires * 1000 > Date.now() &&
      this.receptionRequested() && !this.destroyed && this.memberCurrent() &&
      (approval.standing === true ? this.standingReady() : !this.sourceStandingAllowed);
  }

  private async launchApp(approval: LocalApproval, dispatch: ProviderDispatch, scope: ScreenShareScope): Promise<void> {
    const target = approval.target as ScreenShareTarget;
    requireValue(approval.launchStatus === undefined && validScreenShareTarget(target), 'screen_share_launch_already_attempted');
    // WorkEngine already flushed the attempted WorkRecord. Keep the launch
    // attempt and its system response on that record's bound local approval.
    approval.launchStatus = 'attempted';
    await this.persistApprovals();
    requireValue(this.scopeValid(scope) && this.originalBound(approval, dispatch) && this.foreground() &&
      ScreenShareOwner.liveFrameRevision(scope) > 0, 'screen_share_launch_readiness_lost');

    const want: Want = { bundleName: target.bundle_name, abilityName: target.ability_name };
    let timer: number = -1;
    const deadline = new Promise<string>((resolve): void => {
      timer = setTimeout((): void => { resolve('unknown'); }, Math.min(5000, scope.expiresAt - Date.now()));
    });
    let status: string;
    try {
      status = await Promise.race([this.context.startAbility(want).then((): string => 'system_acknowledged',
        (): string => 'system_rejected'), deadline]);
    } catch (_) { status = 'system_rejected'; }
    finally { clearTimeout(timer); }
    approval.launchStatus = status;
    // Exclude any frame already accepted into delivery when the system ACK was
    // observed. A later receiver ACK is independently required for completion.
    const baseline = status === 'system_acknowledged' ? ScreenShareOwner.liveFrameRevision(scope) : 0;
    if (status === 'system_acknowledged') { approval.launchAckBaseline = baseline; }
    await this.persistApprovals();
    if (status === 'system_rejected') { this.error = 'screen_share_launch_system_rejected'; }
    requireValue(status === 'system_acknowledged' && baseline > 0, 'screen_share_launch_outcome_unconfirmed');
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
    const scope = this.activeScope as ScreenShareScope;
    const authorization: ScreenShareAuthorization = {
      isForeground: (): boolean => this.foreground(),
      valid: (value: ScreenShareScope): boolean => this.scopeValid(value),
      onRevoked: (listener: () => void): (() => void) => {
        requireValue(this.revokedListeners.length < 4, 'screen_share_revocation_listener_limit');
        this.revokedListeners.push(listener);
        return (): void => { this.revokedListeners = this.revokedListeners.filter((entry: () => void): boolean => entry !== listener); };
      },
      closeHdc: (_value: ScreenShareScope): void => {}
    };
    const gate: PhoneScreenShareGate = {
      valid: (value: ScreenShareScope): boolean => this.scopeValid(value),
      serviceLive: (): boolean => this.eligible() && this.scopeValid(scope)
    };
    this.state = 'starting_capture';
    try {
      await this.persistApprovals();
      if (approval.standing === true) { await this.observeScreenState(); }
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
        if (approval.target !== undefined && approval.launchStatus === undefined) {
          requireValue(this.foreground(), 'screen_share_launch_readiness_lost');
          if (ScreenShareOwner.liveFrameRevision(scope) > 0) { await this.launchApp(approval, dispatch, scope); }
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
      if (approval.target !== undefined) {
        const baseline = approval.launchAckBaseline ?? 0;
        requireValue(approval.launchStatus === 'system_acknowledged' && baseline > 0 &&
          final.acknowledgedFrames >= 2 && final.lastFrameRevision > baseline, 'screen_share_launch_later_frame_unconfirmed');
        summary.format = 'amita-phone-screen-share-result/2'; summary.command = 'screen_share_open_app'; summary.readonly = false;
        summary.target = approval.target; summary.launch_system_acknowledged = true;
        summary.launch_baseline_frame_revision = baseline; summary.later_frame_revision = final.lastFrameRevision;
      }
      approval.state = 'closed'; await this.persistApprovals();
      return await controlBlob(summary);
    } catch (_) {
      const media = ScreenShareOwner.status();
      if (this.error === '' && media.error !== '') { this.error = safeCode(media.error); }
      approval.state = 'unknown';
      try { await this.persistApprovals(); }
      finally { try { await ScreenShareOwner.stop(); } catch (_) {} }
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
      this.originalBound(approval, dispatch) &&
      row !== undefined && row.result_json.length > 0 && row.dispatch_json === JSON.stringify(dispatch) &&
      row.permission_context === permissionContext && approval.permissionContext === permissionContext &&
      approval.receiptId === row.receipt_id && approval.invocationNonce === row.invocation_nonce && this.metadataOnly(row, approval);
  }

  private metadataOnly(row: WorkRecord, approval: LocalApproval): boolean {
    try {
      const blob = JSON.parse(row.result_json) as Blob; validateWorkBlob(blob);
      const codec = new util.Base64Helper(); const bytes = codec.decodeSync(blob.base64);
      if (bytes.length !== blob.bytes || bytes.length <= 0 || bytes.length > 4096 ||
        codec.encodeToStringSync(bytes) !== blob.base64) { return false; }
      const value = JSON.parse(new util.TextDecoder('utf-8', { fatal: true }).decodeToString(bytes)) as StreamSummary;
      const commonValid = value.operation_id === row.operation_id && value.task_id === row.task_id && value.task_epoch === row.task_epoch &&
        value.source_closed === true && value.capture_quiescent === true && /^[a-f0-9]{64}$/.test(value.last_ack_sha256) &&
        Number.isSafeInteger(value.acknowledged_frames) && value.acknowledged_frames > 0 &&
        Number.isSafeInteger(value.last_frame_revision) && value.last_frame_revision >= value.acknowledged_frames &&
        /^screen_share_[a-z_]{1,72}$/.test(value.stop_reason);
      const keys = Object.keys(value).sort().join('|');
      if (approval.target === undefined) {
        return commonValid && keys ===
          'acknowledged_frames|capture_quiescent|command|format|last_ack_sha256|last_frame_revision|operation_id|readonly|source_closed|stop_reason|task_epoch|task_id' &&
          value.format === 'amita-phone-screen-share-result/1' && value.command === 'screen_share' && value.readonly === true;
      }
      const baseline = approval.launchAckBaseline ?? 0;
      return commonValid && keys ===
        'acknowledged_frames|capture_quiescent|command|format|last_ack_sha256|last_frame_revision|later_frame_revision|launch_baseline_frame_revision|launch_system_acknowledged|operation_id|readonly|source_closed|stop_reason|target|task_epoch|task_id' &&
        value.format === 'amita-phone-screen-share-result/2' && value.command === 'screen_share_open_app' && value.readonly === false &&
        sameScreenShareTarget(value.target, approval.target) && approval.launchStatus === 'system_acknowledged' &&
        value.launch_system_acknowledged === true && Number.isSafeInteger(baseline) && baseline > 0 &&
        value.launch_baseline_frame_revision === baseline && value.later_frame_revision === value.last_frame_revision &&
        value.acknowledged_frames >= 2 && value.last_frame_revision > baseline;
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
    if (!this.foreground()) {
      if (!this.streamEligible() && (this.pendingApproval !== null || this.admissionApproval !== null ||
        this.executingApproval !== null)) { this.revoke('screen_share_background_ticket_required'); }
      this.pulse(); return;
    }
    this.observedUnknown.clear();
    if (!this.receptionPaused) { this.startPolling(); }
    this.reconcileMetadata().catch((): void => { this.error = 'screen_share_original_metadata_receipt_unconfirmed'; })
      .then((): Promise<void> => this.refreshReceiver());
  }

  stop(pauseReception: boolean = true): Promise<void> {
    if (this.stopping !== null) { return this.stopping; }
    this.manualReceiving = false;
    if (pauseReception) { this.receptionPaused = true; }
    this.revoke('screen_share_owner_stopped');
    clearInterval(this.pulseTimer); clearInterval(this.tickTimer);
    this.pulseTimer = -1; this.tickTimer = -1;
    this.stopping = (async (): Promise<void> => {
      try {
        if (pauseReception) { await this.persist('reception_paused', 'true'); }
      } finally {
        try { await ScreenShareOwner.stop(); }
        finally { await this.pulse(); this.stopping = null; }
      }
    })();
    return this.stopping;
  }

  async destroy(): Promise<void> {
    try { await this.stop(false); }
    finally {
      this.destroyed = true; clearInterval(this.pulseTimer); clearInterval(this.tickTimer);
      this.pulseTimer = -1; this.tickTimer = -1;
      if (ScreenShareRuntime.instance === this) { ScreenShareRuntime.instance = null; }
    }
  }

  pending(): ScreenSharePending | null {
    return this.pendingApproval?.standing === true ? null : this.pendingBinding();
  }

  private pendingBinding(): ScreenSharePending | null {
    const value = this.pendingApproval;
    if (value === null || value.state !== 'pending' || this.resolveApproval === null) { return null; }
    const pending: ScreenSharePending = { approvalId: value.approvalId, nonce: value.nonce, revision: value.revision,
      operationId: value.operationId, taskId: value.taskId, taskEpoch: value.taskEpoch,
      inputHash: value.inputHash, expiresAt: value.expiresAt, allocationDeadline: value.allocationDeadline,
      durationMs: value.requestedDurationMs, maxFps: value.maxFps, purpose: value.purpose };
    if (value.target !== undefined) {
      pending.target = { bundle_name: value.target.bundle_name, ability_name: value.target.ability_name };
    }
    return pending;
  }

  status(): ScreenShareRuntimeStatus {
    const value = new ScreenShareRuntimeStatus();
    const media = ScreenShareOwner.status();
    value.state = this.state; value.error = this.error; value.nodeId = this.nodeId;
    value.controlSessionId = this.identity.control_session_id; value.receiving = this.sourceLive();
    value.sourceInstalled = this.sourceInstalled; value.capabilityEpoch = this.capabilityEpoch;
    value.standingAllowed = this.sourceStandingAllowed; value.receptionPaused = this.receptionPaused;
    value.screenState = this.screenState;
    value.captureConsentPending = this.executingApproval !== null && media.state === 'awaiting_capture_consent';
    value.presenceExpiresAt = this.presenceExpires * 1000; value.foreground = this.foreground();
    value.captureActive = media.captureActive; value.backgroundActive = media.backgroundActive;
    value.cleanupUnknown = media.cleanupUnknown; value.sourceCloseUnknown = ScreenShareOwner.sourceCloseUnconfirmed();
    value.acknowledgedFrames = media.acknowledgedFrames; value.pending = this.pending() !== null;
    value.executing = this.executingApproval !== null;
    const original = this.pendingApproval === null ? this.executingApproval : this.pendingApproval;
    if (original !== null) {
      value.taskId = original.taskId; value.taskEpoch = original.taskEpoch; value.operationId = original.operationId;
      value.launchStatus = original.launchStatus ?? '';
      value.laterFrameAcknowledged = (original.launchAckBaseline ?? 0) > 0 &&
        media.lastFrameRevision > (original.launchAckBaseline ?? 0);
    }
    const records = this.engine === null ? [] : this.engine.snapshot.records;
    value.unknownRecords = records.filter((row: WorkRecord): boolean => row.state === 'unknown').length;
    value.pendingResults = records.filter((row: WorkRecord): boolean => row.result_json.length > 0 && row.state !== 'settled').length;
    value.settledRecords = records.filter((row: WorkRecord): boolean => row.state === 'settled').length;
    return value;
  }
}
