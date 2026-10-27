import { WorkSchedulerExtensionAbility, workScheduler } from '@kit.BackgroundTasksKit';
import { UpdateCoordinator } from '../update/UpdateCoordinator';
import { UPDATE_WORK_ID } from './Schedule';

export default class AmitaUpdateWork extends WorkSchedulerExtensionAbility {
  private coordinator: UpdateCoordinator | null = null;
  onWorkStart(work: workScheduler.WorkInfo): void {
    if (work.workId !== UPDATE_WORK_ID || this.coordinator !== null) { return; }
    const coordinator: UpdateCoordinator = new UpdateCoordinator(this.context);
    this.coordinator = coordinator;
    coordinator.syncSmall().catch((): void => {}).finally((): void => {
      if (this.coordinator === coordinator) { this.coordinator = null; }
    });
  }
  onWorkStop(work: workScheduler.WorkInfo): void {
    if (work.workId === UPDATE_WORK_ID && this.coordinator !== null) { this.coordinator.cancel(); }
  }
}
