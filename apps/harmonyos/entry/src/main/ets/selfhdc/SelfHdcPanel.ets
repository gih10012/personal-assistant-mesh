import { common } from '@kit.AbilityKit';
import { SelfHdcController, SelfHdcViewState } from './SelfHdcController';

// Optional owner panel. Construction never starts a connection or command.
@Component
export struct SelfHdcPanel {
  @Prop @Watch('foregroundChanged') foreground: boolean = false;
  @State port: string = '';
  @State enabled: boolean = false;
  @State busy: boolean = false;
  @State authenticated: boolean = false;
  @State message: string = '本机调试默认关闭';
  @State identity: string = '';
  @State layout: string = '';
  @State imageUri: string = '';
  private controller: SelfHdcController | null = null;

  aboutToAppear(): void {
    const context: common.Context = this.getUIContext().getHostContext() as common.Context;
    this.controller = new SelfHdcController(context);
    this.controller.setForeground(this.foreground);
  }

  aboutToDisappear(): void {
    this.enabled = false;
    this.port = '';
    if (this.controller !== null) { this.controller.setForeground(false); this.apply(this.controller.snapshot()); }
  }

  foregroundChanged(): void {
    if (!this.foreground) { this.enabled = false; this.port = ''; }
    if (this.controller !== null) { this.controller.setForeground(this.foreground); this.apply(this.controller.snapshot()); }
  }

  private apply(value: SelfHdcViewState): void {
    this.busy = value.busy; this.authenticated = value.authenticated;
    this.message = value.message; this.identity = value.identity;
    this.layout = value.layout; this.imageUri = value.imageUri;
  }

  private toggle(value: boolean): void {
    this.enabled = value && this.foreground;
    if (!this.enabled && this.controller !== null) {
      this.controller.disconnect(); this.apply(this.controller.snapshot());
    }
    if (this.enabled) { this.message = '已允许本次前台手动探测；请输入端口后连接'; }
  }

  private async connect(): Promise<void> {
    if (!this.foreground || !this.enabled || this.busy || this.controller === null) { return; }
    this.busy = true;
    this.message = '正在连接本机；如出现系统信任提示，请由本人决定';
    try { this.apply(await this.controller.connect(this.port)); }
    catch (_) { this.message = '上一操作尚未结束，或应用已离开前台'; this.apply(this.controller.snapshot()); }
  }

  private async probe(kind: string): Promise<void> {
    if (!this.foreground || !this.enabled || !this.authenticated || this.busy || this.controller === null) { return; }
    this.busy = true;
    this.message = '正在进行一次只读探测';
    try { this.apply(await this.controller.probe(kind)); }
    catch (_) { this.message = '探测未确认；返回前台后可手动重连'; this.apply(this.controller.snapshot()); }
  }

  private disconnect(): void {
    if (this.controller !== null) { this.controller.disconnect(); this.apply(this.controller.snapshot()); }
  }

  build() {
    Column({ space: 10 }) {
      Text('本机调试连接').fontSize(18).fontWeight(FontWeight.Bold)
      Text('无需 USB 初始化。在系统开发者选项打开“无线调试”，填写页面显示的端口，并由本人确认系统信任提示。').fontSize(12)
      Text('AMITA 只连接本机。系统无线调试仍可能监听局域网；此功能不提供局域网隔离。请勿在不可信网络开启，用完请关闭系统无线调试。').fontSize(12)
      Row({ space: 10 }) {
        Toggle({ type: ToggleType.Switch, isOn: this.enabled }).enabled(this.foreground)
          .onChange((value: boolean): void => { if (value !== this.enabled) { this.toggle(value); } })
        Text('允许本次前台手动探测').fontSize(13)
      }
      TextInput({ text: this.port, placeholder: '无线调试端口' }).type(InputType.Number).maxLength(5)
        .enabled(this.enabled && this.foreground && !this.busy && !this.authenticated)
        .onChange((value: string): void => { this.port = value; })
      Row({ space: 8 }) {
        Button('连接本机').enabled(this.enabled && this.foreground && !this.busy && !this.authenticated)
          .onClick((): void => { this.connect(); })
        Button('断开').enabled(this.enabled || this.busy || this.authenticated)
          .onClick((): void => { this.disconnect(); })
      }
      Text(this.message).fontSize(13)
      Row({ space: 8 }) {
        Button('读取身份').enabled(this.authenticated && this.enabled && this.foreground && !this.busy)
          .onClick((): void => { this.probe('identity'); })
        Button('读取界面树').enabled(this.authenticated && this.enabled && this.foreground && !this.busy)
          .onClick((): void => { this.probe('layout'); })
      }
      Button('拍一张实际截图').enabled(this.authenticated && this.enabled && this.foreground && !this.busy)
        .onClick((): void => { this.probe('screenshot'); })
      if (this.identity.length > 0) { Text(this.identity).fontSize(12) }
      if (this.layout.length > 0) {
        Text('本次界面树 · 仅在本机显示').fontSize(12)
        TextArea({ text: this.layout }).height(180).enabled(false)
      }
      if (this.imageUri.length > 0) { Image(this.imageUri).width('100%').height(280).objectFit(ImageFit.Contain) }
      Text('连接认证通过不代表 GUI 操作已获许可。此处只做手动观察，不接入远程动作；离开前台会断开，返回后需手动连接。').fontSize(12)
    }.width('100%').alignItems(HorizontalAlign.Start).padding(12)
  }
}
