import { common } from '@kit.AbilityKit';
import { AccessibilityEvent, AccessibilityExtensionContext } from '@kit.AccessibilityKit';
import { preferences } from '@kit.ArkData';
import { deviceInfo } from '@kit.BasicServicesKit';
import { util } from '@kit.ArkTS';
import { http } from '@kit.NetworkKit';
import { Action as KeyAction, KeyCode, KeyEvent } from '@kit.InputKit';
import { Blob, requireValue } from '../core/Protocol';
import {
  PhoneResponse, ProviderDispatch, WorkConfig, WorkEngine, WorkHost, WorkRecord, WorkSnapshot,
  coldResumeWork, freshWorkSnapshot, validateWorkConfig, validateWorkDispatch
} from '../core/WorkProtocol';
import { NodeCredential, NodeCredentialStore } from '../security/NodeCredentialStore';
import { AccessibilityProvider } from './AccessibilityProvider';
import { ApprovalSurface } from './ApprovalSurface';
import { buildApprovalWantAgents } from './ApprovalWantAgents';
import { ApprovalTicket, ControlReceipt, ControlRequest, ControlSession, ControlView, OwnerControlDecision } from './ControlSession';
import { ControlStore } from './ControlStore';
import { GlobalScreenCapture, GlobalScreenImage } from './GlobalScreenCapture';
import {
  PhoneControlOwnerOptions, PhoneControlRuntimeStatus, freshControlPreferences, readPhoneControlOwner
} from './PhoneControlBridge';
import { PhoneControlInput, controlBlob, controlDigest, decodeControlInput } from './PhoneControlJSON';
import { RuntimeApprovalGate, RuntimePermit } from './RuntimeApprovalGate';
import { BackgroundDecisionBridge } from './BackgroundDecisionBridge';
import { PhoneControlReviewAcknowledgement } from './PhoneControlBridge';

interface NodeIdentity { node_id: string; }
interface RuntimeIdentity { format: string; node_id: string; control_session_id: string; }
interface ControlResultIdentity {
  format: string; contract: string; principal: string; operation_id: string;
  task_id: string; task_epoch: number; command: string;
}
interface ControlActionResult extends ControlResultIdentity { receipt: ControlReceipt; }
interface ControlInspectResult extends ControlResultIdentity { view: ControlView; }
interface ControlScreenshotPayload {
  format: string; width: number; height: number; display_id: number;
  crop_left: number; crop_top: number; scale_x: number; scale_y: number;
  user_selected: boolean; bytes: number; sha256: string; base64: string;
}
interface ControlScreenshotResult extends ControlResultIdentity { screenshot: ControlScreenshotPayload; }

// The actual AccessibilityExtension lifecycle owns this loop. It neither
// borrows EntryAbility foreground state nor creates a second physical node.
export class PhoneControlRuntime implements WorkHost {
  private context: common.Context;
  private nodeId: string;
  private identity: RuntimeIdentity;
  private store: preferences.Preferences;
  private statusStore: preferences.Preferences;
  private credentials: NodeCredentialStore = new NodeCredentialStore();
  private provider: AccessibilityProvider;
  private control: ControlSession;
  private capture: GlobalScreenCapture = new GlobalScreenCapture();
  private gate: RuntimeApprovalGate = new RuntimeApprovalGate();
  private background: BackgroundDecisionBridge;
  private readPermit: RuntimePermit | null = null;
  private engine: WorkEngine | null = null;
  private owner: PhoneControlOwnerOptions = new PhoneControlOwnerOptions();
  private ownerApplied: string = '';
  private active: boolean = false;
  private connected: boolean = false;
  private busy: boolean = false;
  private pulsing: boolean = false;
  private publishing: boolean = false;
  private deciding: boolean = false;
  private timer: number = -1;
  private lastTick: number = 0;
  private lastDecision: string = '';
  private lastReview: string = '';
  private surfaceBusy: boolean = false;
  private lastSurface: string = '';
  private held: Map<number, number> = new Map();
  private decisionKey: number = -1;
  private decisionPressedTime: number = -1;
  private state: PhoneControlRuntimeStatus = new PhoneControlRuntimeStatus();

  private constructor(context: common.Context, nodeId: string) {
    this.context = context; this.nodeId = nodeId;
    this.store = preferences.getPreferencesSync(context, { name: 'amita_control_runtime_v1' });
    this.statusStore = preferences.getPreferencesSync(context, { name: 'amita_control_status_v1' });
    const saved: string = this.store.getSync('identity', '') as string;
    this.identity = saved.length === 0 ? { format: 'amita-phone-control-runtime-identity/1', node_id: nodeId,
      control_session_id: 'harmony-control-' + util.generateRandomUUID() } : JSON.parse(saved) as RuntimeIdentity;
    requireValue(this.identity.format === 'amita-phone-control-runtime-identity/1' && this.identity.node_id === nodeId &&
      /^[A-Za-z0-9._:-]{1,160}$/.test(this.identity.control_session_id), 'phone_control_original_identity_required');
    this.provider = new AccessibilityProvider(nodeId, (_reason: string): void => { this.activity(); });
    this.provider.setAdmissionPolicy((): boolean => {
      if (!this.active) { return false; }
      try {
        const owner: PhoneControlOwnerOptions = readPhoneControlOwner(this.context);
        return owner.enabled && owner.grant_id === this.owner.grant_id;
      } catch (error) { return false; }
    });
    const controlStore: ControlStore = new ControlStore(context, nodeId);
    this.control = new ControlSession(controlStore.load(), nodeId, controlStore, this.provider);
    this.background = new BackgroundDecisionBridge(async (value: OwnerControlDecision): Promise<void> => {
      const p: RuntimePermit | null = this.gate.current();
      requireValue(p !== null && p.background_agents_verified === true, 'phone_control_background_creator_not_verified');
      await this.decide(value, true);
    });
    this.state.node_id = nodeId; this.state.principal = 'node:' + nodeId;
    this.state.control_session_id = this.identity.control_session_id;
  }
  static async create(context: common.Context): Promise<PhoneControlRuntime> {
    try {
      const original: preferences.Preferences = freshControlPreferences(context, 'mesh_node_v1');
      const raw: string = original.getSync('snapshot', '') as string;
      requireValue(raw.length > 0, 'phone_control_existing_node_required');
      const identity: NodeIdentity = JSON.parse(raw) as NodeIdentity;
      requireValue(typeof identity.node_id === 'string' && /^[A-Za-z0-9._:-]{1,160}$/.test(identity.node_id),
        'phone_control_existing_node_required');
      const runtime: PhoneControlRuntime = new PhoneControlRuntime(context, identity.node_id);
      runtime.store.putSync('identity', JSON.stringify(runtime.identity)); await runtime.store.flush();
      await runtime.control.initialize();
      await runtime.configureFromMember();
      await runtime.publish();
      return runtime;
    } catch (error) { throw new Error('phone_control_runtime_private_state_unavailable'); }
  }
  async configureFromMember(): Promise<void> {
    const memberStore: preferences.Preferences = freshControlPreferences(this.context, 'mesh_work_v1');
    const memberRaw: string = memberStore.getSync('snapshot', '') as string;
    if (memberRaw.length === 0) { return; }
    const member: WorkSnapshot = JSON.parse(memberRaw) as WorkSnapshot;
    if (member.config === null) { return; }
    validateWorkConfig(member.config, this.nodeId);
    requireValue(member.config.session_id !== this.identity.control_session_id, 'phone_control_independent_session_required');
    const config: WorkConfig = { format: member.config.format, resource_url: member.config.resource_url,
      node_id: member.config.node_id, session_id: this.identity.control_session_id,
      mesh_id: member.config.mesh_id, authority_id: member.config.authority_id };
    if (this.engine !== null) {
      requireValue(this.engine.snapshot.config === null || JSON.stringify(this.engine.snapshot.config) === JSON.stringify(config),
        'phone_control_original_route_changed');
      if (this.engine.snapshot.config === null) { this.engine.snapshot.config = config; await this.save(this.engine.snapshot); }
      return;
    }
    const raw: string = this.store.getSync('work_snapshot', '') as string;
    const snapshot: WorkSnapshot = raw.length === 0 ? freshWorkSnapshot(this.nodeId) : JSON.parse(raw) as WorkSnapshot;
    coldResumeWork(snapshot, this.nodeId);
    requireValue(snapshot.config === null || JSON.stringify(snapshot.config) === JSON.stringify(config),
      'phone_control_original_route_changed');
    snapshot.config = config;
    this.engine = new WorkEngine(snapshot, this);
    await this.save(snapshot);
  }
  async onConnect(context: AccessibilityExtensionContext): Promise<void> {
    this.active = true;
    try { await this.provider.onConnect(context); this.state.error = ''; }
    catch (error) { this.state.error = 'phone_control_accessibility_root_unavailable'; }
    if (!this.active) { return; }
    await this.background.start();
    if (!this.active) { await this.background.stop(); return; }
    await this.applyOwner();
    if (this.timer !== -1) { clearInterval(this.timer); }
    this.timer = setInterval((): void => { this.pulse(); }, 1000);
    await this.publish(); this.tick();
  }
  async onDisconnect(): Promise<void> {
    this.active = false; this.provider.onDisconnect(); this.gate.cancel(); this.readPermit = null;
    this.control.setOwnerEnabled(false); this.capture.setOwnerCaptureAllowed(false);
    await this.background.stop();
    if (this.timer !== -1) { clearInterval(this.timer); this.timer = -1; }
    this.state.service_connected = false; this.state.connection = 'service_disconnected';
    await this.publish();
    if (!this.busy) { await this.withdraw(); }
  }
  onAccessibilityEvent(event: AccessibilityEvent): void { this.provider.onAccessibilityEvent(event); }
  onKeyEvent(event: KeyEvent): boolean {
    const code: number = event.key.code;
    if (event.action === KeyAction.UP || event.action === KeyAction.CANCEL) {
      this.held.delete(code);
      if (code === this.decisionKey && event.key.pressedTime === this.decisionPressedTime) {
        this.decisionKey = -1; this.decisionPressedTime = -1;
        return true; // Only the release paired with the consumed decision press.
      }
    }
    const wasHeld: boolean = this.held.has(code);
    if (event.action === KeyAction.DOWN) { this.held.set(code, event.key.pressedTime); }
    const permit: RuntimePermit | null = this.gate.current();
    const decision: string = code === KeyCode.KEYCODE_VOLUME_UP ? 'approve' : code === KeyCode.KEYCODE_VOLUME_DOWN ? 'deny' : '';
    if (permit !== null && ['awaiting_approval', 'awaiting_revalidation'].includes(permit.state) && permit.hardware_armed &&
      permit.ticket.expires_at > Date.now() && event.action === KeyAction.DOWN && !wasHeld &&
      !event.ctrlKey && !event.altKey && !event.shiftKey && event.keys.length === 1 && decision.length > 0 &&
      !(decision === 'approve' && permit.ticket.review_required && permit.details_reviewed !== true)) {
      this.decisionKey = code; this.decisionPressedTime = event.key.pressedTime;
      this.provider.noteOwnerDecisionKey();
      this.decide(this.decisionFor(permit.ticket, decision), permit.state === 'awaiting_revalidation').catch((): void => {});
      return true; // Match the explicit decision BEFORE the ordinary input fence.
    }
    return this.provider.onKeyEvent(event);
  }
  private decisionFor(ticket: ApprovalTicket, decision: string): OwnerControlDecision {
    return { approval_id: ticket.approval_id, nonce: ticket.nonce, operation_id: ticket.operation_id,
      task_id: ticket.task_id, revision: ticket.revision, expires_at: ticket.expires_at, decision: decision };
  }
  private activity(): void {
    this.gate.invalidateForRevalidation();
    if (this.readPermit !== null) { this.readPermit.fence.valid = false; this.readPermit = null; }
    if (this.control !== undefined) { this.control.ownerActivity(); }
  }
  private ownerAllowedNow(): boolean {
    if (!this.active) { return false; }
    try { return readPhoneControlOwner(this.context).enabled; } catch (error) { return false; }
  }
  private async applyOwner(): Promise<void> {
    const owner: PhoneControlOwnerOptions = readPhoneControlOwner(this.context);
    const root: boolean = this.provider.status().root_verified;
    const key: string = owner.grant_id + '|' + owner.enabled + '|' + root;
    if (key === this.ownerApplied) { return; }
    this.gate.cancel(); this.control.setOwnerEnabled(false); // Explicit policy changes cancel dormant candidates too.
    this.activity(); this.owner = owner; this.ownerApplied = key;
    this.provider.setOwnerControlAllowed(owner.enabled && root);
    this.provider.setGestureFallbackAllowed(owner.enabled && root && owner.gesture_fallback);
    this.control.setOwnerEnabled(owner.enabled && root);
    this.capture.setOwnerCaptureAllowed(owner.enabled && owner.screen_capture);
    this.lastTick = 0;
  }
  private capabilities(): string[] {
    return this.active && this.ownerAllowedNow() && this.provider.availability().inspect ? [this.nodeId + ':ui-control-v1'] : [];
  }
  async save(snapshot: WorkSnapshot): Promise<void> {
    this.store.putSync('work_snapshot', JSON.stringify(snapshot)); await this.store.flush();
  }
  id(): string { return util.generateRandomUUID(); }
  now(): number { return Math.floor(Date.now() / 1000); }
  private currentPermit(): RuntimePermit | null { return this.gate.current() === null ? this.readPermit : this.gate.current(); }
  permission(dispatch: ProviderDispatch): string {
    const permit: RuntimePermit | null = this.currentPermit();
    return permit !== null && permit.dispatch_json === JSON.stringify(dispatch) ? permit.permission_context : '';
  }
  allowed(dispatch: ProviderDispatch, permissionContext?: string): boolean {
    const p: RuntimePermit | null = this.currentPermit();
    if (!this.active || !this.ownerAllowedNow() || p === null || p.state !== 'approved' || !p.fence.valid ||
      p.ticket.expires_at <= Date.now() || p.dispatch_json !== JSON.stringify(dispatch) ||
      (permissionContext !== undefined && permissionContext !== p.permission_context) ||
      this.state.presence_expires_at <= this.now()) { return false; }
    const owner: PhoneControlOwnerOptions = readPhoneControlOwner(this.context);
    if (owner.grant_id !== p.owner_grant || !this.provider.availability().inspect) { return false; }
    if (p.action_kind !== 'action') { return p.action_kind !== 'screenshot' || owner.screen_capture; }
    try { return this.provider.availability().act && this.control.approvalCurrent(dispatch.operation_id); }
    catch (error) { return false; }
  }
  reportAllowed(dispatch: ProviderDispatch, permissionContext: string): boolean {
    if (!this.active || !this.ownerAllowedNow() || this.engine === null) { return false; }
    const owner: PhoneControlOwnerOptions = readPhoneControlOwner(this.context);
    const parts: string[] = permissionContext.split('|');
    const row: WorkRecord | undefined = this.engine.snapshot.records.find((item: WorkRecord): boolean =>
      item.operation_id === dispatch.operation_id && item.capability_id === dispatch.capability_id);
    return row !== undefined && row.result_json.length > 0 && row.permission_context === permissionContext &&
      row.dispatch_json === JSON.stringify(dispatch) && parts.length === 4 && parts[1] === owner.grant_id &&
      ['gui-read', 'gui-screen', 'gui-action'].includes(parts[0]) &&
      (parts[0] !== 'gui-screen' || owner.screen_capture);
  }
  async preflight(dispatch: ProviderDispatch): Promise<void> {
    validateWorkDispatch(dispatch, this.nodeId);
    requireValue(dispatch.capability_id === this.nodeId + ':ui-control-v1' && this.active && this.ownerAllowedNow() &&
      this.provider.availability().inspect, 'phone_control_local_provider_unavailable');
    const kind: string = dispatch.selection?.action as string;
    const input: Blob = dispatch.selection?.workload.input as Blob;
    const body: PhoneControlInput = await decodeControlInput(input, kind);
    const owner: PhoneControlOwnerOptions = readPhoneControlOwner(this.context);
    const deadline: number = Math.min(Date.now() + 20000, (dispatch.deadline as number) * 1000 - 1000,
      this.state.presence_expires_at * 1000 - 3000);
    requireValue(deadline > Date.now() + 1000 && owner.enabled && owner.grant_id.length > 0,
      'phone_control_approval_deadline_unavailable');
    this.gate.cancel(); this.readPermit = null;
    let ticket: ApprovalTicket;
    let actionKind: string = 'inspect';
    if (body.command === 'perform_action') {
      const lease = this.control.acquire(dispatch.task_id as string, Math.floor(deadline - Date.now()));
      const request: ControlRequest = { operation_id: dispatch.operation_id, task_id: dispatch.task_id as string,
        action: body.action as string, element_ref: body.element_ref as string, text: body.text as string };
      try { ticket = await this.control.request(request, lease.lease_id); }
      catch (error) { this.control.ownerActivity(); throw new Error('phone_control_original_action_preflight_unavailable'); }
      ticket.summary += ticket.review_required ? ' · 请查看完整输入；音量−拒绝' : ' · 音量＋允许这一步 / 音量−拒绝';
      actionKind = 'action';
    } else {
      actionKind = body.command === 'screenshot' ? 'screenshot' : 'inspect';
      requireValue(actionKind !== 'screenshot' || owner.screen_capture && this.capture.status().permission_granted,
        'phone_control_screen_capture_permission_required');
      ticket = { approval_id: this.id(), nonce: this.id(), operation_id: dispatch.operation_id,
        task_id: dispatch.task_id as string, revision: 0, expires_at: deadline,
        summary: '既有本人授权下的只读观察', review_required: false };
    }
    const prefix: string = actionKind === 'action' ? 'gui-action' : actionKind === 'screenshot' ? 'gui-screen' : 'gui-read';
    const permit: RuntimePermit = { ticket: ticket, dispatch_json: JSON.stringify(dispatch), input_json: JSON.stringify(body),
      input_hash: input.sha256, owner_grant: owner.grant_id,
      permission_context: prefix + '|' + owner.grant_id + '|' + ticket.approval_id + '|' + input.sha256,
      action_kind: actionKind, fence: { valid: true, human_input_epoch: 0 },
      state: actionKind === 'action' ? 'awaiting_approval' : 'approved', hardware_armed: false };
    if (actionKind !== 'action') { this.readPermit = permit; return; }
    this.lastSurface = 'presenting';
    const waiting: Promise<boolean> = this.gate.begin(permit);
    await this.publish();
    await this.presentPending();
    await this.publish();
    const approved: boolean = await waiting;
    if (!approved) { this.control.ownerActivity(); }
    requireValue(approved && this.allowed(dispatch), 'phone_control_owner_approval_denied_expired_or_stale');
  }
  async execute(dispatch: ProviderDispatch): Promise<Blob> {
    requireValue(this.allowed(dispatch), 'phone_control_same_operation_approval_required');
    const p: RuntimePermit = this.currentPermit() as RuntimePermit;
    const body: PhoneControlInput = JSON.parse(p.input_json) as PhoneControlInput;
    p.state = 'consumed';
    if (p.action_kind === 'action') {
      const receipt = await this.control.execute(dispatch.operation_id);
      requireValue(receipt.state === 'input_acknowledged', 'phone_control_original_action_outcome_unknown');
      const result: ControlActionResult = { format: 'amita-phone-ui-control-result/1', contract: 'amita-phone-ui-control/1',
        principal: this.provider.principal(), operation_id: dispatch.operation_id, task_id: dispatch.task_id as string,
        task_epoch: dispatch.task_epoch as number, command: body.command, receipt: receipt };
      return controlBlob(result);
    }
    if (p.action_kind === 'screenshot') {
      const picture: GlobalScreenImage = await this.capture.captureApproved(p.ticket.expires_at, p.fence, true);
      const codec: util.Base64Helper = new util.Base64Helper();
      const screenshot: ControlScreenshotPayload = { format: picture.format, width: picture.width, height: picture.height,
        display_id: picture.display_id, crop_left: picture.crop_left, crop_top: picture.crop_top,
        scale_x: picture.scale_x, scale_y: picture.scale_y, user_selected: false,
        bytes: picture.bytes.length, sha256: await controlDigest(picture.bytes), base64: codec.encodeToStringSync(picture.bytes) };
      const result: ControlScreenshotResult = { format: 'amita-phone-ui-control-result/1', contract: 'amita-phone-ui-control/1',
        principal: this.provider.principal(), operation_id: dispatch.operation_id, task_id: dispatch.task_id as string,
        task_epoch: dispatch.task_epoch as number, command: body.command, screenshot: screenshot };
      return controlBlob(result);
    }
    const view = await this.control.inspect(body.max_elements as number);
    requireValue(p.fence.valid && this.ownerAllowedNow(), 'phone_control_observation_permission_revoked');
    const result: ControlInspectResult = { format: 'amita-phone-ui-control-result/1', contract: 'amita-phone-ui-control/1',
      principal: this.provider.principal(), operation_id: dispatch.operation_id, task_id: dispatch.task_id as string,
      task_epoch: dispatch.task_epoch as number, command: body.command, view: view };
    return controlBlob(result);
  }
  async exchange(action: string, args: Object): Promise<PhoneResponse> {
    requireValue(this.engine !== null && this.engine.snapshot.config !== null && deviceInfo.sdkApiVersion >= 23,
      'phone_control_secure_configuration_required');
    const config: WorkConfig = (this.engine as WorkEngine).snapshot.config as WorkConfig;
    validateWorkConfig(config, this.nodeId);
    const credential: NodeCredential | null = await this.credentials.load();
    requireValue(credential !== null && credential.nodeId === this.nodeId && credential.scope !== undefined &&
      credential.scope.includes('phone:node') && (credential.expiresAt === undefined || credential.expiresAt > this.now()),
      'phone_control_original_node_credential_required');
    const request: http.HttpRequest = http.createHttp();
    try {
      const response: http.HttpResponse = await request.request(config.resource_url, {
        method: http.RequestMethod.POST, header: { 'Authorization': 'Bearer ' + (credential as NodeCredential).token,
          'Accept': 'application/json', 'Content-Type': 'application/json' }, extraData: JSON.stringify({ action: action, arguments: args }),
        expectDataType: http.HttpDataType.STRING, connectTimeout: 8000, readTimeout: 8000,
        usingCache: false, maxLimit: 1048576, maxRedirects: 0 });
      requireValue(response.responseCode === 200 && typeof response.result === 'string' &&
        (response.result as string).length <= 1048576, 'phone_control_authority_unavailable');
      return JSON.parse(response.result as string) as PhoneResponse;
    } finally { request.destroy(); }
  }
  private observe(response: PhoneResponse): void {
    if (response.session === undefined || response.session.network === undefined ||
      !Number.isFinite(response.session.network.expires_at)) { throw new Error('phone_control_presence_response_required'); }
    this.state.connection = response.session.network.state;
    this.state.presence_expires_at = response.session.network.expires_at;
  }
  private async withdraw(): Promise<void> {
    if (this.engine === null || this.engine.snapshot.config === null || !this.connected) { return; }
    try { this.observe(await this.engine.control(false, [])); }
    catch (error) { this.state.error = 'phone_control_offline_withdrawal_unconfirmed'; }
    finally { this.connected = false; await this.publish(); }
  }
  private async tick(): Promise<void> {
    if (this.busy || !this.active) { return; }
    this.busy = true; this.lastTick = Date.now();
    try {
      await this.configureFromMember();
      if (this.engine === null || this.engine.snapshot.config === null) { this.state.connection = 'unconfigured'; return; }
      const engine: WorkEngine = this.engine;
      const capabilities: string[] = this.capabilities();
      this.observe(this.connected ? await engine.control(true, capabilities) : await engine.connect(capabilities));
      this.connected = true;
      for (let i: number = 0, inspected: number = 0; i < engine.snapshot.records.length && inspected < 4 && this.active; i++) {
        if (engine.snapshot.records[i].state !== 'settled') { await engine.reconcile(engine.snapshot.records[i]); inspected++; }
      }
      if (this.active && capabilities.length > 0 && this.state.presence_expires_at > this.now() + 5) {
        const pending: ProviderDispatch[] = await engine.pull();
        for (let i: number = 0; i < pending.length && i < 2 && this.active; i++) {
          try { await engine.run(pending[i]); }
          catch (error) { this.state.error = 'phone_control_original_dispatch_unavailable'; }
          finally {
            if (this.gate.current() !== null) { this.control.ownerActivity(); }
            this.gate.cancel(); this.readPermit = null;
          }
        }
      }
    } catch (error) { this.connected = false; this.state.connection = 'unreachable'; this.state.error = 'phone_control_connection_or_receipt_unconfirmed'; }
    finally { this.busy = false; await this.publish(); if (!this.active) { await this.withdraw(); } }
  }
  private sourcePrepared(permit: RuntimePermit): boolean {
    if (this.engine === null || permit.dispatch_json.length === 0) { return false; }
    const original: ProviderDispatch = JSON.parse(permit.dispatch_json) as ProviderDispatch;
    const row: WorkRecord | undefined = this.engine.snapshot.records.find((item: WorkRecord): boolean =>
      item.operation_id === permit.ticket.operation_id && item.capability_id === original.capability_id);
    return row !== undefined && row.state === 'prepared' && row.invocation_nonce === '' && row.result_json === '' &&
      row.task_id === permit.ticket.task_id && row.task_epoch === original.task_epoch && row.authority_epoch === original.epoch &&
      row.dispatch_json === permit.dispatch_json && row.last_remote_state === 'pending';
  }
  private async presentPending(): Promise<void> {
    const p: RuntimePermit | null = this.gate.current();
    if (this.surfaceBusy || p === null || !['awaiting_approval', 'awaiting_revalidation'].includes(p.state)) { return; }
    this.surfaceBusy = true;
    try {
      const t: ApprovalTicket = p.ticket;
      const display: ApprovalTicket = { approval_id: t.approval_id, nonce: t.nonce, operation_id: t.operation_id,
        task_id: t.task_id, revision: t.revision, expires_at: t.expires_at,
        summary: p.details_reviewed === true ? t.summary + ' · 完整内容已核对；回原窗口后明确决定' : t.summary,
        review_required: t.review_required && p.details_reviewed !== true };
      const useBackground: boolean = this.background.status().subscribed;
      const actions = await buildApprovalWantAgents(display, useBackground);
      p.background_agents_verified = useBackground;
      const surface: ApprovalSurface = new ApprovalSurface(731901, this.owner.liveview_event);
      const presentation = await surface.present(display, actions);
      this.lastSurface = presentation.surface;
      if (presentation.presented) { this.gate.armHardware(); }
    } catch (error) {
      p.background_agents_verified = false;
      this.lastSurface = 'in_app_pending'; this.state.error = 'phone_control_approval_surface_unconfirmed';
    } finally { this.surfaceBusy = false; }
  }
  private async acknowledgeReview(value: PhoneControlReviewAcknowledgement): Promise<void> {
    const p: RuntimePermit | null = this.gate.current();
    requireValue(p !== null && p.ticket.review_required && p.input_json === value.input_json &&
      this.gate.matches({ approval_id: value.approval_id, nonce: value.nonce, operation_id: value.operation_id,
        task_id: value.task_id, revision: value.revision, expires_at: value.expires_at, decision: 'approve' }) &&
      this.ownerAllowedNow() && readPhoneControlOwner(this.context).grant_id === p.owner_grant && this.sourcePrepared(p),
      'phone_control_review_acknowledgement_mismatch');
    (p as RuntimePermit).details_reviewed = true;
    await this.presentPending(); // Only enables the later explicit decision; never approves now.
  }
  async decide(value: OwnerControlDecision, explicitRevalidation: boolean = false): Promise<void> {
    if (this.deciding && value.decision === 'deny' && this.gate.matches(value)) {
      // A fresh exact owner rejection during asynchronous revalidation revokes
      // immediately; it does not wait behind the outstanding approval write.
      this.gate.cancel(); this.control.ownerActivity();
      this.state.error = 'phone_control_owner_denied_during_revalidation';
      await this.publish(); return;
    }
    if (this.deciding || !this.gate.matches(value)) { throw new Error('phone_control_pending_decision_mismatch'); }
    const p: RuntimePermit = this.gate.current() as RuntimePermit;
    requireValue(this.ownerAllowedNow() && readPhoneControlOwner(this.context).grant_id === p.owner_grant,
      'phone_control_owner_grant_revoked');
    requireValue(value.decision !== 'approve' || !p.ticket.review_required || p.details_reviewed === true,
      'phone_control_complete_private_review_required');
    this.deciding = true;
    try {
      if (explicitRevalidation && value.decision === 'approve') {
        await this.control.decideAfterRevalidation(value, (): boolean => this.sourcePrepared(p));
        requireValue(this.control.approvalCurrent(value.operation_id) && this.sourcePrepared(p),
          'phone_control_revalidated_decision_interrupted');
        p.fence = { valid: true, human_input_epoch: 0 };
      } else { await this.control.decideBound(value); }
      this.gate.complete(value.decision === 'approve');
    } catch (error) {
      let pending: boolean = false;
      try { pending = this.control.receipt(value.operation_id).state === 'awaiting_approval'; } catch (readError) {}
      if (explicitRevalidation && pending && this.sourcePrepared(p) && p.ticket.expires_at > Date.now()) {
        this.gate.invalidateForRevalidation(); this.state.error = 'phone_control_return_to_original_window_and_decide_again';
      } else { this.gate.cancel(); this.state.error = 'phone_control_local_decision_unconfirmed'; }
      throw new Error('phone_control_local_decision_unconfirmed');
    }
    finally { this.deciding = false; await this.publish(); }
  }
  private async pulse(): Promise<void> {
    if (this.pulsing || !this.active) { return; }
    this.pulsing = true;
    try {
      await this.applyOwner();
      const mailbox: preferences.Preferences = freshControlPreferences(this.context, 'amita_control_decision_v1');
      const raw: string = mailbox.getSync('decision', '') as string;
      const review: string = mailbox.getSync('review_ack', '') as string;
      if (review.length > 0 && review !== this.lastReview) {
        this.lastReview = review;
        try { await this.acknowledgeReview(JSON.parse(review) as PhoneControlReviewAcknowledgement); }
        catch (error) { this.state.error = 'phone_control_review_acknowledgement_rejected_or_stale'; }
      }
      if (raw.length > 0 && raw !== this.lastDecision) {
        this.lastDecision = raw;
        try { await this.decide(JSON.parse(raw) as OwnerControlDecision, true); }
        catch (error) { this.state.error = 'phone_control_local_decision_rejected_or_stale'; }
      }
      await this.publish();
      if (Date.now() - this.lastTick >= 8000) { this.tick(); }
    } catch (error) { this.activity(); this.state.error = 'phone_control_owner_bridge_unavailable'; await this.publish(); }
    finally { this.pulsing = false; }
  }
  private async publish(): Promise<void> {
    if (this.publishing) { return; }
    this.publishing = true;
    try {
      const p: RuntimePermit | null = this.gate.current();
      const pending: boolean = p !== null && ['awaiting_approval', 'awaiting_revalidation'].includes(p.state);
      const background = this.background.status();
      this.state.background_decision_subscribed = background.subscribed;
      this.state.background_decision_callback_verified = background.callback_verified;
      this.state.background_decision_error = background.error;
      this.state.background_decision_publisher = background.observed_publisher;
      this.state.background_agents_verified = pending && (p as RuntimePermit).background_agents_verified === true;
      this.state.pending_revalidation = pending && (p as RuntimePermit).state === 'awaiting_revalidation';
      this.state.service_connected = this.active; this.state.root_verified = this.provider.status().root_verified;
      this.state.owner_enabled = this.ownerAllowedNow(); this.state.configured = this.engine !== null && this.engine.snapshot.config !== null;
      this.state.pending_operation_id = pending ? (p as RuntimePermit).ticket.operation_id : '';
      this.state.pending_task_id = pending ? (p as RuntimePermit).ticket.task_id : '';
      const dispatch: ProviderDispatch | null = pending ? JSON.parse((p as RuntimePermit).dispatch_json) as ProviderDispatch : null;
      this.state.pending_task_epoch = dispatch === null ? 0 : dispatch.task_epoch as number;
      this.state.pending_expires_at = pending ? (p as RuntimePermit).ticket.expires_at : 0;
      this.state.pending_summary = pending ? (p as RuntimePermit).ticket.summary : '';
      this.state.pending_review_required = pending && (p as RuntimePermit).ticket.review_required &&
        (p as RuntimePermit).details_reviewed !== true;
      this.state.approval_surface = pending ? this.lastSurface : '';
      const records: WorkRecord[] = this.engine === null ? [] : this.engine.snapshot.records;
      this.state.tasks = records.length; this.state.settled = records.filter((r: WorkRecord): boolean => r.state === 'settled').length;
      this.state.unknown = records.filter((r: WorkRecord): boolean => r.state === 'unknown').length;
      this.statusStore.putSync('status', JSON.stringify(this.state));
      this.statusStore.putSync('pending', pending ? JSON.stringify({ ticket: (p as RuntimePermit).ticket,
        task_epoch: this.state.pending_task_epoch, input_json: (p as RuntimePermit).input_json }) : '');
      await this.statusStore.flush();
    } finally { this.publishing = false; }
  }
}
