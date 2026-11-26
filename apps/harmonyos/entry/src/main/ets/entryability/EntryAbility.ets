import { AbilityConstant, UIAbility, Want } from '@kit.AbilityKit';
import { window } from '@kit.ArkUI';
import { MaintenanceService } from '../update/MaintenanceService';
import { PhoneControlBridge } from '../control/PhoneControlBridge';
import { ComputerUseSurface } from '../control/ComputerUseSurface';
import { ApprovalSurface } from '../control/ApprovalSurface';
import { ScreenShareOwner } from '../screenshare/ScreenShareOwner';
import { ScreenShareRuntime } from '../screenshare/ScreenShareRuntime';

export default class EntryAbility extends UIAbility {
  private maintenance: MaintenanceService | null = null;
  private poll: number = -1;
  private shareRuntime: ScreenShareRuntime | null = null;
  private shareCreating: boolean = false;
  private destroyed: boolean = false;
  onCreate(want: Want, _launchParam: AbilityConstant.LaunchParam): void {
    ApprovalSurface.attachNotificationContext(this.context);
    AppStorage.setOrCreate<boolean>('meshForeground', false);
    AppStorage.setOrCreate<string>('meshUpdateStatus', '{"state":"initializing"}');
    this.maintenance = new MaintenanceService(this.context);
    this.handleWant(want).catch((): void => {});
  }
  onNewWant(want: Want, _launchParam: AbilityConstant.LaunchParam): void {
    this.handleWant(want).catch((): void => {});
  }
  private async handleWant(want: Want): Promise<void> {
    if (!await ComputerUseSurface.onNewWant(want)) { await PhoneControlBridge.onNewWant(this.context, want); }
  }
  onWindowStageCreate(stage: window.WindowStage): void { stage.loadContent('pages/Index'); }
  private async attachShareRuntime(): Promise<void> {
    if (this.destroyed || this.shareCreating || this.shareRuntime !== null ||
      AppStorage.get<boolean>('meshForeground') !== true) { return; }
    this.shareCreating = true;
    try {
      const runtime: ScreenShareRuntime = await ScreenShareRuntime.create(this.context);
      if (this.destroyed) { await runtime.destroy(); return; }
      this.shareRuntime = runtime;
      runtime.foregroundChanged();
    } catch (_) {
      // A fresh member may not have persisted its original identity yet.
      // Retry only during a later real foreground pass, without registering,
      // approving or starting capture on behalf of the owner.
    } finally { this.shareCreating = false; }
  }
  private async publish(): Promise<void> {
    if (this.maintenance !== null) {
      try { AppStorage.setOrCreate<string>('meshUpdateStatus', JSON.stringify(await this.maintenance.status())); }
      catch (_) { AppStorage.setOrCreate<string>('meshUpdateStatus', '{"state":"unavailable"}'); }
    }
  }
  onForeground(): void {
    AppStorage.setOrCreate<boolean>('meshForeground', true);
    ScreenShareOwner.foregroundChanged();
    if (this.shareRuntime !== null) { this.shareRuntime.foregroundChanged(); }
    else { this.attachShareRuntime(); }
    if (this.maintenance !== null) {
      this.maintenance.restoreSchedule().catch((): void => {}).then((): Promise<void> => this.publish());
      this.maintenance.syncForeground().catch((): void => {}).then((): Promise<void> => this.publish());
    }
    if (this.poll !== -1) { clearInterval(this.poll); }
    this.poll = setInterval((): void => {
      this.attachShareRuntime();
      if (this.maintenance !== null) {
        this.maintenance.reconcileForeground().catch((): void => {}).then((): Promise<void> => this.publish());
      }
    }, 5000);
  }
  onBackground(): void {
    AppStorage.setOrCreate<boolean>('meshForeground', false);
    ScreenShareOwner.foregroundChanged();
    if (this.shareRuntime !== null) { this.shareRuntime.foregroundChanged(); }
    if (this.poll !== -1) { clearInterval(this.poll); this.poll = -1; }
    if (this.maintenance !== null) { this.maintenance.stop(); }
  }
  onDestroy(): void {
    this.onBackground();
    ApprovalSurface.attachNotificationContext(null);
    this.destroyed = true;
    const runtime: ScreenShareRuntime | null = this.shareRuntime;
    this.shareRuntime = null;
    const stopped: Promise<void> = runtime === null ? Promise.resolve() : runtime.destroy();
    stopped.catch((): void => {}).then((): Promise<void> => ScreenShareOwner.destroyed()).catch((): void => {});
  }
}
