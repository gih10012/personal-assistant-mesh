import { common } from '@kit.AbilityKit';
import { BusinessError, deviceInfo } from '@kit.BasicServicesKit';
import { http } from '@kit.NetworkKit';
import { util } from '@kit.ArkTS';
import { pushService } from '@kit.PushKit';
import { ApprovalSurface } from '../control/ApprovalSurface';
import { WorkConfig, validateWorkConfig } from '../core/WorkProtocol';
import { NodeCredential, NodeCredentialStore } from '../security/NodeCredentialStore';
import { CHAT_CHUNK_BYTES, CHAT_MAX_FILE_BYTES, CHAT_MAX_FILES, CHAT_MAX_LOCAL_MESSAGES, CHAT_MAX_TEXT_BYTES,
  ChatApproval, ChatAttachment, ChatEvents, ChatJournal, ChatPending, ChatView, chatRows, mergeChatEvents } from './ChatState';
import { PhoneChatStore } from './PhoneChatStore';

const PROBE_COUNT: number = 3;
const PROBE_BUDGET_MS: number = 10000;
const SHARED_RECHECK_MS: number = 10000;

interface ChatRequest { session_id: string; }
interface AttachmentRequest extends ChatRequest { attachment_id: string; offset: number; }
interface DecisionRequest extends ChatRequest { interaction_id: string; decision: string; }
interface OwnerPushRequest extends ChatRequest { token: string; }
interface OwnerPushReply { format: string; enabled: boolean; bound: boolean; }
interface EventsRequest extends ChatRequest { after: number; limit: number; }
interface MessageRequest extends ChatRequest {
  event_id: string;
  created_at_ms: number;
  text: string;
  attachment_ids: string[];
  branch_id?: string;
}
interface UploadRequest extends AttachmentRequest { name: string; mime: string; total: number; base64: string; }

interface ChatAvailability { mode: string; available: boolean; }
interface ChatMessageReceipt { event_id: string; task_id: string; mode: string; }
interface ChatDecisionReceipt { interaction_id: string; decision: string; }
interface ChatUploadReceipt { attachment_id: string; next_offset: number; complete: boolean; }
interface ChatDownloadChunk { attachment_id: string; base64: string; next_offset: number; total: number; }

export class PhoneChatClient {
  private store: PhoneChatStore;
  private journal: ChatJournal;
  private config: () => WorkConfig | null;
  private notify: (view: ChatView) => void;
  private credentials: NodeCredentialStore = new NodeCredentialStore();
  private requests: Set<http.HttpRequest> = new Set();
  private base64: util.Base64Helper = new util.Base64Helper();
  private mode: string = 'connecting';
  private problem: string = '';
  private attempts: number = 0;
  private lastProbe: number = 0;
  private reachable: boolean = false;
  private busy: boolean = false;
  private foreground: boolean = false;
  private generation: number = 0;
  private timer: number = -1;
  private nextSync: number = 0;
  private nextPush: number = 0;
  private pushBusy: boolean = false;
  private uploadOffsets: Map<string, number> = new Map();

  constructor(context: common.UIAbilityContext, nodeId: string, config: () => WorkConfig | null,
    notify: (view: ChatView) => void) {
    this.store = new PhoneChatStore(context);
    this.journal = this.store.load(nodeId);
    this.config = config;
    this.notify = notify;
    this.publish();
  }

  setForeground(foreground: boolean): void {
    if (this.foreground === foreground) { return; }
    this.foreground = foreground;
    this.generation++;
    this.reachable = false;
    if (this.timer !== -1) { clearInterval(this.timer); this.timer = -1; }
    if (foreground) {
      this.lastProbe = 0;
      this.nextSync = 0;
      this.nextPush = 0;
      this.timer = setInterval((): void => { this.tick(); }, 750);
      this.tick();
    } else {
      this.mode = 'paused';
      this.requests.forEach((request: http.HttpRequest): void => { request.destroy(); });
      this.requests.clear();
      this.publish();
    }
  }

  refresh(): void {
    this.lastProbe = 0;
    this.nextSync = 0;
    this.nextPush = 0;
    this.tick();
  }

  enqueue(text: string, attachments: ChatAttachment[]): void {
    const content: string = text.trim();
    if (!this.foreground || new util.TextEncoder().encodeInto(content).length > CHAT_MAX_TEXT_BYTES ||
        (content.length === 0 && attachments.length === 0) || attachments.length > CHAT_MAX_FILES ||
        this.journal.queue.length >= CHAT_MAX_LOCAL_MESSAGES) {
      throw new Error('chat_message_not_queued');
    }
    const event: ChatPending = { event_id: 'phone-' + util.generateRandomUUID(), created_at_ms: Date.now(),
      text: content, attachments: attachments.slice() };
    const next: ChatJournal = this.copyJournal();
    next.queue.push(event);
    this.save(next);
    this.publish();
    this.nextSync = 0;
    this.tick();
  }

  async pick(count: number): Promise<ChatAttachment[]> {
    if (!this.foreground || count <= 0 || count > CHAT_MAX_FILES) { return []; }
    return this.store.pick(count);
  }

  discard(attachment: ChatAttachment): void { this.store.remove(attachment.attachment_id); }

  async exportAttachment(attachment: ChatAttachment): Promise<boolean> {
    if (!this.foreground || attachment.bytes <= 0 || attachment.bytes > CHAT_MAX_FILE_BYTES) {
      throw new Error('chat_attachment_unavailable');
    }
    if (!this.store.hasAttachment(attachment)) {
      const expected: number = this.generation;
      const bytes: Uint8Array = new Uint8Array(attachment.bytes);
      let offset: number = 0;
      while (offset < bytes.length) {
        const payload: AttachmentRequest = { session_id: this.sessionId(), attachment_id: attachment.attachment_id, offset: offset };
        const raw: string = await this.exchange('mesh_chat_attachment', payload, 8000, expected);
        const chunk: ChatDownloadChunk = JSON.parse(raw) as ChatDownloadChunk;
        const block: Uint8Array = this.base64.decodeSync(chunk.base64);
        if (chunk.attachment_id !== attachment.attachment_id || chunk.total !== bytes.length ||
            block.length <= 0 || block.length > CHAT_CHUNK_BYTES || chunk.next_offset !== offset + block.length ||
            chunk.next_offset > bytes.length) { throw new Error('chat_attachment_incomplete'); }
        bytes.set(block, offset);
        offset = chunk.next_offset;
      }
      this.assertForeground(expected);
      this.store.cacheAttachment(attachment, bytes);
    }
    return this.store.exportAttachment(attachment);
  }

  async decide(approval: ChatApproval, decision: string): Promise<void> {
    if (!this.foreground || !this.reachable || (decision !== 'accept' && decision !== 'decline') ||
        !this.journal.approvals.some((current: ChatApproval): boolean =>
          current.interaction_id === approval.interaction_id && current.task_id === approval.task_id &&
          current.prompt === approval.prompt)) { throw new Error('chat_original_approval_required'); }
    const payload: DecisionRequest = { session_id: this.sessionId(), interaction_id: approval.interaction_id, decision: decision };
    const raw: string = await this.exchange('mesh_chat_approve', payload, 8000, this.generation);
    const receipt: ChatDecisionReceipt = JSON.parse(raw) as ChatDecisionReceipt;
    if (receipt.interaction_id !== approval.interaction_id || receipt.decision !== decision) {
      throw new Error('chat_decision_unconfirmed');
    }
    const next: ChatJournal = this.copyJournal();
    next.approvals = next.approvals.filter((current: ChatApproval): boolean => current.interaction_id !== approval.interaction_id);
    this.save(next);
    this.publish();
    this.nextSync = 0;
  }

  private async tick(): Promise<void> {
    if (!this.foreground || this.busy || Date.now() < this.nextSync) { return; }
    this.busy = true;
    const expected: number = this.generation;
    try {
      const configuration: WorkConfig | null = this.config();
      if (configuration === null) {
        this.mode = 'unconfigured';
        this.reachable = false;
        return;
      }
      if (this.journal.session_id.length === 0) {
        const next: ChatJournal = this.copyJournal();
        next.session_id = configuration.session_id;
        this.save(next);
      }
      if (this.lastProbe === 0 || (this.mode === 'independent' && Date.now() - this.lastProbe >= SHARED_RECHECK_MS)) {
        await this.probe(expected);
      }
      await this.syncEvents(expected);
      const pending: ChatPending | undefined = this.journal.queue.find((event: ChatPending): boolean => event.accepted !== true);
      if (pending !== undefined) {
        for (const attachment of pending.attachments) { await this.upload(attachment, expected); }
        await this.send(pending, expected);
        await this.syncEvents(expected);
      }
      this.problem = '';
      this.nextSync = Date.now() + (this.journal.queue.length === 0 ? 1250 : 250);
      if (!this.pushBusy && Date.now() >= this.nextPush) { this.syncOwnerPush(expected); }
    } catch (error) {
      if (expected === this.generation) {
        this.problem = (error as Error).message === 'chat_journal_size' ? 'storage' : 'sync';
        this.reachable = false;
        this.nextSync = Date.now() + 2500;
        if (this.mode === 'shared') { this.lastProbe = 0; }
      }
    } finally {
      this.busy = false;
      this.publish();
    }
  }

  private async syncOwnerPush(expected: number): Promise<void> {
    this.pushBusy = true;
    this.nextPush = Date.now() + 60000;
    try {
      const session: ChatRequest = { session_id: this.sessionId() };
      const raw: string = await this.exchange('mesh_phone_push_status', session, 5000, expected);
      const route: OwnerPushReply = JSON.parse(raw) as OwnerPushReply;
      if (route.format !== 'amita-harmony-owner-push/1' || route.enabled !== true) {
        AppStorage.setOrCreate<string>('meshPushState', 'unconfigured');
        return;
      }
      const assigned: string = await pushService.getToken();
      this.assertForeground(expected);
      if (assigned.length === 0) { throw new Error('phone_push_token_unavailable'); }
      const authorized: boolean = await ApprovalSurface.notificationEnabled((): boolean =>
        expected === this.generation && this.foreground);
      this.assertForeground(expected);
      if (authorized || route.bound === true) {
        const binding: OwnerPushRequest = { session_id: session.session_id, token: authorized ? assigned : '' };
        const reply: OwnerPushReply = JSON.parse(await this.exchange('mesh_phone_push_bind', binding, 5000, expected)) as OwnerPushReply;
        if (reply.format !== route.format || reply.enabled !== true || reply.bound !== authorized) {
          throw new Error('phone_push_binding_unconfirmed');
        }
      }
      AppStorage.setOrCreate<string>('meshPushState', authorized ? 'bound' : 'permission_missing');
      if (authorized) { this.nextPush = Date.now() + 300000; }
    } catch (error) {
      if (this.foreground && expected === this.generation) {
        AppStorage.setOrCreate<string>('meshPushState', (error as BusinessError).code === 1000900012 ?
          'provider_entitlement_required' : 'unavailable');
      }
    }
    finally { this.pushBusy = false; }
  }

  private async probe(expected: number): Promise<void> {
    const deadline: number = Date.now() + PROBE_BUDGET_MS;
    const previousMode: string = this.mode;
    if (previousMode !== 'independent') { this.mode = 'connecting'; }
    this.attempts = 0;
    for (let attempt: number = 0; attempt < PROBE_COUNT; attempt++) {
      this.assertForeground(expected);
      const timeout: number = Math.min(3000, deadline - Date.now());
      if (timeout <= 0) { break; }
      this.attempts++;
      this.publish();
      try {
        const payload: ChatRequest = { session_id: this.sessionId() };
        const raw: string = await this.exchange('mesh_chat_status', payload, timeout, expected);
        const status: ChatAvailability = JSON.parse(raw) as ChatAvailability;
        this.reachable = true;
        if (status.available === true && status.mode === 'shared') {
          this.mode = 'shared';
          this.lastProbe = Date.now();
          this.publish();
          return;
        }
      } catch (_) { this.reachable = false; }
    }
    this.assertForeground(expected);
    if (this.journal.branch_id === '') {
      const next: ChatJournal = this.copyJournal();
      next.branch_id = 'phone-branch-' + util.generateRandomUUID();
      this.save(next);
    }
    this.mode = 'independent';
    this.lastProbe = Date.now();
    this.publish();
  }

  private async syncEvents(expected: number): Promise<void> {
    const payload: EventsRequest = { session_id: this.sessionId(), after: this.journal.cursor, limit: 32 };
    const raw: string = await this.exchange('mesh_chat_events', payload, 4000, expected);
    const page: ChatEvents = JSON.parse(raw) as ChatEvents;
    const next: ChatJournal = this.copyJournal();
    mergeChatEvents(next, page);
    if (next.cursor !== this.journal.cursor || next.queue.length !== this.journal.queue.length ||
        JSON.stringify(next.approvals) !== JSON.stringify(this.journal.approvals)) { this.save(next); }
    this.reachable = true;
    this.publish();
  }

  private async send(event: ChatPending, expected: number): Promise<void> {
    // Pin the first attempted route before POST. Reconciliation through the
    // original event ID takes precedence over any subsequent mode change.
    if (event.branch_id === undefined) {
      const next: ChatJournal = this.copyJournal();
      const original: ChatPending = next.queue.find((item: ChatPending): boolean => item.event_id === event.event_id) as ChatPending;
      original.branch_id = this.mode === 'independent' ? this.journal.branch_id : '';
      this.save(next);
      event = original;
    }
    const attachmentIds: string[] = event.attachments.map((attachment: ChatAttachment): string => attachment.attachment_id);
    const payload: MessageRequest = { session_id: this.sessionId(), event_id: event.event_id,
      created_at_ms: event.created_at_ms, text: event.text, attachment_ids: attachmentIds };
    if (event.branch_id) { payload.branch_id = event.branch_id; }
    const raw: string = await this.exchange('mesh_chat_send', payload, 8000, expected);
    const receipt: ChatMessageReceipt = JSON.parse(raw) as ChatMessageReceipt;
    if (receipt.event_id !== event.event_id || typeof receipt.task_id !== 'string' || receipt.task_id.length === 0) {
      throw new Error('chat_send_unconfirmed');
    }
    const next: ChatJournal = this.copyJournal();
    const original: ChatPending | undefined = next.queue.find((item: ChatPending): boolean => item.event_id === event.event_id);
    if (original !== undefined) {
      original.accepted = true;
      this.save(next);
    }
  }

  private async upload(attachment: ChatAttachment, expected: number): Promise<void> {
    let offset: number = this.uploadOffsets.get(attachment.attachment_id) || 0;
    while (offset < attachment.bytes) {
      const size: number = Math.min(CHAT_CHUNK_BYTES, attachment.bytes - offset);
      const bytes: Uint8Array = this.store.readAttachment(attachment, offset, size);
      const payload: UploadRequest = { session_id: this.sessionId(), attachment_id: attachment.attachment_id,
        name: attachment.name, mime: attachment.mime, offset: offset, total: attachment.bytes,
        base64: this.base64.encodeToStringSync(bytes) };
      const raw: string = await this.exchange('mesh_chat_upload', payload, 8000, expected);
      const receipt: ChatUploadReceipt = JSON.parse(raw) as ChatUploadReceipt;
      if (receipt.attachment_id !== attachment.attachment_id || !Number.isSafeInteger(receipt.next_offset) ||
          receipt.next_offset < offset + size || receipt.next_offset > attachment.bytes ||
          receipt.complete !== (receipt.next_offset === attachment.bytes)) { throw new Error('chat_upload_unconfirmed'); }
      offset = receipt.next_offset;
      this.uploadOffsets.set(attachment.attachment_id, offset);
    }
  }

  private sessionId(): string {
    if (this.journal.session_id === '') { throw new Error('chat_original_session_required'); }
    return this.journal.session_id;
  }

  private assertForeground(expected: number): void {
    if (expected !== this.generation || !this.foreground) { throw new Error('chat_foreground_required'); }
  }

  private async exchange(action: string, argumentsValue: ChatRequest, timeout: number, expected: number): Promise<string> {
    this.assertForeground(expected);
    const config: WorkConfig | null = this.config();
    if (config === null || config.session_id !== this.sessionId() || deviceInfo.sdkApiVersion < 23) {
      throw new Error('chat_original_session_required');
    }
    validateWorkConfig(config, this.journal.node_id);
    const credential: NodeCredential | null = await this.credentials.load();
    this.assertForeground(expected);
    if (credential === null || credential.nodeId !== config.node_id || !credential.scope?.includes('phone:node')) {
      throw new Error('chat_original_credential_required');
    }
    const request: http.HttpRequest = http.createHttp();
    this.requests.add(request);
    let timer: number = -1;
    try {
      const response: http.HttpResponse = await new Promise<http.HttpResponse>((resolve, reject): void => {
        timer = setTimeout((): void => { reject(new Error('chat_request_timeout')); }, timeout);
        request.request(config.resource_url, {
          method: http.RequestMethod.POST, header: { 'Authorization': 'Bearer ' + credential.token,
            'Accept': 'application/json', 'Content-Type': 'application/json' },
          extraData: JSON.stringify({ action: action, arguments: argumentsValue }),
          expectDataType: http.HttpDataType.STRING, connectTimeout: timeout, readTimeout: timeout,
          usingCache: false, maxLimit: 1048576, maxRedirects: 0
        }).then(resolve, reject);
      });
      this.assertForeground(expected);
      if (response.responseCode !== 200 || typeof response.result !== 'string') { throw new Error('chat_request_unconfirmed'); }
      return response.result;
    } finally {
      if (timer !== -1) { clearTimeout(timer); }
      if (this.requests.delete(request)) { request.destroy(); }
    }
  }

  private copyJournal(): ChatJournal {
    return { node_id: this.journal.node_id, session_id: this.journal.session_id, cursor: this.journal.cursor,
      events: this.journal.events.slice(), queue: this.journal.queue.map((item: ChatPending): ChatPending =>
        ({ event_id: item.event_id, created_at_ms: item.created_at_ms, text: item.text,
          attachments: item.attachments, branch_id: item.branch_id, accepted: item.accepted })),
      branch_id: this.journal.branch_id, approvals: this.journal.approvals.slice() };
  }

  private save(next: ChatJournal): void { this.store.save(next); this.journal = next; }

  private publish(): void {
    const pending: ChatPending[] = this.journal.queue.filter((item: ChatPending): boolean => item.accepted !== true);
    this.notify({ rows: chatRows(this.journal), approvals: this.journal.approvals.slice(),
      queued: pending.length, attempts: this.attempts, cursor: this.journal.cursor, mode: this.mode, problem: this.problem,
      reachable: this.reachable, queueDetails: pending.map((item: ChatPending): string =>
        item.event_id + ' · ' + new Date(item.created_at_ms).toLocaleString()).join('\n') });
  }
}
