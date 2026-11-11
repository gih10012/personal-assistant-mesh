import { common } from '@kit.AbilityKit';
import { fileIo } from '@kit.CoreFileKit';
import { util } from '@kit.ArkTS';
import { preferences } from '@kit.ArkData';
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
  diagnosticStage: string;
  diagnosticCode: string;
  portRemembered: boolean;
}

export interface SelfHdcDiagnostic { stage: string; code: string; title: string; }

export function selfHdcPort(value: string): string {
  return /^[0-9]{1,5}$/.test(value) && Number(value) >= 1 && Number(value) <= 65535 ? String(Number(value)) : '';
}

export function selfHdcStage(stage?: string): string {
  switch (stage) {
    case 'identity': return '本机身份初始化';
    case 'socket_bind': return '绑定本机回环地址';
    case 'socket_connect': return '连接本机调试端口';
    case 'handshake': return '调试协议握手';
    case 'awaiting_trust': return '系统信任授权';
    case 'ready': return '已认证会话';
    case 'command': return '命令执行';
    default: return '';
  }
}

export function selfHdcDiagnostic(code: string): SelfHdcDiagnostic {
  switch (code) {
    case 'selfhdc_port_invalid':
      return { stage: '端口配置', code: code, title: '请输入设置页面显示的 1–65535 端口号' };
    case 'selfhdc_socket_error':
    case 'selfhdc_connection_or_identity_failed':
      return { stage: '本机连接或身份初始化', code: code,
        title: '本机连接未建立；请核对无线调试开关和当前端口，系统端口可能已变化' };
    case 'selfhdc_authorization_timeout':
      return { stage: '连接及信任授权期限', code: code,
        title: '连接或授权等待超时；请核对端口与系统信任提示后手动连接' };
    case 'selfhdc_authorization_rejected':
    case 'selfhdc_daemon_rejected':
    case 'selfhdc_not_authorized':
      return { stage: '系统信任授权', code: code, title: '系统未批准本机调试，请由本人核对信任提示' };
    case 'selfhdc_tls_unsupported':
    case 'selfhdc_auth_algorithm':
    case 'selfhdc_auth_unsupported':
      return { stage: '认证协议', code: code, title: '当前系统要求的认证方式暂不受支持' };
    case 'selfhdc_session_expired':
      return { stage: '会话期限', code: code, title: '本次会话已到期；端口已保留，请手动重新连接' };
    case 'selfhdc_disconnected':
    case 'selfhdc_not_connected':
    case 'selfhdc_session_closed':
      return { stage: '连接中断', code: code, title: '本机连接已断开；请核对当前端口后手动重新连接' };
    case 'selfhdc_command_timeout_unknown':
    case 'selfhdc_command_transport_unknown':
    case 'selfhdc_command_outcome_unknown':
      return { stage: '命令完成确认', code: code, title: '命令完成情况未知；没有自动重试' };
    case 'selfhdc_output_limit':
    case 'selfhdc_receive_queue_limit':
      return { stage: '返回数据限制', code: code, title: '返回数据超过限制，本次结果未确认' };
    case 'selfhdc_session_busy':
    case 'selfhdc_not_ready_or_busy':
      return { stage: '会话状态', code: code, title: '上一操作尚未结束或连接尚未就绪' };
    case 'selfhdc_random_failed':
    case 'selfhdc_identity_context_changed':
    case 'selfhdc_identity_invalid':
    case 'selfhdc_identity_read':
    case 'selfhdc_identity_size':
    case 'selfhdc_identity_write':
      return { stage: '本机身份', code: code, title: '本机调试身份暂不可用，未更换原身份' };
    case 'selfhdc_auth_session':
    case 'selfhdc_channel_unexpected':
    case 'selfhdc_close_invalid':
    case 'selfhdc_echo_empty':
    case 'selfhdc_protocol_error':
    case 'selfhdc_public_key_sequence':
    case 'selfhdc_signature_sequence':
    case 'selfhdc_unexpected_handshake':
    case 'selfhdc_command_unsupported':
    case 'selfhdc_output_unexpected':
    case 'selfhdc_handshake_duplicate':
    case 'selfhdc_handshake_field':
    case 'selfhdc_handshake_rejected':
    case 'selfhdc_handshake_size':
    case 'selfhdc_packet_checksum':
    case 'selfhdc_packet_header':
    case 'selfhdc_packet_protect':
    case 'selfhdc_packet_size':
    case 'selfhdc_receive_limit':
    case 'selfhdc_tlv_invalid':
    case 'selfhdc_tlv_length':
    case 'selfhdc_tlv_tag':
    case 'selfhdc_tlv_truncated':
    case 'selfhdc_wire_fields':
    case 'selfhdc_wire_integer':
    case 'selfhdc_wire_length':
    case 'selfhdc_wire_overflow':
    case 'selfhdc_wire_tag':
    case 'selfhdc_wire_truncated':
    case 'selfhdc_wire_varint':
      return { stage: '调试协议', code: code, title: '系统调试协议返回未通过核对，本次连接未确认' };
    default:
      return { stage: '本机调试', code: 'selfhdc_unknown_failure', title: '本次操作未确认，请核对设置后手动连接' };
  }
}

function idleState(message: string): SelfHdcViewState {
  return { state: 'off', authenticated: false, busy: false, message: message,
    identity: '', layout: '', imageUri: '', diagnosticStage: '', diagnosticCode: '', portRemembered: false };
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
  private lastPort: string = '';
  private portRemembered: boolean = false;

  constructor(context: common.Context) {
    this.context = context;
    this.client = new SelfHdcClient(context);
    this.driver = new SelfHdcDriver(this.client, (): boolean => this.foreground);
    try {
      const store: preferences.Preferences = preferences.getPreferencesSync(context, { name: 'amita_selfhdc_ui_v1' });
      const value: preferences.ValueType = store.getSync('last_port', '');
      if (typeof value === 'string') { this.lastPort = selfHdcPort(value); }
      this.portRemembered = this.lastPort.length > 0;
    } catch (_) { /* A failed nonsecret preference read must not enable a connection. */ }
  }

  savedPort(): string { return this.lastPort; }

  private async rememberPort(value: string): Promise<void> {
    this.lastPort = value;
    this.portRemembered = false;
    try {
      const store: preferences.Preferences = preferences.getPreferencesSync(this.context, { name: 'amita_selfhdc_ui_v1' });
      store.putSync('last_port', value);
      await store.flush();
      this.portRemembered = true;
    } catch (_) { /* Keep the entered port in memory, without claiming it was saved. */ }
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
    if (this.busy && this.view.state === 'connecting') {
      const stage: string = selfHdcStage(status.stage);
      if (stage.length > 0) { this.view.diagnosticStage = stage; }
      if (status.state === 'awaiting_trust') { this.view.message = '等待本人确认系统信任提示'; }
    }
    if (!this.busy && this.view.authenticated && !status.authenticated) {
      this.clearImage();
      this.view = idleState('连接已断开，请手动重新连接');
      this.view.state = status.error === 'selfhdc_session_expired' ? 'expired' : 'disconnected';
      this.showDiagnostic(status.error, status);
    }
    return { state: this.view.state, authenticated: this.view.authenticated, busy: this.busy,
      message: this.view.message, identity: this.view.identity, layout: this.view.layout, imageUri: this.view.imageUri,
      diagnosticStage: this.view.diagnosticStage, diagnosticCode: this.view.diagnosticCode,
      portRemembered: this.portRemembered };
  }

  private showDiagnostic(code: string, status?: SelfHdcStatus): void {
    const diagnostic: SelfHdcDiagnostic = selfHdcDiagnostic(code);
    this.view.message = diagnostic.title;
    const stage: string = status === undefined ? '' : selfHdcStage(status.stage);
    this.view.diagnosticStage = stage.length > 0 ? stage : diagnostic.stage;
    this.view.diagnosticCode = diagnostic.code;
    if (status !== undefined && status.nativeCode !== undefined &&
      Number.isSafeInteger(status.nativeCode) && status.nativeCode > 0 && status.nativeCode <= 99999999) {
      this.view.diagnosticCode += ' · SDK ' + status.nativeCode;
    }
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
      const port: string = selfHdcPort(portText);
      if (port.length === 0) {
        throw new Error('selfhdc_port_invalid');
      }
      this.clearImage();
      this.view = idleState('正在连接本机；如出现系统信任提示，请由本人决定');
      this.view.state = 'connecting';
      this.view.diagnosticStage = '本机连接及身份初始化';
      await this.rememberPort(port);
      if (!this.current(epoch)) { return this.snapshotAfterFinish(); }
      const status: SelfHdcStatus = await this.client.connect(Number(port), 90000);
      if (this.current(epoch)) {
        this.view.authenticated = status.authenticated;
        this.view.state = status.authenticated ? 'authenticated' : 'unconfirmed';
        if (status.authenticated) {
          this.view.message = '本机调试认证已通过；可手动读取身份、界面树或截图';
          this.view.diagnosticStage = ''; this.view.diagnosticCode = '';
        } else { this.showDiagnostic(status.error, status); }
      }
    } catch (error) {
      if (this.current(epoch)) {
        this.view.state = 'unconfirmed'; this.view.authenticated = false;
        const code: string = (error as Error).message;
        const status: SelfHdcStatus = this.client.status();
        if (code === 'selfhdc_port_invalid') { this.showDiagnostic(code); }
        else { this.showDiagnostic(status.error.length > 0 ? status.error : code, status); }
      }
    } finally { this.finish(); }
    return this.snapshot();
  }

  private snapshotAfterFinish(): SelfHdcViewState {
    this.finish();
    return this.snapshot();
  }

  async probe(kind: string): Promise<SelfHdcViewState> {
    const epoch: number = this.begin();
    try {
      this.view.message = '正在进行一次本人触发的只读探测';
      this.view.diagnosticStage = ''; this.view.diagnosticCode = '';
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
        const status: SelfHdcStatus = this.client.status();
        if (status.error.length > 0) { this.showDiagnostic(status.error, status); }
      }
    } finally { this.finish(); }
    return this.snapshot();
  }
}
