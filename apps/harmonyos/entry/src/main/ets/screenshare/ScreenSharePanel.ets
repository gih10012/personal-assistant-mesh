import { ScreenShareRuntime, ScreenShareRuntimeStatus, ScreenSharePending } from './ScreenShareRuntime';

// Optional page presentation only. The UIAbility retains the runtime and its
// media lifecycle; hiding/destroying this panel never invents a foreground state.
@Component
export struct ScreenSharePanel {
  @Prop @Watch('visibilityChanged') foreground: boolean = false;
  @State receiving: boolean = false;
  @State sourceInstalled: boolean = false;
  @State pendingVisible: boolean = false;
  @State executing: boolean = false;
  @State captureActive: boolean = false;
  @State backgroundActive: boolean = false;
  @State acknowledgedFrames: number = 0;
  @State deciding: boolean = false;
  @State available: boolean = false;
  @State phase: string = '共享组件尚未就绪';
  @State message: string = '';
  @State task: string = '';
  @State purpose: string = '';
  @State duration: string = '';
  @State deadline: string = '';
  @State outcomes: string = '';
  private pendingRequest: ScreenSharePending | null = null;
  private timer: number = -1;

  aboutToAppear(): void { this.refresh(); this.startRefresh(); }
  aboutToDisappear(): void { this.stopRefresh(); this.pendingRequest = null; }
  visibilityChanged(): void {
    if (!this.foreground) { this.stopRefresh(); this.pendingRequest = null; }
    else { this.startRefresh(); }
    this.refresh();
  }
  private startRefresh(): void {
    if (!this.foreground || this.timer !== -1) { return; }
    this.timer = setInterval((): void => { this.refresh(); }, 1000);
  }
  private stopRefresh(): void { clearInterval(this.timer); this.timer = -1; }

  private describeError(code: string): string {
    if (code === '') { return ''; }
    if (code === 'screen_share_owner_denied') { return '已拒绝本次请求，没有开始共享'; }
    if (code === 'screen_share_owner_stopped') { return '已要求停止共享并关闭接收'; }
    if (code === 'screen_share_background_ticket_required') {
      return '离开前台时后台共享尚未就绪，本次授权已取消';
    }
    if (code === 'screen_share_source_presence_unconfirmed' || code === 'screen_share_configuration_or_source_unconfirmed') {
      return '来源配置或连接尚未确认，没有继续共享';
    }
    if (code === 'screen_share_local_approval_expired' || code === 'screen_share_local_decision_unconfirmed') {
      return '本次授权已过期或未确认，请等待新的原任务请求';
    }
    if (code === 'screen_share_original_stream_outcome_unknown') {
      return '原共享请求的结束回执未确认，不会自动重放';
    }
    return '本次共享已停止或授权已失效；可查看原任务回执';
  }

  private refresh(): void {
    const runtime = ScreenShareRuntime.current();
    this.available = runtime !== null;
    if (runtime === null) { this.phase = '成员入网后，共享组件才可用'; return; }
    const status: ScreenShareRuntimeStatus = runtime.status();
    this.receiving = status.receiving; this.sourceInstalled = status.sourceInstalled;
    this.executing = status.executing; this.captureActive = status.captureActive;
    this.backgroundActive = status.backgroundActive; this.acknowledgedFrames = status.acknowledgedFrames;
    const pending = this.foreground ? runtime.pending() : null;
    this.pendingRequest = pending; this.pendingVisible = pending !== null;
    if (pending !== null) {
      this.task = pending.taskId + ' · 任期 ' + String(pending.taskEpoch);
      this.purpose = pending.purpose;
      this.duration = '请求共享 ' + String(Math.ceil(pending.durationMs / 1000)) + ' 秒，最多 ' + String(pending.maxFps) + ' 帧/秒';
      this.deadline = '本次决定剩余约 ' + String(Math.max(0, Math.ceil((pending.expiresAt - Date.now()) / 1000))) +
        ' 秒；原任务租约剩余约 ' + String(Math.max(0, Math.ceil((pending.allocationDeadline - Date.now()) / 1000))) + ' 秒';
    }
    this.phase = status.cleanupUnknown ? '停止结果尚未确认，请通过系统录屏入口确认结束' :
      status.sourceCloseUnknown ? '来源关闭回执尚未确认，本次不会自动恢复' :
      status.captureActive ? (status.backgroundActive ? '只读屏幕共享中，可使用其他应用' : '画面已开始，正在确认后台共享') :
      pending !== null ? '原任务请求共享屏幕，等待本人决定' : status.executing ? '等待系统授权或原请求结束确认' :
      !status.receiving ? '只读屏幕共享已关闭' : !status.sourceInstalled ? '来源尚未安装本机共享适配' :
      status.presenceExpiresAt <= Date.now() ? '共享请求连接尚未确认' : '等待原任务的只读共享请求';
    this.message = this.describeError(status.error);
    this.outcomes = '原记录：已结算 ' + String(status.settledRecords) + '，待回执 ' + String(status.pendingResults) +
      '，未确认 ' + String(status.unknownRecords);
  }

  private async receive(): Promise<void> {
    const runtime = ScreenShareRuntime.current();
    if (!this.foreground || runtime === null || this.deciding) { return; }
    try { await runtime.receive(); }
    catch (_) { this.message = '接收请求尚未确认，没有开始录屏'; }
    this.refresh();
  }

  private async decide(approve: boolean): Promise<void> {
    const runtime = ScreenShareRuntime.current(); const pending = this.pendingRequest;
    if (!this.foreground || runtime === null || pending === null || this.deciding) { return; }
    this.deciding = true;
    try { await runtime.decide(pending, approve); }
    catch (_) { this.message = '本次决定未确认或已经失效，没有复用旧授权'; }
    finally { this.deciding = false; this.refresh(); }
  }

  private async stop(): Promise<void> {
    const runtime = ScreenShareRuntime.current();
    if (runtime === null) { return; }
    try { await runtime.stop(); }
    catch (_) { this.message = '停止或来源关闭回执尚未确认，不会自动恢复'; }
    this.refresh();
  }

  build() {
    Column({ space: 10 }) {
      Text('只读屏幕共享').fontSize(18).fontWeight(FontWeight.Bold)
      Text(this.phase).fontSize(14)
      Text('仅在本人逐次同意并通过系统录屏授权后共享。不会点击或输入。').fontSize(13)
      if (!this.receiving) {
        Button('接收只读共享请求').enabled(this.foreground && this.available && !this.deciding)
          .onClick((): void => { this.receive(); })
      }
      if (this.pendingVisible) {
        Text(this.task).fontSize(13).width('100%')
        Text(this.purpose).fontSize(14).width('100%')
        Text(this.duration).fontSize(13)
        Text(this.deadline).fontSize(12)
        Text('实际时长受原任务剩余租约限制，本组件不会续期原任务。').fontSize(12)
        Row({ space: 12 }) {
          Button('开始本次共享').enabled(this.foreground && !this.deciding)
            .onClick((): void => { this.decide(true); })
          Button('拒绝本次请求').enabled(this.foreground && !this.deciding)
            .onClick((): void => { this.decide(false); })
        }
      }
      if (this.captureActive || this.acknowledgedFrames > 0) {
        Text('已确认接收 ' + String(this.acknowledgedFrames) + ' 帧').fontSize(13)
      }
      if (this.receiving || this.executing || this.pendingVisible) {
        Button('停止共享并关闭接收').onClick((): void => { this.stop(); })
      }
      if (this.message.length > 0) { Text(this.message).fontSize(13) }
      Text(this.outcomes).fontSize(12)
      Text('只有画面和后台共享均确认后才可离开本应用；结束后可能需要回到前台补报原记录回执。').fontSize(12)
    }.width('100%').padding(12).alignItems(HorizontalAlign.Start)
  }
}
