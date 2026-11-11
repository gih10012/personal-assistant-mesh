// SPDX-License-Identifier: Apache-2.0
import { common } from '@kit.AbilityKit';
import { socket } from '@kit.NetworkKit';
import { BusinessError } from '@kit.BasicServicesKit';
import { SelfHdcIdentity } from './SelfHdcIdentity';
import { SelfHdcHandshake, SelfHdcPacket, SelfHdcPacketStream, HDC_CLOSE, HDC_ECHO, HDC_EXECUTE,
  HDC_HANDSHAKE, HDC_RAW, hdcDecodeHandshake, hdcDecodeTlv, hdcEncodeHandshake,
  hdcEncodePacket, hdcEncodeTlv, hdcJoin, hdcRequire, hdcShellCommand, hdcShellTail,
  hdcText, hdcUtf8 } from './SelfHdcProtocol';

export interface SelfHdcStatus { state: string; error: string; authenticated: boolean; }
export interface SelfHdcShellResult {
  stdout: Uint8Array;
  completed: boolean;
  dispatched: boolean;
  exitCode?: number;
}

class AuthWait {
  promise: Promise<void>;
  resolve: () => void = (): void => {};
  reject: (error: Error) => void = (_error: Error): void => {};
  constructor() {
    this.promise = new Promise<void>((resolve, reject): void => { this.resolve = resolve; this.reject = reject; });
    this.promise.catch((_error: Error): void => {});
  }
}
class ShellWait {
  promise: Promise<SelfHdcShellResult>;
  resolve: (result: SelfHdcShellResult) => void = (_result: SelfHdcShellResult): void => {};
  channel: number;
  marker: string;
  limit: number;
  dispatched: boolean = false;
  chunks: Uint8Array[] = [];
  length: number = 0;
  timer: number = -1;
  constructor(channel: number, marker: string, limit: number) {
    this.channel = channel; this.marker = marker; this.limit = limit;
    this.promise = new Promise<SelfHdcShellResult>((resolve): void => { this.resolve = resolve; });
  }
}

// One session and one command at a time. No listener, external address or PC helper.
export class SelfHdcClient {
  private context: common.Context;
  private current: SelfHdcStatus = { state: 'idle', error: '', authenticated: false };
  private tcp: socket.TCPSocket | undefined = undefined;
  private identity: SelfHdcIdentity | undefined = undefined;
  private stream: SelfHdcPacketStream = new SelfHdcPacketStream();
  private authentication: AuthWait | undefined = undefined;
  private active: ShellWait | undefined = undefined;
  private queue: Promise<void> = Promise.resolve();
  private queuedBytes: number = 0;
  private session: number = 0;
  private port: number = 0;
  private channel: number = 1;
  private generation: number = 0;
  private authTimer: number = -1;
  private sessionTimer: number = -1;
  private modern: boolean = false;
  private authSuccess: boolean = false;
  private closeAcknowledged: boolean = false;
  private signingCount: number = 0;
  private publicKeySent: boolean = false;
  private commandPreparing: boolean = false;

  constructor(context: common.Context) { this.context = context; }

  status(): SelfHdcStatus {
    return { state: this.current.state, error: this.current.error, authenticated: this.current.authenticated };
  }

  async connect(port: number, budgetMs: number = 90000): Promise<SelfHdcStatus> {
    hdcRequire(this.tcp === undefined && this.authentication === undefined && !this.commandPreparing,
      'selfhdc_session_busy');
    hdcRequire(Number.isInteger(port) && port >= 1 && port <= 65535, 'selfhdc_port_invalid');
    hdcRequire(Number.isInteger(budgetMs) && budgetMs >= 1000 && budgetMs <= 90000, 'selfhdc_budget_invalid');
    const generation = ++this.generation;
    this.port = port; this.stream = new SelfHdcPacketStream(); this.queue = Promise.resolve(); this.queuedBytes = 0;
    this.modern = false; this.authSuccess = false; this.closeAcknowledged = false;
    this.signingCount = 0; this.publicKeySent = false;
    this.current = { state: 'connecting', error: '', authenticated: false };
    const authentication = new AuthWait(); this.authentication = authentication;
    this.authTimer = setTimeout((): void => { this.fail('selfhdc_authorization_timeout', generation); }, budgetMs);
    // Includes crypto/key generation and socket setup in the same total authorization budget.
    this.startConnect(generation).catch((_error: Error): void => {
      this.fail('selfhdc_connection_or_identity_failed', generation);
    });
    try { await authentication.promise; }
    catch (_error) { /* status is the bounded public failure, never raw daemon/key material */ }
    if (generation === this.generation) { this.authentication = undefined; }
    return this.status();
  }

  private live(generation: number): boolean {
    return generation === this.generation && (this.current.state === 'connecting' ||
      this.current.state === 'awaiting_trust' || this.current.authenticated);
  }

  private async startConnect(generation: number): Promise<void> {
    this.identity = await SelfHdcIdentity.load(this.context);
    if (!this.live(generation)) { return; }
    const random = await SelfHdcIdentity.randomBytes(4);
    if (!this.live(generation)) { return; }
    this.session = new DataView(random.buffer, random.byteOffset, 4).getUint32(0, false) || 1;
    const tcp = socket.constructTCPSocketInstance(); this.tcp = tcp;
    tcp.on('message', (event: socket.SocketMessageInfo): void => {
      if (!this.live(generation)) { return; }
      const bytes = new Uint8Array(event.message);
      this.queuedBytes += bytes.length;
      if (this.queuedBytes > 4 * 1024 * 1024) { this.fail('selfhdc_receive_queue_limit', generation); return; }
      this.queue = this.queue.then(async (): Promise<void> => {
        this.queuedBytes -= bytes.length;
        if (!this.live(generation)) { return; }
        for (const packet of this.stream.feed(bytes)) {
          if (!this.live(generation)) { break; }
          await this.receive(packet, generation);
        }
      }).catch((error: Error): void => {
        this.fail(/^selfhdc_[a-z_]{1,64}$/.test(error.message) ? error.message : 'selfhdc_protocol_error', generation);
      });
    });
    tcp.on('error', (_error: BusinessError): void => { this.fail('selfhdc_socket_error', generation); });
    tcp.on('close', (): void => { if (this.live(generation)) { this.fail('selfhdc_disconnected', generation); } });
    await tcp.bind({ address: '127.0.0.1', family: 1, port: 0 });
    if (!this.live(generation)) { return; }
    await tcp.connect({ address: { address: '127.0.0.1', family: 1, port: this.port }, timeout: 8000 });
    if (!this.live(generation)) { return; }
    await this.handshake(0, hdcEncodeTlv('authtype', '1'), generation);
  }

  private async send(channel: number, command: number, data: Uint8Array, generation: number): Promise<void> {
    const tcp = this.tcp;
    if (!this.live(generation) || tcp === undefined) { throw new Error('selfhdc_not_connected'); }
    const packet = hdcEncodePacket({ channel: channel, command: command, data: data });
    await tcp.send({ data: packet.buffer });
    hdcRequire(this.live(generation), 'selfhdc_session_closed');
  }

  private async handshake(auth: number, data: Uint8Array, generation: number): Promise<void> {
    const value: SelfHdcHandshake = { auth: auth, session: this.session,
      target: '127.0.0.1:' + String(this.port), data: data,
      // Public protocol revision only. No SDK private hash or host attestation is fabricated.
      version: 'Ver: 3.2.0g' };
    await this.send(0, HDC_HANDSHAKE, hdcEncodeHandshake(value), generation);
  }

  private async receive(packet: SelfHdcPacket, generation: number): Promise<void> {
    if (packet.command === HDC_HANDSHAKE) {
      hdcRequire(packet.channel === 0 && !this.current.authenticated, 'selfhdc_unexpected_handshake');
      await this.receiveAuth(hdcDecodeHandshake(packet.data), generation); return;
    }
    if (packet.command === HDC_CLOSE) {
      hdcRequire(packet.data.length === 1 && packet.data[0] <= 1, 'selfhdc_close_invalid');
      if (packet.data[0] > 0) { await this.send(packet.channel, HDC_CLOSE, new Uint8Array([0]), generation); }
      if (packet.channel === 0) {
        // An earlier UNAUTH close does not acknowledge a later SUCCESS close.
        this.closeAcknowledged = this.authSuccess; this.maybeReady(); return;
      }
      const request = this.active;
      if (request === undefined || packet.channel !== request.channel) { throw new Error('selfhdc_channel_unexpected'); }
      clearTimeout(request.timer); this.active = undefined;
      const tail = hdcShellTail(hdcJoin(request.chunks), request.marker);
      if (tail.stdout.length > request.limit) {
        request.resolve({ stdout: tail.stdout.slice(0, request.limit), completed: false,
          dispatched: request.dispatched });
        this.fail('selfhdc_output_limit', generation); return;
      }
      request.resolve({ stdout: tail.stdout, completed: tail.completed, dispatched: request.dispatched,
        exitCode: tail.exitCode });
      if (!tail.completed) { this.fail('selfhdc_command_outcome_unknown', generation); }
      return;
    }
    if (packet.command === HDC_ECHO) {
      hdcRequire(packet.data.length >= 1, 'selfhdc_echo_empty');
      if (packet.data[0] === 0) {
        // Pending trust notices are not success and must never release shell execution.
        if (packet.channel === 0 && !this.current.authenticated &&
          /unauthor|confirm|authoriz|未授权|授权|允许|弹窗/i.test(hdcText(packet.data.slice(1)))) {
          this.current.state = 'awaiting_trust'; return;
        }
        this.fail('selfhdc_daemon_rejected', generation);
      }
      return;
    }
    if (packet.command === HDC_RAW) {
      const request = this.active;
      if (request === undefined || packet.channel !== request.channel || !this.current.authenticated) {
        throw new Error('selfhdc_output_unexpected');
      }
      if (request.length + packet.data.length > request.limit + 64) {
        this.fail('selfhdc_output_limit', generation); return;
      }
      request.chunks.push(packet.data); request.length += packet.data.length; return;
    }
    this.fail(packet.command === 20 ? 'selfhdc_tls_unsupported' : 'selfhdc_command_unsupported', generation);
  }

  private async receiveAuth(value: SelfHdcHandshake, generation: number): Promise<void> {
    hdcRequire(value.session === 0 || value.session === this.session, 'selfhdc_auth_session');
    if (value.auth === 6) { this.fail('selfhdc_tls_unsupported', generation); return; }
    if (value.auth === 3) {
      const identity = this.identity;
      if (this.publicKeySent || identity === undefined) { throw new Error('selfhdc_public_key_sequence'); }
      const fields = value.data.length >= 32 ? hdcDecodeTlv(value.data) : new Map<string, string>();
      if (fields.get('encrypttcp') === '1' || fields.get('authtype') === '2') {
        this.fail('selfhdc_tls_unsupported', generation); return;
      }
      const auth = fields.get('authtype');
      hdcRequire(auth === undefined || auth === '0' || auth === '1', 'selfhdc_auth_algorithm');
      this.modern = auth === '1'; this.publicKeySent = true; this.current.state = 'awaiting_trust';
      await this.handshake(3, hdcUtf8('AMITA Native\f' + identity.publicPem), generation); return;
    }
    if (value.auth === 2) {
      const identity = this.identity;
      if (!this.publicKeySent || identity === undefined || this.signingCount !== 0) {
        throw new Error('selfhdc_signature_sequence');
      }
      this.signingCount++;
      const signature = await identity.sign(value.data, this.modern);
      if (this.live(generation)) { await this.handshake(2, hdcUtf8(signature), generation); }
      return;
    }
    if (value.auth === 4) {
      const fields = hdcDecodeTlv(value.data);
      const status = fields.get('daemonauthstatus');
      if (status === 'DAEMON_UNAUTH') {
        this.authSuccess = false; this.closeAcknowledged = false; this.current.state = 'awaiting_trust'; return;
      }
      if (status !== 'SUCCESS') { this.fail('selfhdc_authorization_rejected', generation); return; }
      this.authSuccess = true; this.maybeReady(); return;
    }
    this.fail('selfhdc_auth_unsupported', generation);
  }

  private maybeReady(): void {
    if (this.authSuccess && this.closeAcknowledged && this.authentication !== undefined) {
      clearTimeout(this.authTimer); this.authTimer = -1;
      this.current = { state: 'ready', error: '', authenticated: true };
      if (this.sessionTimer < 0) {
        const generation = this.generation;
        this.sessionTimer = setTimeout((): void => { this.fail('selfhdc_session_expired', generation); }, 300000);
      }
      this.authentication.resolve();
    }
  }

  async shell(command: string, maxBytes: number = 1048576, timeoutMs: number = 15000): Promise<SelfHdcShellResult> {
    hdcRequire(this.current.authenticated && this.active === undefined && !this.commandPreparing,
      'selfhdc_not_ready_or_busy');
    hdcRequire(Number.isInteger(maxBytes) && maxBytes >= 1 && maxBytes <= 8 * 1024 * 1024,
      'selfhdc_output_budget_invalid');
    hdcRequire(Number.isInteger(timeoutMs) && timeoutMs >= 100 && timeoutMs <= 60000, 'selfhdc_timeout_invalid');
    // Validate before consuming randomness or reserving a command.
    hdcShellCommand(command, 'AMITA_HDC_00000000000000000000000000000000');
    this.commandPreparing = true;
    const generation = this.generation;
    let random: Uint8Array;
    try { random = await SelfHdcIdentity.randomBytes(16); }
    catch (_error) { this.commandPreparing = false; throw new Error('selfhdc_random_failed'); }
    this.commandPreparing = false;
    if (!this.live(generation) || !this.current.authenticated) {
      return { stdout: new Uint8Array(0), completed: false, dispatched: false };
    }
    let suffix = ''; for (const byte of random) { suffix += byte.toString(16).padStart(2, '0'); }
    const request = new ShellWait(this.channel++, 'AMITA_HDC_' + suffix, maxBytes);
    this.active = request;
    request.timer = setTimeout((): void => { this.fail('selfhdc_command_timeout_unknown', generation); }, timeoutMs);
    // Mark before send: a rejected socket send can still have delivered bytes. Never replay automatically.
    request.dispatched = true;
    this.send(request.channel, HDC_EXECUTE,
      hdcUtf8(hdcShellCommand(command, request.marker) + '\0'), generation).catch((_error: Error): void => {
      this.fail('selfhdc_command_transport_unknown', generation);
    });
    return request.promise;
  }

  private fail(error: string, generation: number): void {
    if (generation !== this.generation || this.current.state === 'closed' || this.current.state === 'error') { return; }
    this.current = { state: 'error', error: error, authenticated: false };
    this.finish();
  }

  private finish(): void {
    clearTimeout(this.authTimer); this.authTimer = -1;
    clearTimeout(this.sessionTimer); this.sessionTimer = -1;
    if (this.authentication !== undefined) { this.authentication.reject(new Error('selfhdc_not_authorized')); }
    if (this.active !== undefined) {
      const request = this.active; this.active = undefined; clearTimeout(request.timer);
      const stdout = hdcJoin(request.chunks).slice(0, request.limit);
      request.resolve({ stdout: stdout, completed: false, dispatched: request.dispatched });
    }
    const tcp = this.tcp; this.tcp = undefined;
    if (tcp !== undefined) {
      try { tcp.off('message'); tcp.off('error'); tcp.off('close'); } catch (_error) { /* Already closed. */ }
      try { tcp.close().catch((_error: Error): void => {}); } catch (_error) { /* Already closed. */ }
    }
  }

  close(): void {
    this.current = { state: 'closed', error: '', authenticated: false };
    this.finish(); this.authentication = undefined; this.generation++;
  }
}
