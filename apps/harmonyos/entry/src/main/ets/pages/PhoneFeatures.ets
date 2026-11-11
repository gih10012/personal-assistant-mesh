import { common, bundleManager } from '@kit.AbilityKit';
import { notificationManager } from '@kit.NotificationKit';
import { fileIo } from '@kit.CoreFileKit';
import { util } from '@kit.ArkTS';
import { PhoneControlBridge, PhoneControlOwnerOptions, PhoneControlPending,
  readPhoneControlOwner } from '../control/PhoneControlBridge';
import { ApprovalTicket, OwnerControlDecision } from '../control/ControlSession';
import { GlobalScreenCapture } from '../control/GlobalScreenCapture';
import { MaintenanceService } from '../update/MaintenanceService';
import { UpdateStatus } from '../update/Manifest';
import { MaintenanceSupervisor } from '../maintenance/MaintenanceSupervisor';
import { SelfMaintenanceClient, MaintenanceStatus } from '../selfmaintenance/SelfMaintenanceClient';
import { LocalRepairPolicy, noLocalRepairs } from '../selfmaintenance/LocalRepair';
import { UpdateDB } from '../update/UpdateDB';
import { NativeUpgrade } from '../upgrade/NativeUpgrade';
import { SelfHdcPanel } from '../selfhdc/SelfHdcPanel';

// Local owner controls, separate from the real Accessibility service lifecycle.
@Component
export struct PhoneFeatures {
  @Prop memberReady: boolean = false;
  @StorageLink('meshForeground') @Watch('visibilityChanged') foreground: boolean = false;
  @State controlEnabled: boolean = false;
  @State captureEnabled: boolean = false;
  @State dataEnabled: boolean = false;
  @State selfEnabled: boolean = false;
  @State busy: boolean = false;
  @State message: string = '';
  @State controlLabel: string = '尚未启用手机操控';
  @State systemServiceProbe: string = '';
  @State dataLabel: string = '尚未配置数据更新';
  @State maintenanceLabel: string = '等待系统维护机会';
  @State cognitionLabel: string = '本机监督按需运行；认知路由尚未确认';
  @State versionLabel: string = '';
  @State pendingSummary: string = '';
  @State pendingBody: string = '';
  @State pendingValid: boolean = false;
  private pending: ApprovalTicket | null = null;
  private timer: number = -1;
  private refreshing: boolean = false;
  private maintenance: MaintenanceService | null = null;
  private self: SelfMaintenanceClient | null = null;
  private upgrade: NativeUpgrade | null = null;
  private capture: GlobalScreenCapture = new GlobalScreenCapture();
  private context(): common.UIAbilityContext { return this.getUIContext().getHostContext() as common.UIAbilityContext; }
  aboutToAppear(): void { this.initialize(); }
  private async initialize(): Promise<void> {
    const context: common.UIAbilityContext = this.context();
    this.maintenance = new MaintenanceService(context, (): boolean => this.foreground);
    this.self = new SelfMaintenanceClient(context);
    this.upgrade = new NativeUpgrade(context, (): boolean => this.foreground);
    try {
      const info: bundleManager.BundleInfo = await bundleManager.getBundleInfoForSelf(bundleManager.BundleFlag.GET_BUNDLE_INFO_DEFAULT);
      this.versionLabel = info.versionName;
    } catch (_) { this.versionLabel = '版本暂不可读'; }
    await this.refresh();
    this.systemServiceProbe = await PhoneControlBridge.probeSystemRegistration(context);
    this.timer = setInterval((): void => { if (this.foreground) { this.refresh(); } }, 5000);
  }
  aboutToDisappear(): void {
    if (this.timer !== -1) { clearInterval(this.timer); this.timer = -1; }
    if (this.self !== null) { this.self.stop(); }
  }
  visibilityChanged(): void { if (!this.foreground && this.self !== null) { this.self.stop(); } }
  private async refresh(): Promise<void> {
    if (this.refreshing) { return; }
    this.refreshing = true;
    try {
      const context: common.UIAbilityContext = this.context();
      const owner: PhoneControlOwnerOptions = readPhoneControlOwner(context);
      this.controlEnabled = owner.enabled; this.captureEnabled = owner.screen_capture;
      const control = PhoneControlBridge.status(context);
      this.controlLabel = !owner.enabled ? '手机操控已关闭' : !control.service_connected ?
        '等待在系统设置启用辅助功能服务' : !control.root_verified ? '服务已连接，界面访问尚未验证' :
        control.connection === 'online' && control.presence_expires_at > Date.now() / 1000 ?
          '手机操控服务在线，可离开此页面' : '界面访问已验证，等待 Mesh 服务连接';
      const next: PhoneControlPending | null = PhoneControlBridge.pendingForOwner(context);
      // Never replace a command silently while the owner is reviewing it.
      if (next !== null && (this.pending === null || this.pending.approval_id === next.ticket.approval_id)) {
        this.pending = next.ticket; this.pendingSummary = next.ticket.summary; this.pendingBody = next.input_json;
      }
      this.pendingValid = next !== null && this.pending !== null && next.ticket.approval_id === this.pending.approval_id &&
        next.ticket.nonce === this.pending.nonce && this.pending.expires_at > Date.now();
      if (this.maintenance !== null) {
        const status: UpdateStatus = await this.maintenance.status();
        this.dataEnabled = status.enabled;
        this.dataLabel = status.state === 'unconfigured' ? '尚未导入可信更新配置' : !status.enabled ? '自动更新已关闭' :
          '数据版本 ' + status.activeVersion + ' · 待下载 ' + status.pendingDownloads + ' · 待核对 ' + status.unknown;
      }
      const observation = await new MaintenanceSupervisor(context).status();
      this.maintenanceLabel = '后台维护记录 ' + observation.callbacks.length + ' 次；执行时机由系统安排';
      if (this.self !== null) { this.selfEnabled = await this.self.isEnabled(); }
    } catch (_) { this.message = '状态暂不可读；保留原配置和任务'; }
    finally { this.refreshing = false; }
  }
  private async control(value: boolean): Promise<void> {
    if (!this.foreground || this.busy) { return; }
    this.busy = true;
    try {
      await PhoneControlBridge.ownerToggle(this.context(), value);
      this.message = value ? '请在系统设置搜索“无障碍”，启用 AMITA 手机操控服务；动作仍需本人确认' : '手机操控授权已撤回';
      await this.refresh();
    } catch (_) { this.message = '操控授权未确认；请核对原状态'; }
    finally { this.busy = false; }
  }
  private async allowCapture(): Promise<void> {
    if (!this.foreground || this.busy) { return; }
    this.busy = true;
    try {
      const allowed: boolean = await this.capture.requestPermissionFromOwnerGesture(this.context());
      const options: PhoneControlOwnerOptions = readPhoneControlOwner(this.context());
      options.screen_capture = allowed; await PhoneControlBridge.configure(this.context(), options);
      this.captureEnabled = allowed;
      this.message = allowed ? '已允许授权任务请求单次截图；不会自动录像' : '系统截图权限未授予';
    } catch (_) { this.message = '系统截图授权未确认'; }
    finally { this.busy = false; }
  }
  private async notification(): Promise<void> {
    try { await notificationManager.requestEnableNotification(this.context()); this.message = '请按系统结果核对通知开关'; }
    catch (_) { this.message = '通知授权未确认；没有自动批准任何请求'; }
  }
  private async data(value: boolean): Promise<void> {
    if (this.busy || !this.foreground || this.maintenance === null) { return; }
    this.busy = true;
    try {
      await this.maintenance.setEnabled(value);
      if (this.self !== null && this.selfEnabled) {
        const policy: LocalRepairPolicy = this.self.getRepairPolicy();
        policy.sync_signed_data = value; await this.self.setRepairPolicy(policy);
      }
      await this.refresh();
    }
    catch (_) { this.message = '系统维护调度未确认；原数据保留'; }
    finally { this.busy = false; }
  }
  private async enableSelf(value: boolean): Promise<void> {
    if (this.busy || !this.foreground || this.self === null) { return; }
    this.busy = true;
    try {
      const policy: LocalRepairPolicy = value ? { sync_signed_data: this.dataEnabled,
        observe_receipts: true, rollback_versions: [] } : noLocalRepairs();
      // Revoke the independent local grant before a potentially busy DB lease.
      await this.self.setRepairPolicy(policy);
      await this.self.setEnabled(value); this.selfEnabled = value;
      if (this.maintenance !== null) { await this.maintenance.restoreSchedule(); }
      this.cognitionLabel = value ? '已允许诊断、原回执只读核对，以及已开启的数据同步；原生升级和权限不会自动修改' : '自主诊断与数据自修授权已关闭';
    } catch (_) { this.message = '自主诊断授权未确认'; }
    finally { this.busy = false; }
  }
  private async sync(): Promise<void> {
    if (this.busy || !this.foreground || this.maintenance === null || this.self === null) { return; }
    this.busy = true;
    let state: string = 'ok';
    try {
      try { await this.maintenance.syncForeground(); }
      catch (_) { state = 'unconfirmed'; this.message = '同步未确认；保留旧版本与原下载'; }
      const status: UpdateStatus = await this.maintenance.status();
      const result: MaintenanceStatus = await this.self.runOnce({ diagnostic: 'signed_data_sync', state: state,
        data_version: status.activeVersion, pending_receipts: Math.min(256, status.unknown) });
      this.selfEnabled = result.enabled;
      this.cognitionLabel = !result.enabled ? '自主诊断已关闭' : result.execution === 'healthy' ? '本机监督未发现待处理故障' :
        result.execution === 'not_configured' ? '本机监督已允许，认知维护通道尚未配置' : '维护任务：' + result.execution + ' ' + result.task_status;
      if (result.repair_state !== undefined) { this.cognitionLabel += ' · 本机修复：' + result.repair_state; }
      await this.refresh();
    } catch (_) { this.message = '自检未确认；原事件和回执保留'; }
    finally { this.busy = false; }
  }
  private async decide(decision: string): Promise<void> {
    if (!this.foreground || this.pending === null || !this.pendingValid) { return; }
    const ticket: ApprovalTicket = this.pending;
    const current: PhoneControlPending | null = PhoneControlBridge.pendingForOwner(this.context());
    if (current === null || current.ticket.approval_id !== ticket.approval_id || current.ticket.nonce !== ticket.nonce) {
      this.pendingValid = false; this.message = '原请求已变更；没有批准其他请求'; return;
    }
    try {
      const value: OwnerControlDecision = { approval_id: ticket.approval_id, nonce: ticket.nonce, operation_id: ticket.operation_id,
        task_id: ticket.task_id, revision: ticket.revision, expires_at: ticket.expires_at, decision: decision };
      await PhoneControlBridge.postDecision(this.context(), value);
      this.pendingValid = false; this.message = '已记录本人的决定；是否执行以原任务回执为准';
    } catch (_) { this.message = '原决定未确认；没有自动重试'; }
  }
  private async reviewed(): Promise<void> {
    if (!this.foreground || this.pending === null || !this.pendingValid) { return; }
    try {
      await PhoneControlBridge.markPendingReviewed(this.context(), this.pending.approval_id,
        this.pending.nonce, this.pendingBody);
      this.message = '仅记录完整内容已核对。返回原应用后，再从通知或实况窗明确允许这一步';
    } catch (_) { this.message = '原内容或请求已变更，没有记录其他内容为已核对'; }
  }
  private async allowPreviousRollback(): Promise<void> {
    if (!this.foreground || !this.selfEnabled || this.self === null || this.busy) { return; }
    this.busy = true;
    try {
      const db: UpdateDB = await UpdateDB.open(this.context());
      let version: number = 0;
      try { version = (await db.state()).previous; } finally { await db.close(); }
      if (version <= 0) { this.message = '尚无已保留的上一数据版本'; return; }
      const policy: LocalRepairPolicy = this.self.getRepairPolicy();
      policy.rollback_versions = [version]; await this.self.setRepairPolicy(policy);
      this.message = '仅允许维护计划在完整验签后回退到数据版本 ' + version + '，不允许回退原生应用';
    } catch (_) { this.message = '回退授权未确认；原版本和原任务保留'; }
    finally { this.busy = false; }
  }
  private async nativeUpdate(distribution: boolean): Promise<void> {
    if (this.busy || !this.foreground || this.upgrade === null || this.maintenance === null) { return; }
    this.busy = true;
    try {
      if (distribution) {
        const path: string = await this.maintenance.resolveDataPath('config/native-distribution.json');
        const file: fileIo.File = fileIo.openSync(path, fileIo.OpenMode.READ_ONLY);
        let raw: string = '';
        try {
          const size: number = fileIo.statSync(file.fd).size;
          if (size <= 0 || size > 4096) { throw new Error('native_distribution_config_budget'); }
          const bytes: ArrayBuffer = new ArrayBuffer(size);
          if (fileIo.readSync(file.fd, bytes) !== size) { throw new Error('native_distribution_config_incomplete'); }
          raw = new util.TextDecoder('utf-8', { fatal: true }).decodeToString(new Uint8Array(bytes));
        } finally { fileIo.closeSync(file); }
        await this.upgrade.openOwnerDistributionLink(raw);
        this.message = '已请求系统打开可信分发页；安装仍需系统确认';
      } else {
        const status = await this.upgrade.checkAppGallery();
        if (status.updateAvailable) { await this.upgrade.showAppGalleryDialog(); this.message = '请按系统更新提示操作'; }
        else { this.message = '应用市场尚未报告更新；调试包无线升级仍待实际分发'; }
      }
    } catch (_) { this.message = distribution ? '尚无可用的可信无线分发配置；原应用保留' : '应用市场升级通道尚不可用；原应用保留'; }
    finally { this.busy = false; }
  }
  build() {
    Column({ space: 12 }) {
      SelfHdcPanel({ foreground: this.foreground })
      Divider()
      Text('系统辅助服务与审批').fontSize(18).fontWeight(FontWeight.Bold)
      Text(this.controlLabel).fontSize(13)
      Text(this.systemServiceProbe).fontSize(12)
      Row({ space: 12 }) {
        Toggle({ type: ToggleType.Switch, isOn: this.controlEnabled }).enabled(this.memberReady && !this.busy)
          .onChange((value: boolean): void => { if (value !== this.controlEnabled) { this.control(value); } })
        Text('允许 AMITA 协助观察和操作手机').fontSize(13)
      }
      Text('此设备尚未找到第三方辅助服务的启用入口。上方本机调试可独立连接；辅助服务未实际运行时，相关远程请求排队。').fontSize(12)
      Row({ space: 8 }) {
        Button(this.captureEnabled ? '截图已授权' : '允许单次截图').enabled(this.memberReady && !this.busy).onClick((): void => { this.allowCapture(); })
        Button('允许审批通知').enabled(this.memberReady && !this.busy).onClick((): void => { this.notification(); })
      }
      Text('辅助服务运行后的动作审批优先实况窗，未获权益时使用通知。服务未启用时音量键审批也不可用。输入文字需查看完整内容。').fontSize(12)
      if (this.pendingSummary.length > 0) {
        Text('待本人决定：' + this.pendingSummary).fontSize(14)
        Text(this.pendingBody).fontSize(12)
        Button('已核对完整内容').enabled(this.pendingValid).onClick((): void => { this.reviewed(); })
        Row({ space: 8 }) {
          Button('拒绝').enabled(this.pendingValid).onClick((): void => { this.decide('deny'); })
          Button('刷新请求').onClick((): void => { this.pending = null; this.pendingSummary = ''; this.pendingBody = ''; this.refresh(); })
        }
      }
      Divider()
      Text('后台与自维护').fontSize(18).fontWeight(FontWeight.Bold)
      Row({ space: 12 }) {
        Toggle({ type: ToggleType.Switch, isOn: this.dataEnabled }).enabled(this.memberReady && !this.busy)
          .onChange((value: boolean): void => { if (value !== this.dataEnabled) { this.data(value); } })
        Text('允许系统择机更新配置、技能和文件').fontSize(13)
      }
      Text(this.dataLabel).fontSize(12)
      Text(this.maintenanceLabel).fontSize(12)
      Row({ space: 12 }) {
        Toggle({ type: ToggleType.Switch, isOn: this.selfEnabled }).enabled(this.memberReady && !this.busy)
          .onChange((value: boolean): void => { if (value !== this.selfEnabled) { this.enableSelf(value); } })
        Text('允许自主诊断与 Mesh 维护任务').fontSize(13)
      }
      Text(this.cognitionLabel).fontSize(12)
      Button('允许必要时回退当前上一数据版本').enabled(this.memberReady && this.selfEnabled && !this.busy)
        .onClick((): void => { this.allowPreviousRollback(); })
      Button('同步与自检').enabled(this.memberReady && !this.busy).onClick((): void => { this.sync(); })
      Divider()
      Text('应用升级 · ' + this.versionLabel).fontSize(18).fontWeight(FontWeight.Bold)
      Text('配置和技能可签名更新。原生应用升级需要系统确认，分发通道以实际检查结果为准。').fontSize(12)
      Row({ space: 8 }) {
        Button('可信无线分发').enabled(this.memberReady && !this.busy).onClick((): void => { this.nativeUpdate(true); })
        Button('应用市场更新').enabled(!this.busy).onClick((): void => { this.nativeUpdate(false); })
      }
      if (this.message.length > 0) { Text(this.message).fontSize(13) }
    }.width('100%').alignItems(HorizontalAlign.Start).padding(12)
  }
}
