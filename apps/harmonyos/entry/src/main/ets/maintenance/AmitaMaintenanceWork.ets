import { WorkSchedulerExtensionAbility, workScheduler } from '@kit.BackgroundTasksKit';
import { hilog } from '@kit.PerformanceAnalysisKit';
import { UPDATE_WORK_ID } from '../background/Schedule';
import { MaintenanceSupervisor } from './MaintenanceSupervisor';
import { maintenanceErrorCode } from './MaintenanceError';

// Root points the existing AmitaUpdateWork module srcEntry here. Keep its
// identity, schedule and conditions; do not register a second periodic job.
export default class AmitaMaintenanceWork extends WorkSchedulerExtensionAbility {
  private supervisor: MaintenanceSupervisor | null = null;
  onWorkStart(work: workScheduler.WorkInfo): void {
    if (work.workId !== UPDATE_WORK_ID || this.supervisor !== null) { return; }
    const supervisor: MaintenanceSupervisor = new MaintenanceSupervisor(this.context);
    this.supervisor = supervisor;
    supervisor.runScheduled().catch((error: Error): void => {
      // Persisted run remains diagnostic; do not swallow an exception as success.
      hilog.error(0x0000, 'AMITA_MAINTENANCE', 'callback failed or stopped code=%{public}d', maintenanceErrorCode(error));
    }).finally((): void => {
      if (this.supervisor === supervisor) { this.supervisor = null; }
    });
  }
  onWorkStop(work: workScheduler.WorkInfo): void {
    if (work.workId === UPDATE_WORK_ID && this.supervisor !== null) { this.supervisor.stop(); }
  }
}
