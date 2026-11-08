import { common } from '@kit.AbilityKit';
import { preferences, relationalStore } from '@kit.ArkData';
import { util } from '@kit.ArkTS';
import { http } from '@kit.NetworkKit';
import { NodeCredential, NodeCredentialStore } from '../security/NodeCredentialStore';
import { WorkConfig, WorkSnapshot, validateWorkConfig } from '../core/WorkProtocol';
import { UpdateDB } from '../update/UpdateDB';
import { DiagnosticSnapshot, IncidentRecord, MaintenanceArguments, MaintenanceReply, SelfMaintenanceSupervisor,
  SupervisorHost, SupervisorState, freshSupervisor, restoreSupervisor } from './SelfMaintenance';

export interface MaintenanceStatus {
  enabled: boolean; local_cognition: string; execution: string; incident_id: string;
  task_id: string; operation_id: string; task_status: string; result: string;
}

// Called once from a real foreground/system opportunity. No timer or invented
// OS wake capability. The existing update lease serializes this journal with
// foreground/background signed data maintenance; every RPC remains bounded.
export class SelfMaintenanceClient implements SupervisorHost {
  private context: common.Context;
  private database: relationalStore.RdbStore | null = null;
  private owner: string = '';
  private config: WorkConfig | null = null;
  private credential: NodeCredential | null = null;
  private request: http.HttpRequest | null = null;
  private stopped: boolean = false;
  private busy: boolean = false;
  private deadline: number = 0;
  constructor(context: common.Context) { this.context = context; }
  id(): string { return util.generateRandomUUID(); }
  stop(): void {
    this.stopped = true;
    if (this.request !== null) { this.request.destroy(); this.request = null; }
  }
  private check(): void {
    if (this.stopped || Date.now() >= this.deadline) { throw new Error('mesh_self_maintenance_budget_or_stop'); }
  }
  async save(state: SupervisorState): Promise<void> {
    this.check();
    // The journal shares the incumbent update DB, not a second task ledger.
    // A stale callback cannot overwrite after its lease expired/replaced.
    await (this.database as relationalStore.RdbStore).executeSql(
      'UPDATE phone_self_maintenance SET state=? WHERE id=1 AND EXISTS (SELECT 1 FROM update_state WHERE id=1 AND lease=? AND lease_until>?)',
      [JSON.stringify(state), this.owner, Date.now()]);
    const rows: relationalStore.ResultSet = await (this.database as relationalStore.RdbStore).querySql(
      'SELECT lease FROM update_state WHERE id=1 AND lease=? AND lease_until>?', [this.owner, Date.now()]);
    try { if (!rows.goToFirstRow()) { throw new Error('mesh_self_maintenance_lease_lost'); } }
    finally { rows.close(); }
  }
  async exchange(action: string, args: MaintenanceArguments): Promise<MaintenanceReply> {
    this.check();
    const credential: NodeCredential = this.credential as NodeCredential;
    if (!credential.scope?.includes('phone:maintenance')) { throw new Error('mesh_self_maintenance_scope_required'); }
    const transport: http.HttpRequest = http.createHttp();
    this.request = transport;
    try {
      const remaining: number = Math.max(1, Math.min(5000, this.deadline - Date.now()));
      const response: http.HttpResponse = await transport.request((this.config as WorkConfig).resource_url, {
        method: http.RequestMethod.POST,
        header: { 'Authorization': 'Bearer ' + credential.token, 'Content-Type': 'application/json', 'Accept': 'application/json' },
        extraData: JSON.stringify({ action: action, arguments: args }),
        expectDataType: http.HttpDataType.STRING, connectTimeout: remaining, readTimeout: remaining,
        usingCache: false, maxLimit: 131072, maxRedirects: 0
      });
      this.check();
      if (response.responseCode !== 200 || typeof response.result !== 'string' || response.result.length > 131072) {
        throw new Error('mesh_self_maintenance_authority_unconfirmed');
      }
      return JSON.parse(response.result as string) as MaintenanceReply;
    } finally { transport.destroy(); if (this.request === transport) { this.request = null; } }
  }
  private async open(): Promise<SupervisorState> {
    this.database = await relationalStore.getRdbStore(this.context, {
      name: 'amita_updates_v1.db', securityLevel: relationalStore.SecurityLevel.S2
    });
    await this.database.executeSql('CREATE TABLE IF NOT EXISTS phone_self_maintenance (id INTEGER PRIMARY KEY CHECK(id=1), state TEXT NOT NULL)');
    await this.database.executeSql('INSERT OR IGNORE INTO phone_self_maintenance(id,state) VALUES(1,?)', [JSON.stringify(freshSupervisor())]);
    const rows: relationalStore.ResultSet = await this.database.querySql('SELECT state FROM phone_self_maintenance WHERE id=1');
    try {
      if (!rows.goToFirstRow()) { throw new Error('mesh_self_maintenance_state_missing'); }
      const state: SupervisorState = JSON.parse(rows.getString(0)) as SupervisorState;
      restoreSupervisor(state); return state;
    } finally { rows.close(); }
  }
  private async close(): Promise<void> {
    if (this.database !== null) { await this.database.close(); this.database = null; }
    this.credential = null; this.config = null; this.owner = '';
  }
  async setEnabled(enabled: boolean): Promise<void> {
    if (this.busy) { throw new Error('mesh_self_maintenance_busy'); }
    this.busy = true;
    this.stopped = false; this.deadline = Date.now() + 10000;
    let updates: UpdateDB | null = null;
    try {
      updates = await UpdateDB.open(this.context);
      this.owner = await updates.acquire();
      const state: SupervisorState = await this.open();
      await new SelfMaintenanceSupervisor(state, this).setEnabled(enabled);
    } finally {
      try {
        if (updates !== null) {
          if (this.owner.length > 0) { await updates.release(this.owner); }
          await updates.close();
        }
      } finally { await this.close(); this.busy = false; }
    }
  }
  async runOnce(snapshot: DiagnosticSnapshot): Promise<MaintenanceStatus> {
    if (this.busy) { throw new Error('mesh_self_maintenance_busy'); }
    this.busy = true;
    this.stopped = false; this.deadline = Date.now() + 20000;
    let updates: UpdateDB | null = null;
    try {
      updates = await UpdateDB.open(this.context);
      this.owner = await updates.acquire();
      const state: SupervisorState = await this.open();
      const status: MaintenanceStatus = { enabled: state.enabled, local_cognition: 'not_configured',
        execution: state.enabled ? 'not_configured' : 'disabled', incident_id: '', task_id: '',
        operation_id: '', task_status: '', result: '' };
      if (!state.enabled) { return status; }
      const store: preferences.Preferences = preferences.getPreferencesSync(this.context, { name: 'mesh_work_v1' });
      const raw: string = store.getSync('snapshot', '') as string;
      const saved: WorkSnapshot | null = raw.length === 0 ? null : JSON.parse(raw) as WorkSnapshot;
      this.credential = await new NodeCredentialStore().load();
      if (saved === null || saved.config === null || this.credential === null ||
        !this.credential.scope?.includes('phone:maintenance')) { return status; }
      this.config = saved.config;
      validateWorkConfig(this.config, this.credential.nodeId);
      const supervisor: SelfMaintenanceSupervisor = new SelfMaintenanceSupervisor(state, this);
      // Persist cold-start attempted -> unknown before any network access.
      await this.save(state);
      let row: IncidentRecord | null = await supervisor.incident(snapshot);
      if (row === null) {
        for (let i: number = 0; i < state.records.length; i++) {
          if (state.records[i].state !== 'finished') { row = state.records[i]; break; }
        }
      }
      if (row === null) { status.execution = 'healthy'; return status; }
      try { await supervisor.advance(this.config.session_id, row); status.execution = row.state; }
      catch (error) { status.execution = 'unconfirmed'; }
      status.incident_id = row.incident_id; status.task_id = row.task_id;
      status.operation_id = row.operation_id; status.task_status = row.task_status; status.result = row.result;
      return status;
    } finally {
      try {
        if (updates !== null) {
          if (this.owner.length > 0) { await updates.release(this.owner); }
          await updates.close();
        }
      } finally { await this.close(); this.busy = false; }
    }
  }
}
