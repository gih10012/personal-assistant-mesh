// Foreground provider transport over the existing session_nodes / Allocations.
// This is a private execution journal, never a replacement task ledger.
// No platform imports: the shipped state machine runs in the Node TS harness.
import type { Blob } from './Protocol';
import { MAX_BYTES, requireValue, validateJoinUrl } from './Protocol';

export interface WorkConfig {
  format: string; resource_url: string; node_id: string; session_id: string;
  mesh_id: string; authority_id: string;
}
export interface Workload { input?: Blob; repeat?: number; max_bytes?: number; }
export interface WorkSelection {
  capability_id: string; epoch: number; action: string; scope: Object;
  workload: Workload; observations?: Object[];
}
export interface ProviderSettlement { outcome: string; evidence: SettlementEvidence; }
export interface ProviderDispatch {
  operation_id: string; capability_id: string; provider: string; state: string;
  epoch: number; receipt_id: string | null; selection?: WorkSelection;
  task_id?: string; task_epoch?: number; deadline?: number;
  settlement?: ProviderSettlement | null; execute_once?: boolean; accepted_new?: boolean;
}
export interface ProviderAllocation {
  operation_id: string; task_id: string; task_epoch: number;
  plan: AllocationPlan; dispatch: ProviderDispatch[];
}
export interface AllocationPlan { selections: WorkSelection[]; }
export interface ResultReceipt { result_reference: string; sha256: string; bytes: number; }
export interface SettlementEvidence {
  resource_quiescent: boolean; result_reference: string; sha256: string; bytes: number;
  adapter_handle: string; invocation_nonce: string;
}
export interface SessionNetwork { state: string; expires_at: number; presence_ttl_seconds: number; }
export interface SessionView { node_id: string; session_id: string; network: SessionNetwork; }
export interface PhoneResponse {
  format: string; mesh_id: string; session?: SessionView;
  dispatch?: ProviderDispatch[]; provider?: Object;
  execution_verified?: boolean; native_tools_restricted?: boolean;
}
export interface ProviderArguments {
  operation_id: string; capability_id: string; epoch?: number; receipt_id?: string;
  result?: Blob; outcome?: string; evidence?: SettlementEvidence;
}
export interface ProviderCall { action: string; arguments: ProviderArguments; }
export interface ReportArguments { session_id: string; report_id: string; provider: ProviderCall; }
export interface WorkRecord {
  operation_id: string; capability_id: string; task_id: string; task_epoch: number;
  dispatch_json: string; receipt_id: string; state: string; authority_epoch: number;
  invocation_nonce: string; result_json: string; result_reference: string;
  last_remote_state: string; error: string;
  permission_context: string;
}
export interface ControlIntent { action: string; control_id: string; online: boolean; capabilities: string[]; }
export interface WorkSnapshot {
  format: string; node_id: string; config: WorkConfig | null;
  pending_control: ControlIntent | null; pending_pull: string; records: WorkRecord[];
}
export interface WorkHost {
  save(snapshot: WorkSnapshot): Promise<void>;
  exchange(action: string, args: Object): Promise<PhoneResponse>;
  execute(dispatch: ProviderDispatch): Promise<Blob>;
  allowed(dispatch: ProviderDispatch, permissionContext?: string): boolean;
  permission(dispatch: ProviderDispatch): string;
  id(): string;
  now(): number;
}

function name(value: string): void {
  requireValue(typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,255}$/.test(value),
    'mesh_work_identity_invalid');
}
function epoch(value: number): void {
  requireValue(Number.isSafeInteger(value) && value >= 1, 'mesh_work_epoch_invalid');
}
export function validateWorkConfig(value: WorkConfig, nodeId: string): void {
  requireValue(value !== null && value.format === 'mesh-phone-client/1' && value.node_id === nodeId,
    'mesh_work_original_node_required');
  requireValue(Object.keys(value).sort().join('|') === 'authority_id|format|mesh_id|node_id|resource_url|session_id',
    'mesh_work_public_configuration_only');
  name(value.node_id); name(value.session_id); name(value.mesh_id); name(value.authority_id);
  requireValue(typeof value.resource_url === 'string' && value.resource_url.endsWith('/phone/node'),
    'mesh_work_https_required');
  validateJoinUrl(value.resource_url.slice(0, -'/phone/node'.length));
}
export function freshWorkSnapshot(nodeId: string): WorkSnapshot {
  name(nodeId);
  return { format: 'mesh-phone-journal/1', node_id: nodeId, config: null,
    pending_control: null, pending_pull: '', records: [] };
}
export function coldResumeWork(snapshot: WorkSnapshot, nodeId: string): void {
  requireValue(snapshot.format === 'mesh-phone-journal/1' && snapshot.node_id === nodeId &&
    Array.isArray(snapshot.records) && snapshot.records.length <= 256, 'mesh_work_private_state_invalid');
  if (snapshot.config !== null) { validateWorkConfig(snapshot.config, nodeId); }
  const identities: string[] = [];
  for (let i: number = 0; i < snapshot.records.length; i++) {
    const row: WorkRecord = snapshot.records[i];
    const dispatch: ProviderDispatch = JSON.parse(row.dispatch_json) as ProviderDispatch;
    validateWorkDispatch(dispatch, nodeId, false);
    requireValue(row.operation_id === dispatch.operation_id && row.capability_id === dispatch.capability_id &&
      row.task_id === dispatch.task_id && row.task_epoch === dispatch.task_epoch,
      'mesh_work_original_binding_changed');
    name(row.receipt_id);
    requireValue(typeof row.permission_context === 'string', 'mesh_work_permission_context_required');
    const identity: string = row.operation_id + '|' + row.capability_id;
    requireValue(!identities.includes(identity), 'mesh_work_duplicate_journal'); identities.push(identity);
    if (row.state !== 'settled' && row.result_json.length === 0) {
      row.state = 'unknown'; row.error = 'mesh_work_restart_observe_original_only';
    }
    if (row.result_json.length > 0) { validateWorkBlob(JSON.parse(row.result_json) as Blob); }
  }
}
export function validateWorkBlob(value: Blob): void {
  requireValue(value !== null && Number.isSafeInteger(value.bytes) && value.bytes >= 0 && value.bytes <= MAX_BYTES &&
    typeof value.sha256 === 'string' && /^[0-9a-f]{64}$/.test(value.sha256) &&
    typeof value.base64 === 'string' && value.base64.length <= 87384, 'mesh_work_blob_invalid');
}
export function validateWorkDispatch(value: ProviderDispatch, nodeId: string, pending: boolean = true): void {
  requireValue(value !== null && typeof value === 'object', 'mesh_work_dispatch_invalid');
  name(value.operation_id); name(value.capability_id); name(value.provider);
  name(value.task_id as string); epoch(value.task_epoch as number); epoch(value.epoch);
  requireValue(value.provider === nodeId && Number.isFinite(value.deadline), 'mesh_work_provider_mismatch');
  if (pending) { requireValue(value.state === 'pending' && value.receipt_id === null, 'mesh_work_not_pending'); }
  const selected: WorkSelection = value.selection as WorkSelection;
  requireValue(selected !== null && selected !== undefined && selected.capability_id === value.capability_id,
    'mesh_work_selection_mismatch');
  epoch(selected.epoch);
  const suffix: string = selected.action === 'challenge.bytes.repeat' ? ':bytes-v1' : ':selected-file-v1';
  requireValue((selected.action === 'challenge.bytes.repeat' || selected.action === 'file.read.selected') &&
    value.capability_id === nodeId + suffix && selected.epoch === 1, 'mesh_work_adapter_contract_mismatch');
  requireValue(selected.scope !== null && selected.scope !== undefined && selected.workload !== null &&
    typeof selected.workload === 'object', 'mesh_work_scope_required');
  if (selected.action === 'challenge.bytes.repeat') {
    validateWorkBlob(selected.workload.input as Blob);
    const repeat: number = selected.workload.repeat as number;
    requireValue(Number.isSafeInteger(repeat) && repeat >= 1 && repeat <= 64 &&
      (selected.workload.input as Blob).bytes * repeat <= MAX_BYTES, 'mesh_work_recipe_budget_invalid');
    requireValue(Object.keys(selected.workload).sort().join('|') === 'input|repeat', 'mesh_work_recipe_shape_invalid');
  } else {
    requireValue(Object.keys(selected.workload).join('|') === 'max_bytes' &&
      Number.isSafeInteger(selected.workload.max_bytes) && (selected.workload.max_bytes as number) >= 0 &&
      (selected.workload.max_bytes as number) <= MAX_BYTES, 'mesh_work_selected_file_budget_invalid');
  }
}
function sameSelection(left: WorkSelection, right: WorkSelection): boolean {
  // The original JSON is preserved, so no text/recipe can change between pending and admission.
  return left.capability_id === right.capability_id && left.epoch === right.epoch && left.action === right.action &&
    JSON.stringify(left.scope) === JSON.stringify(right.scope) && JSON.stringify(left.workload) === JSON.stringify(right.workload);
}

export class WorkEngine {
  snapshot: WorkSnapshot;
  private host: WorkHost;
  constructor(snapshot: WorkSnapshot, host: WorkHost) { this.snapshot = snapshot; this.host = host; }
  private async save(): Promise<void> { await this.host.save(this.snapshot); }
  private config(): WorkConfig {
    requireValue(this.snapshot.config !== null, 'mesh_work_configuration_required');
    return this.snapshot.config as WorkConfig;
  }
  private validateResponse(value: PhoneResponse): void {
    requireValue(value !== null && value.format === 'mesh-interactive-node/1' &&
      value.mesh_id === this.config().mesh_id && value.execution_verified !== true &&
      value.native_tools_restricted !== true, 'mesh_work_authority_response_invalid');
    if (value.session !== undefined) {
      requireValue(value.session.session_id === this.config().session_id &&
        value.session.node_id === this.snapshot.node_id, 'mesh_work_session_mismatch');
    }
  }
  async control(online: boolean, capabilities: string[]): Promise<PhoneResponse> {
    if (this.snapshot.pending_control === null) {
      this.snapshot.pending_control = { action: 'mesh_node_update', control_id: this.host.id(),
        online: online, capabilities: capabilities.slice() };
      await this.save();
    }
    const intent: ControlIntent = this.snapshot.pending_control as ControlIntent;
    const result: PhoneResponse = await this.host.exchange(intent.action, { session_id: this.config().session_id,
      control_id: intent.control_id, online: intent.online, capabilities: intent.capabilities,
      agent_state: intent.capabilities.length > 0 ? 'ready' : 'idle', model_state: 'unavailable' });
    this.validateResponse(result); this.snapshot.pending_control = null; await this.save();
    // A failed old heartbeat must never swallow a subsequent permission withdrawal.
    if (intent.online !== online || intent.capabilities.join('|') !== capabilities.join('|')) {
      return this.control(online, capabilities);
    }
    return result;
  }
  async connect(capabilities: string[]): Promise<PhoneResponse> {
    if (this.snapshot.pending_control === null) {
      this.snapshot.pending_control = { action: 'mesh_connect', control_id: this.host.id(),
        online: true, capabilities: capabilities.slice() };
      await this.save();
    }
    return this.control(true, capabilities);
  }
  async discover(): Promise<PhoneResponse> {
    const result: PhoneResponse = await this.host.exchange('mesh_discover', { session_id: this.config().session_id });
    this.validateResponse(result); return result;
  }
  async pull(): Promise<ProviderDispatch[]> {
    if (this.snapshot.pending_pull.length === 0) { this.snapshot.pending_pull = this.host.id(); await this.save(); }
    const result: PhoneResponse = await this.host.exchange('mesh_work_pull', {
      session_id: this.config().session_id, pull_id: this.snapshot.pending_pull });
    this.validateResponse(result);
    requireValue(Array.isArray(result.dispatch) && (result.dispatch as ProviderDispatch[]).length <= 100,
      'mesh_work_dispatch_response_invalid');
    const dispatch: ProviderDispatch[] = result.dispatch as ProviderDispatch[];
    // A failed response stays bound to its old pull id until successfully observed.
    this.snapshot.pending_pull = ''; await this.save(); return dispatch;
  }
  private async provider(row: WorkRecord, action: string, args: ProviderArguments): Promise<Object> {
    const read: boolean = action === 'inspect';
    const request: ReportArguments = { session_id: this.config().session_id,
      report_id: read ? this.host.id() : row.receipt_id + ':' + action,
      provider: { action: action, arguments: args } };
    const response: PhoneResponse = await this.host.exchange('mesh_work_report', request);
    this.validateResponse(response);
    requireValue(response.provider !== null && response.provider !== undefined, 'mesh_work_provider_response_invalid');
    return response.provider as Object;
  }
  private response(row: WorkRecord, value: ProviderDispatch, state: string): void {
    requireValue(value.operation_id === row.operation_id && value.capability_id === row.capability_id &&
      value.provider === this.snapshot.node_id && value.receipt_id === row.receipt_id && value.state === state,
      'mesh_work_provider_response_mismatch'); epoch(value.epoch);
  }
  private async inspect(row: WorkRecord): Promise<ProviderDispatch> {
    const allocation: ProviderAllocation = await this.provider(row, 'inspect', {
      operation_id: row.operation_id, capability_id: row.capability_id }) as ProviderAllocation;
    requireValue(allocation.operation_id === row.operation_id && allocation.task_id === row.task_id &&
      allocation.task_epoch === row.task_epoch && Array.isArray(allocation.dispatch) &&
      allocation.plan !== null && Array.isArray(allocation.plan.selections), 'mesh_work_allocation_binding_changed');
    const original: ProviderDispatch = JSON.parse(row.dispatch_json) as ProviderDispatch;
    const selections: WorkSelection[] = allocation.plan.selections.filter((item: WorkSelection): boolean =>
      item.capability_id === row.capability_id);
    const matches: ProviderDispatch[] = allocation.dispatch.filter((item: ProviderDispatch): boolean =>
      item.capability_id === row.capability_id);
    requireValue(selections.length === 1 && matches.length === 1 &&
      sameSelection(selections[0], original.selection as WorkSelection), 'mesh_work_allocation_content_changed');
    const dispatch: ProviderDispatch = matches[0];
    requireValue(dispatch.operation_id === row.operation_id && dispatch.provider === this.snapshot.node_id &&
      (dispatch.receipt_id === null || dispatch.receipt_id === row.receipt_id), 'mesh_work_original_receipt_changed');
    epoch(dispatch.epoch); row.last_remote_state = dispatch.state; await this.save(); return dispatch;
  }
  async run(dispatch: ProviderDispatch): Promise<WorkRecord> {
    validateWorkDispatch(dispatch, this.snapshot.node_id);
    for (let i: number = 0; i < this.snapshot.records.length; i++) {
      const old: WorkRecord = this.snapshot.records[i];
      if (old.operation_id === dispatch.operation_id && old.capability_id === dispatch.capability_id) {
        requireValue(old.dispatch_json === JSON.stringify(dispatch), 'mesh_work_original_dispatch_changed');
        return this.reconcile(old); // No restart or re-delivery can invoke the native adapter.
      }
    }
    requireValue(this.host.allowed(dispatch) && (dispatch.deadline as number) > this.host.now(), 'mesh_work_permission_unavailable');
    requireValue(this.snapshot.records.length < 256, 'mesh_work_journal_capacity_reached');
    const row: WorkRecord = { operation_id: dispatch.operation_id, capability_id: dispatch.capability_id,
      task_id: dispatch.task_id as string, task_epoch: dispatch.task_epoch as number, dispatch_json: JSON.stringify(dispatch),
      receipt_id: 'phone-' + this.host.id(), state: 'prepared', authority_epoch: dispatch.epoch,
      invocation_nonce: '', result_json: '', result_reference: '', last_remote_state: 'pending', error: '',
      permission_context: this.host.permission(dispatch) };
    this.snapshot.records.push(row); await this.save();
    try {
      const observed: ProviderDispatch = await this.inspect(row);
      requireValue(observed.state === 'pending' && observed.epoch === dispatch.epoch && observed.receipt_id === null,
        'mesh_work_original_not_pending');
      requireValue(this.host.allowed(dispatch, row.permission_context) && (dispatch.deadline as number) > this.host.now(),
        'mesh_work_permission_revoked');
      row.state = 'accept_intent'; await this.save();
      const accepted: ProviderDispatch = await this.provider(row, 'accept', {
        operation_id: row.operation_id, capability_id: row.capability_id,
        epoch: row.authority_epoch, receipt_id: row.receipt_id }) as ProviderDispatch;
      this.response(row, accepted, 'accepted');
      requireValue(accepted.epoch === row.authority_epoch + 1, 'mesh_work_accept_epoch_invalid');
      row.authority_epoch = accepted.epoch; row.state = 'accepted'; await this.save();
      requireValue(this.host.allowed(dispatch, row.permission_context) && (dispatch.deadline as number) > this.host.now(),
        'mesh_work_permission_revoked');
      row.state = 'start_intent'; await this.save();
      const started: ProviderDispatch = await this.provider(row, 'start', {
        operation_id: row.operation_id, capability_id: row.capability_id,
        epoch: row.authority_epoch, receipt_id: row.receipt_id }) as ProviderDispatch;
      this.response(row, started, 'running');
      requireValue(started.execute_once === true && started.epoch === row.authority_epoch + 1,
        'mesh_work_start_not_new');
      row.authority_epoch = started.epoch; row.invocation_nonce = this.host.id();
      row.state = 'attempted'; await this.save(); // Durable BEFORE reading even the selected document.
      requireValue(this.host.allowed(dispatch, row.permission_context) && (dispatch.deadline as number) > this.host.now(),
        'mesh_work_permission_revoked');
      const result: Blob = await this.host.execute(dispatch); validateWorkBlob(result);
      row.result_json = JSON.stringify(result); row.state = 'result_ready'; await this.save();
      return await this.reportResult(row);
    } catch (error) {
      row.state = row.result_json.length > 0 ? 'result_pending' : 'unknown';
      row.error = 'mesh_work_original_outcome_unconfirmed'; await this.save(); return row;
    }
  }
  private async reportResult(row: WorkRecord): Promise<WorkRecord> {
    const dispatch: ProviderDispatch = JSON.parse(row.dispatch_json) as ProviderDispatch;
    // File-sharing revocation also prevents sending a locally buffered private document.
    requireValue(this.host.allowed(dispatch, row.permission_context), 'mesh_work_result_permission_required');
    const result: Blob = JSON.parse(row.result_json) as Blob; validateWorkBlob(result);
    row.state = 'result_pending'; await this.save();
    const receipt: ResultReceipt = await this.provider(row, 'result', { operation_id: row.operation_id,
      capability_id: row.capability_id, receipt_id: row.receipt_id, result: result }) as ResultReceipt;
    requireValue(receipt.sha256 === result.sha256 && receipt.bytes === result.bytes &&
      receipt.result_reference === 'phone-result:' + result.sha256, 'mesh_work_complete_result_unconfirmed');
    row.result_reference = receipt.result_reference; row.state = 'settlement_pending'; await this.save();
    const evidence: SettlementEvidence = { resource_quiescent: true, result_reference: row.result_reference,
      sha256: result.sha256, bytes: result.bytes, adapter_handle: 'harmony-native-v1', invocation_nonce: row.invocation_nonce };
    const settled: ProviderDispatch = await this.provider(row, 'settle', { operation_id: row.operation_id,
      capability_id: row.capability_id, epoch: row.authority_epoch, receipt_id: row.receipt_id,
      outcome: 'completed', evidence: evidence }) as ProviderDispatch;
    this.response(row, settled, 'completed');
    requireValue(settled.settlement !== null && settled.settlement !== undefined &&
      settled.settlement.outcome === 'completed' && settled.settlement.evidence.result_reference === row.result_reference,
      'mesh_work_settlement_unconfirmed');
    row.authority_epoch = settled.epoch; row.state = 'settled'; row.error = '';
    row.last_remote_state = 'completed'; await this.save(); return row;
  }
  async reconcile(row: WorkRecord): Promise<WorkRecord> {
    if (row.state === 'settled') { return row; }
    try {
      const observed: ProviderDispatch = await this.inspect(row);
      if (observed.state === 'completed' && row.result_json.length > 0) {
        const result: Blob = JSON.parse(row.result_json) as Blob;
        const evidence: SettlementEvidence | undefined = observed.settlement?.evidence;
        requireValue(observed.receipt_id === row.receipt_id && observed.settlement?.outcome === 'completed' &&
          evidence !== undefined && evidence.result_reference === 'phone-result:' + result.sha256 &&
          evidence.sha256 === result.sha256 && evidence.bytes === result.bytes &&
          evidence.invocation_nonce === row.invocation_nonce && evidence.resource_quiescent === true,
          'mesh_work_original_settlement_changed');
        row.authority_epoch = observed.epoch; row.result_reference = (evidence as SettlementEvidence).result_reference;
        row.state = 'settled'; row.error = ''; await this.save(); return row;
      }
      if (row.result_json.length > 0) {
        requireValue(observed.receipt_id === row.receipt_id, 'mesh_work_original_receipt_required');
        // Existing settlement has fixed bytes/IDs; report is idempotent, never another invocation.
        row.authority_epoch = observed.epoch; await this.save(); return await this.reportResult(row);
      }
      row.state = 'unknown';
      if ((observed.state === 'accepted' || observed.state === 'running') && observed.receipt_id === row.receipt_id) {
        row.authority_epoch = observed.epoch; await this.save();
        const value: ProviderDispatch = await this.provider(row, 'unknown', { operation_id: row.operation_id,
          capability_id: row.capability_id, epoch: observed.epoch }) as ProviderDispatch;
        this.response(row, value, 'unknown'); row.last_remote_state = value.state;
      }
      await this.save();
    } catch (error) { row.error = 'mesh_work_original_reconciliation_unavailable'; await this.save(); }
    return row;
  }
}
