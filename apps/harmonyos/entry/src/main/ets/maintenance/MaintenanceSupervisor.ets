import { common } from '@kit.AbilityKit';
import { util } from '@kit.ArkTS';
import { UpdateCoordinator } from '../update/UpdateCoordinator';
import { UpdateDB, UpdateState } from '../update/UpdateDB';
import { UpdateTrust } from '../update/Manifest';
import { MaintenanceJournal, MaintenanceRun } from './MaintenanceJournal';
import { MaintenanceApiError, maintenanceErrorCode } from './MaintenanceError';

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
  constructor(context: common.Context) { this.context = context; }
  stop(): void {
    this.stopped = true;
    if (this.coordinator !== null) { this.coordinator.cancel(); }
  }
  private async state(): Promise<UpdateState> {
    const db: UpdateDB = await UpdateDB.open(this.context);
    try { return await db.state(); } finally { await db.close(); }
  }
  async runScheduled(): Promise<void> {
    if (this.running) { throw new Error('maintenance_callback_already_running'); }
    this.running = true;
    const id: string = util.generateRandomUUID();
    let journal: MaintenanceJournal | null = null;
    let began: boolean = false;
    try {
      journal = await MaintenanceJournal.open(this.context);
      await journal.begin(id, 'system_work_scheduler'); began = true;
      if (this.stopped) { throw new Error('maintenance_stop_before_execution'); }
      const before: UpdateState = await this.state();
      if (before.trust.length === 0 || !(JSON.parse(before.trust) as UpdateTrust).enabled) {
        await journal.finish(id, before.trust.length === 0 ? 'unconfigured' : 'disabled', false,
          before.active, before.pending.length > 0, 0);
        return;
      }
      this.coordinator = new UpdateCoordinator(this.context);
      // This real path already enforces a 90s deadline, 1MiB total response
      // budget, pinned signature/key/node, exact routes and immutable hashes.
      // It never calls NativeServices, selected files, request.agent or a model.
      await this.coordinator.syncSmall();
      const state: UpdateState = await this.state();
      await journal.finish(id, this.stopped ? 'stopped' :
        state.trust.length === 0 ? 'unconfigured' : state.pending.length > 0 ? 'pending_foreground' : 'check_finished',
        this.stopped, state.active, state.pending.length > 0, 0);
    } catch (error) {
      if (journal !== null && began) {
        await journal.finish(id, this.stopped ? 'stopped' : 'unconfirmed_old_data_retained', this.stopped,
          0, false, maintenanceErrorCode(error as Error));
      }
      if (this.stopped) { throw new Error('maintenance_callback_stopped'); }
      throw new MaintenanceApiError(error as Error);
    } finally {
      this.coordinator = null; this.running = false;
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
