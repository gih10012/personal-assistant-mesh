import { common } from '@kit.AbilityKit';
import { fileIo } from '@kit.CoreFileKit';
import { util } from '@kit.ArkTS';
import { SelfHdcClient, SelfHdcStatus } from './SelfHdcClient';
import { SelfHdcDriver, SelfHdcCapture } from './SelfHdcDriver';

export interface SelfHdcViewState {
  state: string;
  authenticated: boolean;
  busy: boolean;
  message: string;
  identity: string;
  layout: string;
  imageUri: string;
}

function idleState(message: string): SelfHdcViewState {
  return { state: 'off', authenticated: false, busy: false, message: message,
    identity: '', layout: '', imageUri: '' };
}

export class SelfHdcController {
  private client: SelfHdcClient;
  private driver: SelfHdcDriver;
  private context: common.Context;
  private foreground: boolean = false;
  private epoch: number = 0;
  private busy: boolean = false;
  private view: SelfHdcViewState = idleState('尚未连接');
  private imagePath: string = '';

  constructor(context: common.Context) {
    this.context = context;
    this.client = new SelfHdcClient(context);
    this.driver = new SelfHdcDriver(this.client, (): boolean => this.foreground);
  }

  setForeground(value: boolean): void {
    this.foreground = value;
    if (!value) { this.disconnect('已离开前台，本机调试连接已关闭；返回后需手动连接'); }
  }

  private clearImage(): void {
    if (this.imagePath.length > 0) {
      try { fileIo.unlinkSync(this.imagePath); } catch (_) {}
      this.imagePath = '';
    }
  }

  disconnect(message: string = '本机连接已断开；系统无线调试开关需在设置中关闭'): void {
    this.epoch += 1;
    this.client.close();
    this.clearImage();
    // Keep busy true until the cancelled promise settles; an old operation
    // must not share the client with a newly connected session.
    this.view = idleState(message);
    this.view.busy = this.busy;
  }

  snapshot(): SelfHdcViewState {
    const status: SelfHdcStatus = this.client.status();
    if (!this.busy && this.view.authenticated && !status.authenticated) {
      this.view.authenticated = false;
      this.view.state = 'disconnected';
      this.view.message = '连接已断开，原探测结果不是当前在线证据；请手动重连';
    }
    return { state: this.view.state, authenticated: this.view.authenticated, busy: this.busy,
      message: this.view.message, identity: this.view.identity, layout: this.view.layout, imageUri: this.view.imageUri };
  }

  private begin(): number {
    if (!this.foreground) { throw new Error('selfhdc_foreground_required'); }
    if (this.busy) { throw new Error('selfhdc_probe_busy'); }
    this.busy = true;
    this.view.busy = true;
    return this.epoch;
  }

  private current(epoch: number): boolean { return this.foreground && this.epoch === epoch; }

  private failure(error: Error): string {
    // Never surface arbitrary transport exceptions or raw protocol contents.
    switch (error.message) {
      case 'selfhdc_command_failed_45': return '系统未提供 base64，当前截图读取方式不可用';
      case 'selfhdc_command_failed_43': return '探测文件为空或超过大小限制；没有读取或截断为成功结果';
      case 'selfhdc_command_failed_41': return '系统截图或界面树命令未成功；没有验证该能力';
      case 'selfhdc_command_failed_42': return '系统未能读取本次临时文件；没有验证该能力';
      case 'selfhdc_command_failed_44': return '系统未能完整输出本次探测文件';
      case 'selfhdc_execution_unknown': return '命令已发出但完成情况未知；没有自动重试';
      case 'selfhdc_not_dispatched': return '命令未发出；请核对连接后手动操作';
      case 'selfhdc_exit_status_unknown': return '命令结束码未确认，不能视为探测成功';
      case 'selfhdc_probe_busy': return '上一操作尚未结束';
      case 'selfhdc_foreground_required': return '请返回 AMITA 前台后手动连接';
      case 'selfhdc_foreground_connection_required': return '本机已断开或离开前台，探测未确认';
      case 'selfhdc_capture_encoding_invalid': return '截图数据不完整或编码无效；未显示为成功截图';
      case 'selfhdc_capture_format_unconfirmed': return '系统截图格式未确认；未显示为成功截图';
      case 'selfhdc_layout_unconfirmed': return '界面树内容未确认；没有验证界面访问';
      case 'selfhdc_identity_unconfirmed': return '本机 shell 身份返回未确认';
      default: return '本次操作未确认；可核对系统调试开关和信任提示后手动重连';
    }
  }

  private finish(): void { this.busy = false; this.view.busy = false; }

  async connect(portText: string): Promise<SelfHdcViewState> {
    const epoch: number = this.begin();
    try {
      if (!/^[0-9]{1,5}$/.test(portText) || Number(portText) < 1 || Number(portText) > 65535) {
        throw new Error('selfhdc_port_invalid');
      }
      this.view.state = 'connecting';
      this.view.message = '正在连接本机；如出现系统信任提示，请由本人决定';
      const status: SelfHdcStatus = await this.client.connect(Number(portText), 90000);
      if (this.current(epoch)) {
        this.view.authenticated = status.authenticated;
        this.view.state = status.authenticated ? 'authenticated' : 'unconfirmed';
        this.view.message = status.authenticated ? '本机调试认证已通过；尚未验证界面观察、截图或动作权限' :
          '本机调试尚未认证；请核对端口、无线调试开关和系统信任提示';
      }
    } catch (error) {
      if (this.current(epoch)) {
        this.view.state = 'unconfirmed'; this.view.authenticated = false;
        this.view.message = (error as Error).message === 'selfhdc_port_invalid' ?
          '请输入系统无线调试页面显示的 1–65535 端口号' : this.failure(error as Error);
      }
    } finally { this.finish(); }
    return this.snapshot();
  }

  async probe(kind: string): Promise<SelfHdcViewState> {
    const epoch: number = this.begin();
    try {
      this.view.message = '正在进行一次本人触发的只读探测';
      if (kind === 'identity') {
        const value: string = await this.driver.identity();
        if (this.current(epoch)) { this.view.identity = value; this.view.message = '已读取本机实际 shell 身份'; }
      } else if (kind === 'layout') {
        const value: string = await this.driver.layout();
        if (this.current(epoch)) {
          this.view.layout = value;
          this.view.message = '已读取一次完整界面树，仅在本机展示；不代表持续观察可用';
        }
      } else if (kind === 'screenshot') {
        const capture: SelfHdcCapture = await this.driver.screenshot();
        if (this.current(epoch)) {
          this.clearImage();
          const path: string = this.context.cacheDir + '/selfhdc_' + util.generateRandomUUID() + '.jpeg';
          const file: fileIo.File = fileIo.openSync(path,
            fileIo.OpenMode.CREATE | fileIo.OpenMode.WRITE_ONLY | fileIo.OpenMode.TRUNC);
          this.imagePath = path;
          try {
            const bytes: ArrayBuffer = new ArrayBuffer(capture.bytes.length);
            new Uint8Array(bytes).set(capture.bytes);
            if (fileIo.writeSync(file.fd, bytes) !== capture.bytes.length) { throw new Error('selfhdc_image_write_incomplete'); }
          } finally { fileIo.closeSync(file); }
          this.view.imageUri = 'file://' + path;
          this.view.message = '已取得一张实际截图，仅在本机预览；离开前台即清除';
        }
      } else { throw new Error('selfhdc_probe_invalid'); }
    } catch (error) {
      if (this.current(epoch)) {
        if (kind === 'screenshot') { this.clearImage(); this.view.imageUri = ''; }
        if (kind === 'layout') { this.view.layout = ''; }
        if (kind === 'identity') { this.view.identity = ''; }
        this.view.message = this.failure(error as Error);
      }
    } finally { this.finish(); }
    return this.snapshot();
  }
}
