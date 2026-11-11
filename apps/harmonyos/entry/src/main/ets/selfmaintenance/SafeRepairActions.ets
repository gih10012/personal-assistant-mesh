import { common } from '@kit.AbilityKit';
import { util } from '@kit.ArkTS';
import { http } from '@kit.NetworkKit';
import { NodeCredential } from '../security/NodeCredentialStore';
import { PhoneResponse, ProviderAllocation, ProviderDispatch, ReportArguments, WorkConfig, WorkSnapshot, validateWorkConfig } from '../core/WorkProtocol';
import { DataManifest, UpdateTrust, requireUpdate, validateTrust } from '../update/Manifest';
import { UpdateDB, UpdateState } from '../update/UpdateDB';
import { UpdateFiles } from '../update/UpdateFiles';
import { digest, verifyEnvelope } from '../update/UpdateCrypto';
import { UpdateCoordinator } from '../update/UpdateCoordinator';
import { RepairPlan } from './LocalRepair';
import { RepairCheckpoint, RepairEvidence } from './SelfMaintenance';
interface RepairInspectRequest { action: string; arguments: ReportArguments; }

// These dependencies are deployment code objects, not model request fields.
// The existing SelfMaintenanceClient retains its update lease and journal.
export class SafeRepairActions {
  private context: common.Context;
  private updates: UpdateDB;
  private owner: string;
  private work: WorkSnapshot;
  private credential: NodeCredential;
  private remaining: () => number;
  private ownerAllowed: () => boolean;
  private stopped: boolean = false;
  private coordinator: UpdateCoordinator | null = null;
  private request: http.HttpRequest | null = null;
  constructor(context: common.Context, updates: UpdateDB, owner: string, work: WorkSnapshot,
    credential: NodeCredential, remaining: () => number, ownerAllowed: () => boolean = (): boolean => true) {
    this.context = context; this.updates = updates; this.owner = owner;
    this.work = work; this.credential = credential; this.remaining = remaining;
    this.ownerAllowed = ownerAllowed;
    requireUpdate(work.node_id === credential.nodeId && work.config !== null, 'repair_original_node_required');
    requireUpdate(Array.isArray(work.records) && work.records.length <= 256, 'repair_original_work_journal_required');
    validateWorkConfig(work.config as WorkConfig, credential.nodeId);
  }
  stop(): void {
    this.stopped = true;
    if (this.coordinator !== null) { this.coordinator.cancel(); }
    if (this.request !== null) { this.request.destroy(); this.request = null; }
  }
  private async state(): Promise<UpdateState> {
    requireUpdate(!this.stopped && this.remaining() > 0 && this.ownerAllowed(), 'repair_budget_or_stop');
    const state: UpdateState = await this.updates.state();
    requireUpdate(!this.stopped && this.remaining() > 0 && state.lease === this.owner && this.owner.length > 0,
      'repair_original_lease_required');
    return state;
  }
  private async verified(raw: string, version: number, state: UpdateState): Promise<string> {
    requireUpdate(raw.length > 0 && state.trust.length > 0, 'repair_signed_version_required');
    const trust: UpdateTrust = JSON.parse(state.trust) as UpdateTrust;
    validateTrust(trust, this.credential.nodeId);
    const manifest: DataManifest = await verifyEnvelope(raw, trust, 0, true);
    requireUpdate(manifest.version === version, 'repair_original_signed_version_changed');
    const files: UpdateFiles = new UpdateFiles(this.context);
    for (let index: number = 0; index < manifest.artifacts.length; index++) {
      await this.state();
      requireUpdate(await files.valid(manifest.artifacts[index]), 'repair_complete_signed_bytes_required');
    }
    return digest(new util.TextEncoder().encodeInto(raw));
  }
  async capture(plan: RepairPlan): Promise<RepairCheckpoint> {
    const state: UpdateState = await this.state();
    let hash: string = '';
    if (plan.action === 'rollback_signed_data') {
      requireUpdate(state.previous === plan.target_version && state.previous > 0, 'repair_original_previous_version_required');
      hash = await this.verified(state.previous_json, state.previous, state);
    }
    return { active_version: state.active, target_version: plan.target_version, target_manifest_sha256: hash };
  }
  async execute(plan: RepairPlan, checkpoint: RepairCheckpoint): Promise<RepairEvidence> {
    let state: UpdateState = await this.state();
    if (plan.action === 'observe_receipts') { return this.inspectReceipts(); }
    if (plan.action === 'sync_signed_data') {
      this.coordinator = new UpdateCoordinator(this.context);
      try { await this.coordinator.syncSmall(this.updates, this.owner,
        Math.min(90000, Math.floor(this.remaining() - 500)), this.ownerAllowed); }
      finally { this.coordinator = null; }
      state = await this.state();
      if (state.active > 0 && state.pending.length === 0) {
        await this.verified(state.active_json, state.active, state);
        return { outcome: 'confirmed', active_version: state.active, observed_receipts: 0, target_manifest_sha256: '' };
      }
      return { outcome: 'unconfirmed', active_version: state.active, observed_receipts: 0, target_manifest_sha256: '' };
    }
    requireUpdate(plan.action === 'rollback_signed_data' && state.active === checkpoint.active_version &&
      state.previous === plan.target_version && checkpoint.target_version === plan.target_version, 'repair_rollback_source_changed');
    requireUpdate(await this.verified(state.previous_json, state.previous, state) === checkpoint.target_manifest_sha256,
      'repair_rollback_signed_manifest_changed');
    await this.state();
    await this.updates.rollback(this.owner);  // Existing fenced pointer switch, highest retained.
    return this.observe(plan, checkpoint);
  }
  async observe(plan: RepairPlan, checkpoint: RepairCheckpoint): Promise<RepairEvidence> {
    if (plan.action === 'observe_receipts') { return this.inspectReceipts(); } // Read-only repeat is safe.
    const state: UpdateState = await this.state();
    if (plan.action === 'rollback_signed_data' && state.active === checkpoint.target_version &&
      state.active > 0 && await this.verified(state.active_json, state.active, state) === checkpoint.target_manifest_sha256) {
      return { outcome: 'confirmed', active_version: state.active, observed_receipts: 0,
        target_manifest_sha256: checkpoint.target_manifest_sha256 };
    }
    // No second sync/rollback for an original unknown intent. Current data is
    // an observation, never fabricated proof that this attempt executed.
    return { outcome: 'unconfirmed', active_version: state.active, observed_receipts: 0, target_manifest_sha256: '' };
  }
  private async inspectReceipts(): Promise<RepairEvidence> {
    const config: WorkConfig = this.work.config as WorkConfig;
    let count: number = 0; let pending: number = 0;
    for (let index: number = 0; index < this.work.records.length; index++) {
      const row = this.work.records[index];
      if (row.state === 'settled') { continue; }
      pending++;
      if (count >= 4) { continue; }  // Bounded opportunity, not an infinite reconciliation daemon.
      await this.state();
      const transport: http.HttpRequest = http.createHttp(); this.request = transport;
      try {
        const timeout: number = Math.max(1, Math.min(5000, this.remaining()));
        const args: ReportArguments = { session_id: config.session_id, report_id: util.generateRandomUUID(),
          provider: { action: 'inspect', arguments: { operation_id: row.operation_id, capability_id: row.capability_id } } };
        const request: RepairInspectRequest = { action: 'mesh_work_report', arguments: args };
        const response: http.HttpResponse = await transport.request(config.resource_url, {
          method: http.RequestMethod.POST,
          header: { 'Authorization': 'Bearer ' + this.credential.token, 'Content-Type': 'application/json' },
          extraData: JSON.stringify(request),
          expectDataType: http.HttpDataType.STRING, usingCache: false, maxLimit: 131072, maxRedirects: 0,
          connectTimeout: timeout, readTimeout: timeout
        });
        await this.state();
        requireUpdate(response.responseCode === 200 && typeof response.result === 'string', 'repair_receipt_unconfirmed');
        const reply: PhoneResponse = JSON.parse(response.result as string) as PhoneResponse;
        const allocation: ProviderAllocation = reply.provider as ProviderAllocation;
        requireUpdate(reply.format === 'mesh-interactive-node/1' && reply.mesh_id === config.mesh_id && allocation !== null &&
          allocation.operation_id === row.operation_id && allocation.task_id === row.task_id &&
          allocation.task_epoch === row.task_epoch && Array.isArray(allocation.dispatch), 'repair_original_allocation_changed');
        const matches: ProviderDispatch[] = allocation.dispatch.filter((value: ProviderDispatch): boolean => value.capability_id === row.capability_id);
        requireUpdate(matches.length === 1 && matches[0].operation_id === row.operation_id &&
          matches[0].provider === 'node:' + config.node_id &&
          (matches[0].receipt_id === null || matches[0].receipt_id === row.receipt_id), 'repair_original_receipt_changed');
        count++;
      } finally { transport.destroy(); if (this.request === transport) { this.request = null; } }
    }
    const state: UpdateState = await this.state();
    // Work records are NEVER cleared, settled, accepted, started or reexecuted here.
    return { outcome: count === pending ? 'confirmed' : 'unconfirmed', active_version: state.active,
      observed_receipts: count, target_manifest_sha256: '' };
  }
}
