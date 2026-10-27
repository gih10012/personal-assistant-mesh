import { common } from '@kit.AbilityKit';
import { NativeServices } from '../platform/NativeServices';
import { WorkClient, WorkStatus, WorkTaskStatus } from '../platform/WorkClient';
import { MaintenanceService } from '../update/MaintenanceService';

@Entry
@Component
struct Index {
  @StorageLink('meshForeground') @Watch('onVisibilityChanged') foreground: boolean = false;
  @StorageLink('meshUpdateStatus') updateStatus: string = '尚未配置维护';
  @State nodeId: string = '正在读取应用私有身份';
  @State status: string = '未配置；未注册 Mesh';
  @State enrollment: string = '';
  @State endpoint: string = '';
  @State token: string = '';
  @State challenge: string = '';
  @State relayConfig: string = '';
  @State operationId: string = '';
  @State output: string = '';
  @State share: boolean = false;
  @State ready: boolean = false;
  @State workBootstrap: string = '';
  @State workStatus: string = '未配置持续出站任务连接';
  @State workTasks: string = '';
  @State updateConfig: string = '';
  @State maintenanceEnabled: boolean = false;
  private native: NativeServices | null = null;
  private work: WorkClient | null = null;
  private maintenance: MaintenanceService | null = null;

  aboutToAppear(): void { this.initialize(); }
  private async initialize(): Promise<void> {
    try {
      const context: common.UIAbilityContext = this.getUIContext().getHostContext() as common.UIAbilityContext;
      this.native = new NativeServices(context);
      await this.native.initialize();
      this.native.setForeground(this.foreground);
      this.nodeId = this.native.snapshot.node_id;
      this.enrollment = this.native.snapshot.enrollment_json;
      this.work = new WorkClient(context, this.native, (value: string): void => { this.showWorkStatus(value); });
      await this.work.initialize();
      this.work.setForeground(this.foreground);
      this.maintenance = new MaintenanceService(context);
      const maintenanceStatus = await this.maintenance.status();
      this.maintenanceEnabled = maintenanceStatus.enabled;
      AppStorage.setOrCreate('meshUpdateStatus', JSON.stringify(maintenanceStatus));
      this.ready = true;
      this.status = '身份已恢复；仅前台交互节点；未注册';
    } catch (error) { this.status = '私有状态不可用，保留原记录；不自动初始化替代身份'; }
  }
  private showWorkStatus(raw: string): void {
    const value: WorkStatus = JSON.parse(raw) as WorkStatus;
    this.workStatus = '连接：' + value.connection + ' · 完成 ' + value.settled + '/' + value.tasks +
      ' · 待核对 ' + value.unknown + ' · 待回传 ' + value.pending_results;
    this.workTasks = value.task_states.map((row: WorkTaskStatus): string =>
      row.task_id + ' · ' + row.state + ' · 原任务期次 ' + row.task_epoch +
      ' · 租约截至 ' + new Date(row.lease_deadline * 1000).toLocaleString()).join('\n');
  }
  async maintain(action: string): Promise<void> {
    try {
      const service: MaintenanceService = this.maintenance as MaintenanceService;
      if (action === 'configure') { await service.configureTrusted(this.updateConfig, this.nodeId); }
      else if (action === 'sync') { await service.syncForeground(); }
      else if (action === 'rollback') { await service.rollback(); }
      else { await service.setEnabled(this.maintenanceEnabled); }
      AppStorage.setOrCreate('meshUpdateStatus', JSON.stringify(await service.status()));
      this.status = '维护状态已更新；大文件与系统后台回调按实际回执显示';
    } catch (error) { this.status = '维护未确认；保留旧版本与原下载记录'; }
  }
  aboutToDisappear(): void {
    if (this.work !== null) { this.work.close(); }
  }
  onVisibilityChanged(): void {
    if (this.native !== null) { this.native.setForeground(this.foreground); }
    if (this.work !== null) { this.work.setForeground(this.foreground); }
    if (!this.foreground) { this.share = false; this.token = ''; this.status = '后台/熄屏暂停 Mesh 新调用；原请求与未知状态保留'; }
  }
  private setSharing(value: boolean): void {
    this.share = value;
    if (this.native !== null) { this.native.setSharing(value); }
    if (this.work !== null) { this.work.refresh(); }
  }
  async bindWork(): Promise<void> {
    try {
      await (this.work as WorkClient).bindBootstrap(this.workBootstrap);
      this.status = '持续出站连接已配置；原节点凭据存入Asset，前台按本人授权领取原分配';
    } catch (error) { this.status = '持续连接配置未确认；原身份、任务、unknown和结果均保留'; }
    finally { this.workBootstrap = ''; }
  }
  async bind(): Promise<void> {
    try { await (this.native as NativeServices).bindEnrollment(this.enrollment); this.status = '原入网身份已绑定；待路径验证与独立挑战'; }
    catch (error) { this.status = '身份/原绑定不匹配；未替换或清除旧记录'; }
  }
  async connect(): Promise<void> {
    try { await (this.native as NativeServices).connect(this.endpoint, this.token); this.status = '原 peer/authority 路径验证通过；不等于已注册/已授权'; }
    catch (error) { this.status = '连接或原 peer 验证失败；未改路、未重放旧请求'; }
    finally { this.token = ''; }
  }
  async choose(): Promise<void> {
    this.setSharing(false);
    try { await (this.native as NativeServices).chooseDocument(); this.share = false; this.status = '选择完成/取消；仍需本人允许本次文件能力'; }
    catch (error) { this.status = '文件选择不可用；不请求全盘/照片/联系人权限'; }
  }
  async execute(): Promise<void> {
    try { this.output = await (this.native as NativeServices).executeApprovedChallenge(this.challenge); this.status = '完整 provider-reported 成果已保存，等待原 Mesh 独立核验'; }
    catch (error) { this.status = '未完成；按原 operation ID 查询，禁止换 ID 自动重做'; }
  }
  async bindRelay(): Promise<void> {
    try {
      this.operationId = await (this.native as NativeServices).bindRelay(this.relayConfig);
      this.status = '原 relay/任务/epoch/nonce 已绑定；没有清除旧 unknown；凭据另填且只在内存';
    } catch (error) { this.status = '原 relay 配置不匹配；未换 URL、身份或清除旧记录'; }
  }
  async runRelay(): Promise<void> {
    try {
      this.output = await (this.native as NativeServices).runRelay(this.operationId, this.token);
      this.status = '原 relay 动作完成；仅明确许可才执行，完整结果保留；状态见下方，不等于已注册';
    } catch (error) { this.status = '原请求未确认或权限不可用；本地成果保留，后续只查原状态，不换 ID 重做'; }
    finally { this.token = ''; }
  }
  async submitRelay(): Promise<void> {
    try {
      this.output = await (this.native as NativeServices).submitRelayOriginalResult(this.operationId, this.token);
      this.status = '原结果首次回传/状态查回完成；结果 POST 若曾 attempted/unknown，本次只读查状态';
    } catch (error) { this.status = '回传未确认；完整本地结果仍可查，只查原状态，不自动重复上传'; }
    finally { this.token = ''; }
  }
  async queryRelay(): Promise<void> {
    try {
      this.output = await (this.native as NativeServices).queryRelayStatus(this.operationId, this.token);
      this.status = '远端原状态已查回；哈希一致才确认接收，不将本地 unknown 重新执行';
    } catch (error) { this.status = '原状态暂不可查；未执行、重传或改投其他入口；本地结果仍可读'; }
    finally { this.token = ''; }
  }
  query(): void {
    try { this.output = (this.native as NativeServices).originalResult(this.operationId); }
    catch (error) { this.status = '原记录未找到，不代表未执行；没有重试'; }
  }
  inventory(): void {
    try { this.output = JSON.stringify((this.native as NativeServices).adaptationReport()); }
    catch (error) { this.status = '先导入本人已授权的原 enrollment；本机不能自签登记'; }
  }
  dynamicState(): void {
    if (this.native !== null) { this.output = JSON.stringify(this.native.dynamicState()); }
  }
  build() {
    Scroll() {
      Column({ space: 12 }) {
        Text('Mesh · 鸿蒙原生节点').fontSize(25).fontWeight(FontWeight.Bold)
        Text(this.nodeId).fontSize(12).copyOption(CopyOptions.LocalDevice)
        Text(this.status).fontSize(14)
        Text('只管理 Mesh 额外共享接口，不限制手机原生功能。小艺/GUI 未接入；不承诺后台常驻。').fontSize(12)
        TextArea({ text: this.enrollment, placeholder: '导入原 mesh-enrollment/1（使用上方节点 ID、interactive 模式）' })
          .height(140).onChange((value: string) => {
            this.enrollment = value; this.setSharing(false);
          })
        Button('绑定原身份').enabled(this.ready && this.foreground).onClick(() => { this.bind(); })
        TextInput({ text: this.endpoint, placeholder: '本人授权的 HTTPS 加入入口（无自动公网回退）' })
          .onChange((value: string) => { this.endpoint = value; })
        TextInput({ text: this.token, placeholder: '仅本次动作的 scoped token（peer/单 operation relay 不混用），不落盘' }).type(InputType.Password)
          .onChange((value: string) => { this.token = value; })
        Button('核对原 peer（非注册）').enabled(this.ready && this.foreground).onClick(() => { this.connect(); })
        Text('持续出站任务').fontSize(18).fontWeight(FontWeight.Bold)
        Text('先完成原登记，再导入可信服务签发的手机配置。手机主动连接，不开放入站调试口。').fontSize(12)
        TextArea({ text: this.workBootstrap, placeholder: '私有 mesh-phone-bootstrap/1（含节点凭据，导入后立即清空）' })
          .height(120).onChange((value: string) => { this.workBootstrap = value; this.setSharing(false); })
        Button('保存节点配置并连接').enabled(this.ready && this.foreground).onClick(() => { this.bindWork(); })
        Text(this.workStatus).fontSize(12)
        Text(this.workTasks).fontSize(12)
        Text('在线状态与任务租约分别显示；unknown只核对原回执，不重做。关闭共享或离开前台立即撤回本机能力。').fontSize(12)
        Text('数据与技能维护').fontSize(18).fontWeight(FontWeight.Bold)
        TextArea({ text: this.updateConfig, placeholder: '可信维护配置：固定公钥、版本化manifest入口与本机身份' })
          .height(100).onChange((value: string) => { this.updateConfig = value; })
        Button('保存可信维护配置').enabled(this.ready && this.foreground).onClick(() => { this.maintain('configure'); })
        Row({ space: 10 }) {
          Toggle({ type: ToggleType.Switch, isOn: this.maintenanceEnabled }).onChange((value: boolean) => {
            this.maintenanceEnabled = value; this.maintain('schedule');
          })
          Text('允许系统择机同步应用数据').fontSize(12)
        }
        Row({ space: 10 }) {
          Button('同步数据与技能').enabled(this.ready && this.foreground).onClick(() => { this.maintain('sync'); })
          Button('回退上一数据版本').enabled(this.ready && this.foreground).onClick(() => { this.maintain('rollback'); })
        }
        Text(this.updateStatus).fontSize(12)
        Button('系统文件选择器：选择一个文件').enabled(this.ready && this.foreground).onClick(() => { this.choose(); })
        Row({ space: 12 }) {
          Toggle({ type: ToggleType.Switch, isOn: this.share }).onChange((value: boolean) => {
            this.setSharing(value);
          })
          Text('本人允许前台 Mesh 使用字节及本人选定文件能力').fontSize(12)
        }
        TextArea({ text: this.relayConfig, placeholder: '本人提供的 mesh-enrollment-relay-client/1：原 /enrollment/node URL + 完整 binding，不含 token' })
          .height(130).onChange((value: string) => {
            this.relayConfig = value; this.setSharing(false);
          })
        Button('绑定原远程任务（不重置记录）').enabled(this.ready && this.foreground).onClick(() => { this.bindRelay(); })
        TextInput({ text: this.operationId, placeholder: '原 operation ID：远程调度/只读查回/本地完整结果' })
          .onChange((value: string) => { this.operationId = value; })
        Button('远程领取 → 执行一次 → 完整回传').enabled(this.ready && this.foreground && this.share)
          .onClick(() => { this.runRelay(); })
        Text('领取/结果提交曾未确认时，上方动作只查原状态；不会重做。需要匹配原任务的可信服务端与单 operation token。').fontSize(12)
        Row({ space: 10 }) {
          Button('原结果首次回传').enabled(this.ready && this.foreground && this.share).onClick(() => { this.submitRelay(); })
          Button('远端原状态').enabled(this.ready && this.foreground).onClick(() => { this.queryRelay(); })
        }
        TextArea({ text: this.challenge, placeholder: '原 mesh-enrollment-challenge/1；仅字节 recipe 或选定文件完整只读（≤64KiB）' })
          .height(150).onChange((value: string) => { this.challenge = value; this.setSharing(false); })
        Row({ space: 10 }) {
          Button('执行一次').enabled(this.ready && this.foreground && this.share).onClick(() => { this.execute(); })
          Button('声明能力').enabled(this.ready).onClick(() => { this.inventory(); })
          Button('动态状态').enabled(this.ready).onClick(() => { this.dynamicState(); })
        }
        Button('原结果查回，不重做').enabled(this.ready).onClick(() => { this.query(); })
        TextArea({ text: this.output, placeholder: '私有完整结果；不要贴公共仓库/日志' }).height(220).enabled(false)
      }.padding(20).width('100%')
    }.height('100%').width('100%')
  }
}
