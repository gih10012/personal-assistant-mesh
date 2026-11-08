import { AbilityConstant, UIAbility, Want } from '@kit.AbilityKit';
import { window } from '@kit.ArkUI';
import { MaintenanceService } from '../update/MaintenanceService';
import { PhoneControlBridge } from '../control/PhoneControlBridge';

export default class EntryAbility extends UIAbility {
  private maintenance: MaintenanceService | null = null;
  private poll: number = -1;
  onCreate(want: Want, _launchParam: AbilityConstant.LaunchParam): void {
    AppStorage.setOrCreate<boolean>('meshForeground', false);
    AppStorage.setOrCreate<string>('meshUpdateStatus', '{"state":"initializing"}');
    this.maintenance = new MaintenanceService(this.context);
    PhoneControlBridge.onNewWant(this.context, want).catch((): void => {});
  }
  onNewWant(want: Want, _launchParam: AbilityConstant.LaunchParam): void {
    PhoneControlBridge.onNewWant(this.context, want).catch((): void => {});
  }
  onWindowStageCreate(stage: window.WindowStage): void { stage.loadContent('pages/Index'); }
  private async publish(): Promise<void> {
    if (this.maintenance !== null) {
      try { AppStorage.setOrCreate<string>('meshUpdateStatus', JSON.stringify(await this.maintenance.status())); }
      catch (_) { AppStorage.setOrCreate<string>('meshUpdateStatus', '{"state":"unavailable"}'); }
    }
  }
  onForeground(): void {
    AppStorage.setOrCreate<boolean>('meshForeground', true);
    if (this.maintenance !== null) {
      this.maintenance.restoreSchedule().catch((): void => {}).then((): Promise<void> => this.publish());
      this.maintenance.syncForeground().catch((): void => {}).then((): Promise<void> => this.publish());
    }
    if (this.poll !== -1) { clearInterval(this.poll); }
    this.poll = setInterval((): void => {
      if (this.maintenance !== null) {
        this.maintenance.reconcileForeground().catch((): void => {}).then((): Promise<void> => this.publish());
      }
    }, 5000);
  }
  onBackground(): void {
    AppStorage.setOrCreate<boolean>('meshForeground', false);
    if (this.poll !== -1) { clearInterval(this.poll); this.poll = -1; }
    if (this.maintenance !== null) { this.maintenance.stop(); }
  }
  onDestroy(): void { this.onBackground(); }
}
