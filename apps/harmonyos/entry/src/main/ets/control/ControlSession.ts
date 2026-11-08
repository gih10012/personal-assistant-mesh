// No platform imports: the same approval/identity code is exercised by Node.
// This is a controller for an authorised provider, never an input injector.
export interface ControlBounds { left: number; top: number; width: number; height: number; }
export interface ProviderElement {
  key: string; role: string; label: string; sensitive: boolean;
  enabled: boolean; actions: string[]; bounds: ControlBounds;
  state_fingerprint?: string;
}
export interface ProviderView {
  bundle_name: string; window_id: string; view_token: string;
  width: number; height: number; truncated: boolean; elements: ProviderElement[];
}
export interface ControlElement {
  element_ref: string; role: string; label: string; sensitive: boolean;
  enabled: boolean; actions: string[]; bounds: ControlBounds;
  state_fingerprint?: string;
}
export interface ControlView {
  revision: number; bundle_name: string; window_id: string; view_ref: string;
  width: number; height: number; truncated: boolean; elements: ControlElement[];
}
export interface ControlAvailability {
  provider: string; inspect: boolean; act: boolean; human_input_observable: boolean; reason: string;
}
export interface ControlFence { valid: boolean; human_input_epoch: number; }
export interface ControlInvocation {
  operation_id: string; bundle_name: string; window_id: string; view_token: string;
  element_key: string; action: string; text: string; fence: ControlFence;
}
export interface ControlProvider {
  availability(): ControlAvailability;
  inspect(maxElements: number): Promise<ProviderView>;
  // Immediately before delivering input, revalidate active window, view_token,
  // actual element identity and fence. Acknowledgement does not verify effect.
  act(invocation: ControlInvocation): Promise<boolean>;
}
export interface ControlRequest {
  operation_id: string; task_id: string; action: string; element_ref: string; text: string;
}
export interface ControlRecord {
  operation_id: string; task_id: string; request_json: string; revision: number;
  approval_id: string; nonce: string; lease_id: string; expires_at: number; state: string;
}
export interface ControlState { format: string; node_id: string; records: ControlRecord[]; }
export interface ControlHost {
  now(): number; // milliseconds
  id(): string; // unpredictable local cryptographic identifier
  // Atomically commit in app-private storage; serialise one writer per phone
  // screen resource. Never acknowledge this promise before durable completion.
  save(state: ControlState): Promise<void>;
}
export interface ControlLease { task_id: string; lease_id: string; expires_at: number; }
export interface ApprovalTicket {
  approval_id: string; nonce: string; operation_id: string; task_id: string; revision: number;
  expires_at: number; summary: string; review_required: boolean;
}
export interface OwnerControlDecision {
  approval_id: string; nonce: string; operation_id: string; task_id: string;
  revision: number; expires_at: number; decision: string;
}
export interface ControlReceipt {
  operation_id: string; state: string; input_acknowledged: boolean;
  effect_verified: boolean; automatic_replay: boolean;
}
interface StableProviderView {
  bundle_name: string; window_id: string; width: number; height: number;
  truncated: boolean; elements: ProviderElement[];
}
interface DecisionCandidate {
  request_json: string; fingerprint: string; target_key: string;
}
interface RevalidatedExecution {
  approval_id: string; request_json: string; revision: number; human_epoch: number;
  lease_id: string; target_key: string; fingerprint: string;
}
function viewFingerprint(value: ProviderView): string {
  const stable: StableProviderView = { bundle_name: value.bundle_name, window_id: value.window_id,
    width: value.width, height: value.height, truncated: value.truncated, elements: value.elements };
  return JSON.stringify(stable); // Exact bounded in-memory equality; not an external authentication hash.
}
function requireControl(ok: boolean, code: string): void { if (!ok) { throw new Error(code); } }
function identifier(value: string): boolean {
  return typeof value === 'string' && /^[A-Za-z0-9._:-]{1,180}$/.test(value);
}
function canonicalRequest(value: ControlRequest): string {
  requireControl(value !== null && typeof value === 'object' && !Array.isArray(value) &&
    Object.keys(value).sort().join('|') === ['operation_id', 'task_id', 'action', 'element_ref', 'text'].sort().join('|'),
    'phone_control_request_invalid');
  requireControl(identifier(value.operation_id) && identifier(value.task_id) && identifier(value.element_ref) &&
    ['tap', 'set_text', 'scroll_forward', 'scroll_backward'].includes(value.action) &&
    typeof value.text === 'string' && value.text.length <= 4096 &&
    (value.action === 'set_text' || value.text === ''), 'phone_control_request_invalid');
  return JSON.stringify({ operation_id: value.operation_id, task_id: value.task_id,
    action: value.action, element_ref: value.element_ref, text: value.text });
}
function recordedRequest(raw: string): ControlRequest {
  try {
    const value: ControlRequest = JSON.parse(raw) as ControlRequest;
    requireControl(canonicalRequest(value) === raw, 'phone_control_private_state_invalid');
    return value;
  } catch (error) { throw new Error('phone_control_private_state_invalid'); }
}
export function freshControlState(nodeId: string): ControlState {
  requireControl(identifier(nodeId), 'phone_control_node_invalid');
  return { format: 'amita-phone-control-state/1', node_id: nodeId, records: [] };
}

export class ControlSession {
  private state: ControlState;
  private host: ControlHost;
  private provider: ControlProvider;
  private sessionId: string;
  private enabled: boolean = false;
  private ready: boolean = false;
  private busy: boolean = false;
  private humanEpoch: number = 0;
  private revision: number = 0;
  private lease: ControlLease | null = null;
  private view: ProviderView | null = null;
  private observed: ControlView | null = null;
  private fence: ControlFence | null = null;
  private candidates: Map<string, DecisionCandidate> = new Map();
  private revalidated: Map<string, RevalidatedExecution> = new Map();

  constructor(state: ControlState, nodeId: string, host: ControlHost, provider: ControlProvider) {
    requireControl(state !== null && state.format === 'amita-phone-control-state/1' && state.node_id === nodeId &&
      Array.isArray(state.records) && state.records.length <= 128, 'phone_control_private_state_invalid');
    this.state = state; this.host = host; this.provider = provider; this.sessionId = host.id();
    requireControl(identifier(this.sessionId), 'phone_control_session_invalid');
    const ids: string[] = [];
    for (let i: number = 0; i < state.records.length; i++) {
      const row: ControlRecord = state.records[i];
      const request: ControlRequest = recordedRequest(row.request_json);
      requireControl(canonicalRequest(request) === row.request_json && row.operation_id === request.operation_id &&
        row.task_id === request.task_id && !ids.includes(row.operation_id) && identifier(row.approval_id) &&
        identifier(row.nonce) && identifier(row.lease_id) && Number.isSafeInteger(row.revision) && row.revision > 0 &&
        Number.isSafeInteger(row.expires_at) && ['awaiting_approval', 'approved', 'denied', 'cancelled',
          'attempted', 'input_acknowledged', 'unknown'].includes(row.state), 'phone_control_private_state_invalid');
      ids.push(row.operation_id);
      if (row.state === 'attempted') { row.state = 'unknown'; }
      // A new process has no current observed object, lease, or owner grant.
      if (row.state === 'approved' || row.state === 'awaiting_approval') { row.state = 'cancelled'; }
    }
  }
  private async persist(): Promise<void> {
    await this.host.save(JSON.parse(JSON.stringify(this.state)) as ControlState);
  }
  async initialize(): Promise<void> { await this.persist(); this.ready = true; }
  availability(): ControlAvailability { return this.provider.availability(); }
  resource(): string { return 'screen:' + this.state.node_id + ':display-0'; }
  // Called only by the trusted local owner UI, not the remote tool transport.
  setOwnerEnabled(value: boolean): void {
    this.enabled = value;
    if (!value) { this.candidates.clear(); this.ownerActivity(); }
  }
  // Native human input, system lock, focus/window change and provider disconnect
  // all invalidate refs and pending approvals. Never swallow the owner's input.
  ownerActivity(): void {
    this.humanEpoch++; this.revision++; this.view = null; this.observed = null; this.lease = null;
    this.revalidated.clear();
    if (this.fence !== null) { this.fence.valid = false; }
  }
  acquire(taskId: string, durationMs: number): ControlLease {
    requireControl(this.ready && this.enabled && identifier(taskId) && Number.isSafeInteger(durationMs) &&
      durationMs > 0 && durationMs <= 30000, 'phone_control_owner_or_lease_required');
    requireControl(this.lease === null || this.lease.expires_at <= this.host.now(), 'phone_control_screen_busy');
    const lease: ControlLease = { task_id: taskId, lease_id: this.host.id(), expires_at: this.host.now() + durationMs };
    requireControl(identifier(lease.lease_id), 'phone_control_lease_invalid');
    this.lease = lease;
    return { task_id: lease.task_id, lease_id: lease.lease_id, expires_at: lease.expires_at };
  }
  release(taskId: string, leaseId: string): void {
    requireControl(this.lease !== null && this.lease.task_id === taskId && this.lease.lease_id === leaseId,
      'phone_control_lease_mismatch');
    this.ownerActivity();
  }
  private currentLease(taskId: string, leaseId: string): ControlLease {
    const lease: ControlLease | null = this.lease;
    requireControl(this.ready && this.enabled && lease !== null && lease.task_id === taskId &&
      lease.lease_id === leaseId && lease.expires_at > this.host.now(), 'phone_control_lease_expired_or_revoked');
    return lease as ControlLease;
  }
  async inspect(maxElements: number = 64): Promise<ControlView> {
    requireControl(this.ready && this.enabled && !this.busy && this.provider.availability().inspect &&
      Number.isSafeInteger(maxElements) && maxElements > 0 && maxElements <= 128,
      'phone_control_inspect_unavailable');
    this.busy = true;
    const epoch: number = this.humanEpoch;
    try {
      const value: ProviderView = await this.provider.inspect(maxElements);
      requireControl(epoch === this.humanEpoch && this.enabled && identifier(value.bundle_name) &&
        identifier(value.window_id) && identifier(value.view_token) && Number.isSafeInteger(value.width) &&
        value.width > 0 && value.width <= 16384 && Number.isSafeInteger(value.height) &&
        value.height > 0 && value.height <= 16384 && typeof value.truncated === 'boolean' &&
        Array.isArray(value.elements) && value.elements.length <= maxElements, 'phone_control_view_unavailable');
      const elements: ControlElement[] = [];
      const keys: string[] = [];
      this.revision++;
      for (let i: number = 0; i < value.elements.length; i++) {
        const row: ProviderElement = value.elements[i];
        const box: ControlBounds = row.bounds;
        requireControl(identifier(row.key) && !keys.includes(row.key) && typeof row.role === 'string' &&
          row.role.length <= 80 && typeof row.label === 'string' && row.label.length <= 1024 &&
          typeof row.sensitive === 'boolean' && typeof row.enabled === 'boolean' && Array.isArray(row.actions) &&
          row.actions.length <= 4 && row.actions.every((action: string): boolean =>
            ['tap', 'set_text', 'scroll_forward', 'scroll_backward'].includes(action)) &&
          Number.isSafeInteger(box.left) && Number.isSafeInteger(box.top) && Number.isSafeInteger(box.width) &&
          Number.isSafeInteger(box.height) && box.left >= 0 && box.top >= 0 && box.width > 0 && box.height > 0 &&
          box.left + box.width <= value.width && box.top + box.height <= value.height, 'phone_control_element_invalid');
        keys.push(row.key);
        requireControl(row.state_fingerprint === undefined || /^[a-f0-9]{64}$/.test(row.state_fingerprint),
          'phone_control_element_fingerprint_invalid');
        elements.push({ element_ref: this.sessionId + ':' + this.revision + ':' + i, role: row.role,
          label: row.sensitive ? '[private]' : row.label.slice(0, 160), sensitive: row.sensitive,
          enabled: row.enabled, actions: row.actions.slice(), bounds: { left: box.left, top: box.top, width: box.width, height: box.height },
          state_fingerprint: row.state_fingerprint });
      }
      const observed: ControlView = { revision: this.revision, bundle_name: value.bundle_name,
        window_id: value.window_id, view_ref: this.sessionId + ':' + this.revision,
        width: value.width, height: value.height, truncated: value.truncated, elements: elements };
      this.view = value; this.observed = observed;
      return JSON.parse(JSON.stringify(observed)) as ControlView;
    } finally { this.busy = false; }
  }
  private target(ref: string): ProviderElement {
    requireControl(this.view !== null && this.observed !== null, 'phone_control_fresh_observation_required');
    const index: number = (this.observed as ControlView).elements.findIndex((row: ControlElement): boolean => row.element_ref === ref);
    requireControl(index >= 0, 'phone_control_stale_element_ref');
    return (this.view as ProviderView).elements[index];
  }
  async request(request: ControlRequest, leaseId: string): Promise<ApprovalTicket> {
    requireControl(!this.busy, 'phone_control_busy');
    const encoded: string = canonicalRequest(request);
    const lease: ControlLease = this.currentLease(request.task_id, leaseId);
    const available: ControlAvailability = this.provider.availability();
    requireControl(available.act && available.human_input_observable, 'phone_control_provider_unavailable');
    requireControl(!this.state.records.some((row: ControlRecord): boolean => row.operation_id === request.operation_id),
      'phone_control_original_operation_observe_only');
    const target: ProviderElement = this.target(request.element_ref);
    requireControl(target.enabled && !target.sensitive && target.actions.includes(request.action), 'phone_control_target_not_authorised');
    requireControl(this.state.records.length < 128, 'phone_control_journal_full');
    this.busy = true;
    try {
      const record: ControlRecord = { operation_id: request.operation_id, task_id: request.task_id, request_json: encoded,
        revision: this.revision, approval_id: this.host.id(), nonce: this.host.id(), lease_id: leaseId,
        expires_at: Math.min(lease.expires_at, this.host.now() + 30000), state: 'awaiting_approval' };
      requireControl(identifier(record.approval_id) && identifier(record.nonce), 'phone_control_approval_invalid');
      this.state.records.push(record);
      const originalView: ProviderView = this.view as ProviderView;
      if (!originalView.truncated && originalView.elements.every((element: ProviderElement): boolean =>
        element.state_fingerprint !== undefined && /^[a-f0-9]{64}$/.test(element.state_fingerprint))) {
        this.candidates.set(record.approval_id, { request_json: encoded,
          fingerprint: viewFingerprint(originalView), target_key: target.key });
      }
      const summary: string = request.action + ' · ' + (this.view as ProviderView).bundle_name + ' · ' +
        target.role + ' · ' + target.label.slice(0, 80);
      await this.persist();
      if (record.revision !== this.revision) {
        record.state = 'cancelled'; await this.persist();
        throw new Error('phone_control_owner_interrupted');
      }
      return { approval_id: record.approval_id, nonce: record.nonce, operation_id: record.operation_id,
        task_id: record.task_id, revision: record.revision, expires_at: record.expires_at,
        summary: summary, review_required: request.action === 'set_text' };
    } finally { this.busy = false; }
  }
  async decide(approvalId: string, nonce: string, decision: string): Promise<void> {
    requireControl(!this.busy && (decision === 'approve' || decision === 'deny'), 'phone_control_decision_unavailable');
    const row: ControlRecord | undefined = this.state.records.find((value: ControlRecord): boolean => value.approval_id === approvalId);
    requireControl(row !== undefined && row.nonce === nonce && row.state === 'awaiting_approval', 'phone_control_approval_mismatch');
    const record: ControlRecord = row as ControlRecord;
    if (decision === 'approve') {
      this.currentLease(record.task_id, record.lease_id);
      requireControl(record.expires_at > this.host.now() && record.revision === this.revision && this.view !== null,
        'phone_control_approval_expired_or_stale');
    }
    this.busy = true;
    try {
      record.state = decision === 'approve' ? 'approved' : 'denied'; await this.persist();
      this.candidates.delete(record.approval_id);
    }
    finally { this.busy = false; }
  }
  async decideBound(value: OwnerControlDecision): Promise<void> {
    requireControl(value !== null && typeof value === 'object' && !Array.isArray(value) &&
      Object.keys(value).sort().join('|') === ['approval_id', 'nonce', 'operation_id', 'task_id', 'revision',
        'expires_at', 'decision'].sort().join('|'), 'phone_control_approval_mismatch');
    const row: ControlRecord | undefined = this.state.records.find((record: ControlRecord): boolean =>
      record.approval_id === value.approval_id);
    requireControl(row !== undefined && row.operation_id === value.operation_id && row.task_id === value.task_id &&
      row.revision === value.revision && row.expires_at === value.expires_at, 'phone_control_approval_mismatch');
    await this.decide(value.approval_id, value.nonce, value.decision);
  }
  // Only a trusted explicit owner callback may enter this separate path. Old
  // decideBound remains strict. A candidate is memory-only evidence, never a
  // grant; every ordinary input still revokes all current refs/leases/fences.
  async decideAfterRevalidation(value: OwnerControlDecision, sourcePrepared: () => boolean): Promise<void> {
    requireControl(value !== null && typeof value === 'object' && !Array.isArray(value) &&
      Object.keys(value).sort().join('|') === ['approval_id', 'nonce', 'operation_id', 'task_id', 'revision',
        'expires_at', 'decision'].sort().join('|'), 'phone_control_approval_mismatch');
    if (value.decision === 'deny') { await this.decideBound(value); return; }
    requireControl(!this.busy && this.ready && this.enabled && value.decision === 'approve' && sourcePrepared(),
      'phone_control_explicit_revalidation_unavailable');
    const row: ControlRecord | undefined = this.state.records.find((record: ControlRecord): boolean =>
      record.approval_id === value.approval_id);
    const candidate: DecisionCandidate | undefined = this.candidates.get(value.approval_id);
    requireControl(row !== undefined && candidate !== undefined && row.state === 'awaiting_approval' &&
      row.operation_id === value.operation_id && row.task_id === value.task_id && row.nonce === value.nonce &&
      row.revision === value.revision && row.expires_at === value.expires_at && row.expires_at > this.host.now() &&
      row.request_json === candidate.request_json, 'phone_control_original_pending_candidate_required');
    const record: ControlRecord = row as ControlRecord;
    const original: DecisionCandidate = candidate as DecisionCandidate;
    const epoch: number = this.humanEpoch;
    await this.inspect(128); // Actual root and full bounded tree; validates all elements again.
    requireControl(!this.busy, 'phone_control_busy');
    this.busy = true;
    try {
      const current: ProviderView = this.view as ProviderView;
      requireControl(this.enabled && this.view !== null && epoch === this.humanEpoch && sourcePrepared() &&
        record.state === 'awaiting_approval' && record.expires_at > this.host.now() && !current.truncated &&
        viewFingerprint(current) === original.fingerprint &&
        current.elements.some((element: ProviderElement): boolean => element.key === original.target_key) &&
        this.provider.availability().act && this.provider.availability().human_input_observable,
        'phone_control_original_view_revalidation_failed');
      requireControl(this.lease === null || this.lease.expires_at <= this.host.now() || this.lease.task_id === record.task_id,
        'phone_control_screen_busy');
      const leaseId: string = this.host.id();
      requireControl(identifier(leaseId), 'phone_control_lease_invalid');
      const proof: RevalidatedExecution = { approval_id: record.approval_id, request_json: record.request_json,
        revision: this.revision, human_epoch: this.humanEpoch, lease_id: leaseId,
        target_key: original.target_key, fingerprint: original.fingerprint };
      this.lease = { task_id: record.task_id, lease_id: leaseId, expires_at: record.expires_at };
      this.revalidated.set(record.operation_id, proof);
      record.state = 'approved';
      await this.persist();
      if (!this.enabled || this.revalidated.get(record.operation_id) !== proof || epoch !== this.humanEpoch ||
        proof.revision !== this.revision || record.expires_at <= this.host.now() || !sourcePrepared()) {
        this.revalidated.delete(record.operation_id); record.state = 'cancelled'; await this.persist();
        throw new Error('phone_control_revalidated_grant_interrupted');
      }
      this.candidates.delete(record.approval_id);
    } catch (error) {
      this.revalidated.delete(record.operation_id);
      throw new Error('phone_control_original_view_revalidation_failed');
    } finally { this.busy = false; }
  }
  receipt(operationId: string): ControlReceipt {
    const row: ControlRecord | undefined = this.state.records.find((value: ControlRecord): boolean => value.operation_id === operationId);
    requireControl(row !== undefined, 'phone_control_original_operation_not_found');
    const record: ControlRecord = row as ControlRecord;
    return { operation_id: record.operation_id, state: record.state, input_acknowledged: record.state === 'input_acknowledged',
      effect_verified: false, automatic_replay: false };
  }
  approvalCurrent(operationId: string): boolean {
    const record: ControlRecord | undefined = this.state.records.find((row: ControlRecord): boolean => row.operation_id === operationId);
    if (!this.ready || !this.enabled || record === undefined || record.state !== 'approved' || this.view === null ||
      record.expires_at <= this.host.now()) { return false; }
    const proof: RevalidatedExecution | undefined = this.revalidated.get(operationId);
    try { this.currentLease(record.task_id, proof === undefined ? record.lease_id : proof.lease_id); }
    catch (error) { return false; }
    return proof === undefined ? record.revision === this.revision :
      proof.approval_id === record.approval_id && proof.request_json === record.request_json &&
      proof.revision === this.revision && proof.human_epoch === this.humanEpoch &&
      viewFingerprint(this.view) === proof.fingerprint;
  }
  async execute(operationId: string): Promise<ControlReceipt> {
    requireControl(!this.busy, 'phone_control_busy');
    const row: ControlRecord | undefined = this.state.records.find((value: ControlRecord): boolean => value.operation_id === operationId);
    requireControl(row !== undefined && row.state === 'approved', 'phone_control_original_operation_not_approved');
    const record: ControlRecord = row as ControlRecord;
    const proof: RevalidatedExecution | undefined = this.revalidated.get(operationId);
    this.currentLease(record.task_id, proof === undefined ? record.lease_id : proof.lease_id);
    const currentRevision: boolean = proof === undefined ? record.revision === this.revision :
      proof.approval_id === record.approval_id && proof.request_json === record.request_json &&
      proof.revision === this.revision && proof.human_epoch === this.humanEpoch && this.view !== null &&
      viewFingerprint(this.view as ProviderView) === proof.fingerprint;
    requireControl(currentRevision && record.expires_at > this.host.now() && this.view !== null,
      'phone_control_approval_expired_or_stale');
    const request: ControlRequest = recordedRequest(record.request_json);
    const view: ProviderView = this.view as ProviderView;
    const renewedTarget: ProviderElement | undefined = proof === undefined ? undefined :
      view.elements.find((element: ProviderElement): boolean => element.key === proof.target_key);
    requireControl(proof === undefined || renewedTarget !== undefined, 'phone_control_revalidated_target_missing');
    const target: ProviderElement = proof === undefined ? this.target(request.element_ref) : renewedTarget as ProviderElement;
    const available: ControlAvailability = this.provider.availability();
    requireControl(available.act && available.human_input_observable && target.enabled && !target.sensitive &&
      target.actions.includes(request.action), 'phone_control_provider_unavailable');
    this.busy = true;
    const fence: ControlFence = { valid: true, human_input_epoch: this.humanEpoch };
    this.fence = fence;
    try {
      record.state = 'attempted';
      await this.persist(); // Admission is impossible until the original intent is durable.
      if (!fence.valid || this.host.now() >= record.expires_at) {
        record.state = 'cancelled'; await this.persist();
        throw new Error('phone_control_owner_interrupted');
      }
      try {
        const accepted: boolean = await this.provider.act({ operation_id: record.operation_id,
          bundle_name: view.bundle_name, window_id: view.window_id, view_token: view.view_token,
          element_key: target.key, action: request.action, text: request.text, fence: fence });
        record.state = accepted && fence.valid ? 'input_acknowledged' : 'unknown';
      } catch (error) { record.state = 'unknown'; }
      await this.persist();
      return this.receipt(operationId);
    } finally {
      fence.valid = false; this.fence = null; this.busy = false;
      this.revalidated.delete(operationId); this.candidates.delete(record.approval_id);
      this.view = null; this.observed = null; this.revision++;
    }
  }
}
