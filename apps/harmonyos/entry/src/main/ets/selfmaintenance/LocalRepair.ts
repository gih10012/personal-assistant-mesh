import type { IncidentRecord, RepairCheckpoint, RepairEvidence, RepairRecord, SupervisorState } from './SelfMaintenance';

export interface RepairPlan {
  format: string; incident_id: string; task_id: string; task_epoch: number;
  operation_id: string; action: string; target_version: number;
}
// Installed by explicit local owner UI/code. Never loaded from a model reply,
// runtime routing config or phone service JSON. Default allows nothing.
export interface LocalRepairPolicy { sync_signed_data: boolean; observe_receipts: boolean; rollback_versions: number[]; }
interface NumericRepairError extends Error { code?: number; }
export function noLocalRepairs(): LocalRepairPolicy {
  return { sync_signed_data: false, observe_receipts: false, rollback_versions: [] };
}
export interface LocalRepairHost {
  remaining(): number;
  save(state: SupervisorState): Promise<void>;
  capture(plan: RepairPlan): Promise<RepairCheckpoint>;
  execute(plan: RepairPlan, checkpoint: RepairCheckpoint): Promise<RepairEvidence>;
  observe(plan: RepairPlan, checkpoint: RepairCheckpoint): Promise<RepairEvidence>;
}
function requireRepair(value: boolean, reason: string): void {
  if (!value) { throw new Error('mesh_local_repair_' + reason); }
}
export function parseRepairPlan(raw: string, row: IncidentRecord): RepairPlan {
  requireRepair(typeof raw === 'string' && raw.length <= 8192, 'plan_budget');
  const plan: RepairPlan = JSON.parse(raw) as RepairPlan;
  requireRepair(plan !== null && typeof plan === 'object' &&
    Object.keys(plan).sort().join('|') === 'action|format|incident_id|operation_id|target_version|task_epoch|task_id' &&
    plan.format === 'amita-phone-maintenance-repair/1' && row.state === 'finished' && row.task_status === 'completed' &&
    plan.incident_id === row.incident_id && plan.task_id === row.task_id && plan.operation_id === row.operation_id &&
    Number.isSafeInteger(plan.task_epoch) && plan.task_epoch > 0 && plan.task_epoch === row.task_epoch &&
    ['sync_signed_data', 'observe_receipts', 'rollback_signed_data'].includes(plan.action) &&
    Number.isSafeInteger(plan.target_version) &&
    (plan.action === 'rollback_signed_data' ? plan.target_version > 0 : plan.target_version === 0), 'original_completed_plan_required');
  // Normalize exact permitted fields, retaining no model-supplied executable,
  // URL, file path, grant, role, receipt body or free-form instruction.
  return { format: plan.format, incident_id: row.incident_id, task_id: row.task_id,
    task_epoch: plan.task_epoch, operation_id: row.operation_id, action: plan.action, target_version: plan.target_version };
}
function permitted(plan: RepairPlan, policy: LocalRepairPolicy): boolean {
  requireRepair(typeof policy.sync_signed_data === 'boolean' && typeof policy.observe_receipts === 'boolean' &&
    Array.isArray(policy.rollback_versions) && policy.rollback_versions.length <= 16 &&
    policy.rollback_versions.every((version: number): boolean => Number.isSafeInteger(version) && version > 0), 'owner_policy_required');
  return plan.action === 'sync_signed_data' ? policy.sync_signed_data :
    plan.action === 'observe_receipts' ? policy.observe_receipts : policy.rollback_versions.includes(plan.target_version);
}
function evidence(value: RepairEvidence): void {
  requireRepair(value !== null && ['confirmed', 'unconfirmed'].includes(value.outcome) &&
    Number.isSafeInteger(value.active_version) && value.active_version >= 0 &&
    Number.isSafeInteger(value.observed_receipts) && value.observed_receipts >= 0 && value.observed_receipts <= 256 &&
    typeof value.target_manifest_sha256 === 'string' &&
    (value.target_manifest_sha256 === '' || /^[0-9a-f]{64}$/.test(value.target_manifest_sha256)), 'observation_invalid');
}

export class LocalRepairController {
  private state: SupervisorState;
  private host: LocalRepairHost;
  private policy: LocalRepairPolicy;
  constructor(state: SupervisorState, host: LocalRepairHost, policy: LocalRepairPolicy) {
    this.state = state; this.host = host; this.policy = policy;
  }
  async advance(row: IncidentRecord): Promise<RepairRecord | null> {
    requireRepair(this.state.enabled && this.state.records.includes(row), 'original_enabled_incident_required');
    const plan: RepairPlan = parseRepairPlan(row.result, row);
    requireRepair(permitted(plan, this.policy), 'local_owner_scope_required');
    // Keep the same original plan for a later OS opportunity, before creating
    // any new intent. Do not append a fresh 90s window to an exhausted callback.
    if (this.host.remaining() < 2000) { return row.repair === undefined ? null : row.repair; }
    const normalized: string = JSON.stringify(plan);
    if (row.repair !== undefined) {
      const old: RepairRecord = row.repair;
      requireRepair(old.plan_json === normalized, 'original_plan_changed');
      if (old.state === 'completed') { return old; }
      old.state = 'unknown'; await this.host.save(this.state);
      // The only path for a previous intent/attempt/unknown is read-only.
      // In particular, an unconfirmed rollback never calls rollback again.
      const observed: RepairEvidence = await this.host.observe(plan, old.checkpoint);
      evidence(observed); old.evidence = observed;
      if (observed.outcome === 'confirmed') { old.state = 'completed'; }
      await this.host.save(this.state); return old;
    }
    const checkpoint: RepairCheckpoint = await this.host.capture(plan);
    requireRepair(Number.isSafeInteger(checkpoint.active_version) && checkpoint.active_version >= 0 &&
      Number.isSafeInteger(checkpoint.target_version) && checkpoint.target_version === plan.target_version &&
      (plan.action !== 'rollback_signed_data' || /^[0-9a-f]{64}$/.test(checkpoint.target_manifest_sha256)), 'checkpoint_invalid');
    if (this.host.remaining() < 2000) { return null; } // Signature/full-byte reads also consume the original window.
    const record: RepairRecord = { format: 'amita-local-repair-record/1', plan_json: normalized, state: 'intent',
      checkpoint: checkpoint, evidence: null, error_code: 0 };
    row.repair = record;
    await this.host.save(this.state);  // Durable original intent BEFORE any effect.
    record.state = 'attempted'; await this.host.save(this.state);
    try {
      requireRepair(this.state.enabled && permitted(plan, this.policy), 'local_owner_scope_required');
      const result: RepairEvidence = await this.host.execute(plan, checkpoint);
      evidence(result); record.evidence = result;
      record.state = result.outcome === 'confirmed' ? 'completed' : 'unknown';
      await this.host.save(this.state); return record;
    } catch (error) {
      const code: number | undefined = (error as NumericRepairError).code;
      record.state = 'unknown'; record.error_code = Number.isSafeInteger(code) ? code as number : -1;
      await this.host.save(this.state); throw new Error('mesh_local_repair_original_attempt_unconfirmed');
    }
  }
}
