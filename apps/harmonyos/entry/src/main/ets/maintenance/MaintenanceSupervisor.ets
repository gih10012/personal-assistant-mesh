import { common } from '@kit.AbilityKit';
import { util } from '@kit.ArkTS';
import { UpdateCoordinator } from '../update/UpdateCoordinator';
import { UpdateDB, UpdateState } from '../update/UpdateDB';
import { UpdateTrust } from '../update/Manifest';
import { MaintenanceJournal, MaintenanceRun } from './MaintenanceJournal';
import { MaintenanceApiError, maintenanceErrorCode } from './MaintenanceError';
import { preferences } from '@kit.ArkData';
import { SelfMaintenanceClient } from '../selfmaintenance/SelfMaintenanceClient';
import { WorkSnapshot } from '../core/WorkProtocol';

export interface MaintenanceObservation {
  runtime: string;
  cognition: string;
  cognitionReason: string;
  alwaysRunning: boolean;
  callbacks: MaintenanceRun[];
}

// Model-independent bounded supervision, not an LLM execution claim. The
// existing narrow phone credential has no cognition-delegation permission.
export class MaintenanceSupervisor {
  private context: common.Context;
  private coordinator: UpdateCoordinator | null = null;
  private running: boolean = false;
  private stopped: boolean = false;
  private self: SelfMaintenanceClient | null = null;
  constructor(context: common.Context) { this.context = context; }
  stop(): void {
    this.stopped = true;
    if (this.coordinator !== null) { this.coordinator.cancel(); }
    if (this.self !== null) { this.self.stop(); }
  }
  private async state(): Promise<UpdateState> {
    const db: UpdateDB = await UpdateDB.open(this.context);
    try { return await db.state(); } finally { await db.close(); }
  }
  async runScheduled(): Promise<void> {
    if (this.running) { throw new Error('maintenance_callback_already_running'); }
    this.running = true;
    const timeout: number = setTimeout((): void => this.stop(), 110000);
    const id: string = util.generateRandomUUID();
    let journal: MaintenanceJournal | null = null;
    let began: boolean = false;
    try {
      journal = await MaintenanceJournal.open(this.context);
      await journal.begin(id, 'system_work_scheduler'); began = true;
      if (this.stopped) { throw new Error('maintenance_stop_before_execution'); }
      const before: UpdateState = await this.state();
      this.self = new SelfMaintenanceClient(this.context);
      const selfEnabled: boolean = await this.self.isEnabled();
      const dataEnabled: boolean = before.trust.length > 0 && (JSON.parse(before.trust) as UpdateTrust).enabled;
      if (!dataEnabled && !selfEnabled) {
        await journal.finish(id, before.trust.length === 0 ? 'unconfigured' : 'disabled', false,
          before.active, before.pending.length > 0, 0);
        return;
      }
      let syncState: string = 'ok';
      // This real path already enforces a 90s deadline, 1MiB total response
      // budget, pinned signature/key/node, exact routes and immutable hashes.
      // It never calls NativeServices, selected files, request.agent or a model.
      if (dataEnabled) {
        this.coordinator = new UpdateCoordinator(this.context);
        try { await this.coordinator.syncSmall(); }
        catch (error) {
          syncState = 'unconfirmed';
          await journal.finish(id, this.stopped ? 'stopped' : 'unconfirmed_old_data_retained', this.stopped,
            before.active, before.pending.length > 0, maintenanceErrorCode(error as Error));
        }
      }
      const state: UpdateState = await this.state();
      if (selfEnabled && !this.stopped) {
        // The update coordinator released its lease before this separate bounded
        // maintenance opportunity. No model is called for a healthy snapshot.
        const store: preferences.Preferences = preferences.getPreferencesSync(this.context, { name: 'mesh_work_v1' });
        const raw: string = store.getSync('snapshot', '') as string;
        const work: WorkSnapshot | null = raw.length === 0 ? null : JSON.parse(raw) as WorkSnapshot;
        const pending: number = work === null ? 0 : work.records.filter(row =>
          row.state === 'unknown' || row.state === 'result_pending' || row.state === 'settlement_pending').length;
        await this.self.runOnce({ diagnostic: dataEnabled ? 'signed_data_sync' : 'receipt_reconciliation',
          state: dataEnabled ? syncState : pending > 0 ? 'unconfirmed' : 'ok',
          data_version: state.active, pending_receipts: Math.min(256, pending) });
      }
      if (syncState === 'ok') { await journal.finish(id, this.stopped ? 'stopped' :
        state.trust.length === 0 ? 'unconfigured' : state.pending.length > 0 ? 'pending_foreground' : 'check_finished',
        this.stopped, state.active, state.pending.length > 0, 0); }
    } catch (error) {
      if (journal !== null && began) {
        await journal.finish(id, this.stopped ? 'stopped' : 'unconfirmed_old_data_retained', this.stopped,
          0, false, maintenanceErrorCode(error as Error));
      }
      if (this.stopped) { throw new Error('maintenance_callback_stopped'); }
      throw new MaintenanceApiError(error as Error);
    } finally {
      this.coordinator = null; this.running = false;
      this.self = null; clearTimeout(timeout);
      if (journal !== null) { await journal.close(); }
    }
  }
  async status(): Promise<MaintenanceObservation> {
    const journal: MaintenanceJournal = await MaintenanceJournal.open(this.context);
    try {
      return { runtime: 'bounded_system_callback', cognition: 'not_configured', cognitionReason: 'no_authorized_route',
        alwaysRunning: false, callbacks: await journal.recent() };
    } finally { await journal.close(); }
  }
}
