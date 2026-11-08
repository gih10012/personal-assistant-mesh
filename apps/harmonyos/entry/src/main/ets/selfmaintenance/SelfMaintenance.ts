// Deterministic device supervisor. Cognition runs only on a real Mesh worker.
// No interval, background-residency claim, repair command or model credential.
export interface DiagnosticSnapshot {
  diagnostic: string; state: string; data_version: number; pending_receipts: number;
}
export interface MaintenanceReply {
  format: string; incident_id: string; task_id: string; operation_id: string;
  status: string; task_epoch: number; task_created: boolean; local_cognition: string;
  result_available: boolean; automatic_repair: boolean; result?: string;
}
export interface IncidentRecord {
  incident_id: string; snapshot: DiagnosticSnapshot; state: string;
  task_id: string; operation_id: string; task_status: string; result: string;
  recovered: boolean;
}
export interface SupervisorState {
  format: string; enabled: boolean; records: IncidentRecord[];
}
export interface MaintenanceArguments { session_id: string; incident_id: string; snapshot?: DiagnosticSnapshot; }
export interface SupervisorHost {
  id(): string;
  save(state: SupervisorState): Promise<void>;
  exchange(action: string, args: MaintenanceArguments): Promise<MaintenanceReply>;
}
export function freshSupervisor(): SupervisorState {
  return { format: 'mesh-phone-supervisor/1', enabled: false, records: [] };
}
function requireState(value: boolean): void {
  if (!value) { throw new Error('mesh_self_maintenance_original_state_required'); }
}
export function validateSnapshot(value: DiagnosticSnapshot): void {
  requireState(value !== null && typeof value === 'object' &&
    ['signed_data_sync', 'receipt_reconciliation', 'node_connection'].includes(value.diagnostic) &&
    ['ok', 'unreachable', 'unconfirmed', 'verification_failed', 'not_configured'].includes(value.state) &&
    Number.isSafeInteger(value.data_version) && value.data_version >= 0 &&
    Number.isSafeInteger(value.pending_receipts) && value.pending_receipts >= 0 && value.pending_receipts <= 256);
}
export function restoreSupervisor(state: SupervisorState): void {
  requireState(state.format === 'mesh-phone-supervisor/1' && typeof state.enabled === 'boolean' &&
    Array.isArray(state.records) && state.records.length <= 64);
  for (let i: number = 0; i < state.records.length; i++) {
    const row: IncidentRecord = state.records[i];
    validateSnapshot(row.snapshot);
    if (row.recovered === undefined) { row.recovered = false; }
    requireState(/^[A-Za-z0-9_-]{1,96}$/.test(row.incident_id) &&
      typeof row.recovered === 'boolean' &&
      ['pending', 'attempted', 'unknown', 'accepted', 'finished'].includes(row.state));
    for (let j: number = 0; j < i; j++) { requireState(state.records[j].incident_id !== row.incident_id); }
    if (row.state === 'attempted') { row.state = 'unknown'; }
  }
}
export class SelfMaintenanceSupervisor {
  readonly state: SupervisorState;
  private host: SupervisorHost;
  constructor(state: SupervisorState, host: SupervisorHost) { this.state = state; this.host = host; }
  async setEnabled(enabled: boolean): Promise<void> { this.state.enabled = enabled; await this.host.save(this.state); }
  async incident(snapshot: DiagnosticSnapshot): Promise<IncidentRecord | null> {
    validateSnapshot(snapshot);
    if (!this.state.enabled) { return null; }
    if (snapshot.state === 'ok') {
      for (let i: number = 0; i < this.state.records.length; i++) {
        const row: IncidentRecord = this.state.records[i];
        if (row.snapshot.diagnostic === snapshot.diagnostic && row.state === 'finished') { row.recovered = true; }
      }
      await this.host.save(this.state); return null;
    }
    // An unresolved episode is immutable; changed observations never create a
    // replacement task for unknown effects. Explicitly begin a new episode
    // only after the original task finished and the failure recurred.
    for (let i: number = 0; i < this.state.records.length; i++) {
      const row: IncidentRecord = this.state.records[i];
      if (row.snapshot.diagnostic === snapshot.diagnostic && !row.recovered) { return row; }
    }
    requireState(this.state.records.length < 64);
    const captured: DiagnosticSnapshot = { diagnostic: snapshot.diagnostic, state: snapshot.state,
      data_version: snapshot.data_version, pending_receipts: snapshot.pending_receipts };
    const row: IncidentRecord = { incident_id: this.host.id(), snapshot: captured, state: 'pending',
      task_id: '', operation_id: '', task_status: '', result: '', recovered: false };
    requireState(/^[A-Za-z0-9_-]{1,96}$/.test(row.incident_id));
    this.state.records.push(row); await this.host.save(this.state); return row;
  }
  async advance(sessionId: string, row: IncidentRecord): Promise<void> {
    if (!this.state.enabled || row.state === 'finished') { return; }
    let action: string = 'mesh_maintenance_status';
    const args: MaintenanceArguments = { session_id: sessionId, incident_id: row.incident_id };
    if (row.state === 'pending') {
      row.state = 'attempted'; await this.host.save(this.state); // durable BEFORE POST
      action = 'mesh_maintenance_submit'; args.snapshot = row.snapshot;
    }
    try {
      const reply: MaintenanceReply = await this.host.exchange(action, args);
      this.observe(row, reply);
      if (!reply.result_available && ['completed', 'failed'].includes(reply.status)) { row.state = 'finished'; }
      await this.host.save(this.state);
      if (reply.result_available) {
        const output: MaintenanceReply = await this.host.exchange('mesh_maintenance_result',
          { session_id: sessionId, incident_id: row.incident_id });
        this.observe(row, output);
        requireState(typeof output.result === 'string' && output.result.length <= 65536);
        row.result = output.result as string;
        if (['completed', 'failed'].includes(output.status)) { row.state = 'finished'; }
        await this.host.save(this.state);
      }
    } catch (error) {
      if (row.state === 'attempted') { row.state = 'unknown'; }
      await this.host.save(this.state); throw new Error('mesh_self_maintenance_original_incident_unconfirmed');
    }
  }
  private observe(row: IncidentRecord, reply: MaintenanceReply): void {
    requireState(reply.format === 'mesh-phone-maintenance/1' && reply.incident_id === row.incident_id &&
      reply.local_cognition === 'not_configured' && reply.automatic_repair === false &&
      typeof reply.task_id === 'string' && reply.task_id.length > 0 &&
      typeof reply.operation_id === 'string' && reply.operation_id.length > 0 &&
      (row.task_id === '' || row.task_id === reply.task_id) &&
      (row.operation_id === '' || row.operation_id === reply.operation_id));
    row.task_id = reply.task_id; row.operation_id = reply.operation_id;
    row.task_status = reply.status; row.state = 'accepted';
  }
}
