import { common } from '@kit.AbilityKit';
import { deviceInfo } from '@kit.BasicServicesKit';
import { preferences } from '@kit.ArkData';
import { util } from '@kit.ArkTS';
import { http } from '@kit.NetworkKit';
import { Blob, requireValue } from '../core/Protocol';
import {
  PhoneResponse, ProviderDispatch, WorkConfig, WorkEngine, WorkHost, WorkRecord, WorkSnapshot,
  coldResumeWork, freshWorkSnapshot, validateWorkConfig
} from '../core/WorkProtocol';
import { NodeCredential, NodeCredentialStore } from '../security/NodeCredentialStore';
import { NativeServices } from './NativeServices';

interface RegistrationReceipt {
  format: string; operation_id: string; authority_id: string; publication_sha256: string; registry_ref: string;
}
interface BootstrapEnrollment {
  node_id: string; mesh_id: string; authority_id: string; operation_id: string;
  request_sha256: string; registration_receipt: RegistrationReceipt;
}
interface BootstrapCredential { kind: string; token: string; scope: string[]; expires_at?: number; }
interface WorkBootstrap {
  format: string; config: WorkConfig; credential: BootstrapCredential; enrollment: BootstrapEnrollment;
}
export class WorkStatus {
  configured: boolean = false;
  connection: string = 'unconfigured';
  presence_expires_at: number = 0;
  discovery_at: number = 0;
  tasks: number = 0;
  settled: number = 0;
  unknown: number = 0;
  pending_results: number = 0;
  error: string = '';
  task_states: WorkTaskStatus[] = [];
}
export interface WorkTaskStatus {
  task_id: string; task_epoch: number; operation_id: string; state: string;
  remote_state: string; lease_deadline: number;
}

// The foreground UI owns this loop. WorkScheduler only performs bounded data
// maintenance; it does not keep this executor resident or extend task leases.
export class WorkClient implements WorkHost {
  private native: NativeServices;
  private store: preferences.Preferences;
  private credentials: NodeCredentialStore = new NodeCredentialStore();
  private engine: WorkEngine | null = null;
  private foreground: boolean = false;
  private closed: boolean = false;
  private busy: boolean = false;
  private connected: boolean = false;
  private announced: string = '';
  private reconcileCursor: number = 0;
  private timer: number = -1;
  private notify: (status: string) => void;
  private state: WorkStatus = new WorkStatus();

  constructor(context: common.UIAbilityContext, native: NativeServices, notify: (status: string) => void) {
    this.native = native; this.notify = notify;
    this.store = preferences.getPreferencesSync(context, { name: 'mesh_work_v1' });
  }
  async initialize(): Promise<void> {
    const saved: string = this.store.getSync('snapshot', '') as string;
    const snapshot: WorkSnapshot = saved.length === 0 ? freshWorkSnapshot(this.native.snapshot.node_id) :
      JSON.parse(saved) as WorkSnapshot;
    coldResumeWork(snapshot, this.native.snapshot.node_id);
    this.engine = new WorkEngine(snapshot, this);
    await this.save(snapshot); this.publish();
    this.timer = setInterval(() => { this.tick(); }, 10000);
  }
  async save(snapshot: WorkSnapshot): Promise<void> {
    this.store.putSync('snapshot', JSON.stringify(snapshot)); await this.store.flush();
  }
  id(): string { return util.generateRandomUUID(); }
  now(): number { return Math.floor(Date.now() / 1000); }
  permission(dispatch: ProviderDispatch): string { return this.native.workPermission(dispatch); }
  allowed(dispatch: ProviderDispatch, permissionContext?: string): boolean {
    return !this.closed && this.foreground && this.native.workAllowed(dispatch, permissionContext);
  }
  async execute(dispatch: ProviderDispatch): Promise<Blob> {
    requireValue(this.foreground && this.state.presence_expires_at > this.now(), 'mesh_work_presence_required');
    return this.native.executeWorkDispatch(dispatch);
  }
  async exchange(action: string, args: Object): Promise<PhoneResponse> {
    const config: WorkConfig | null = (this.engine as WorkEngine).snapshot.config;
    requireValue(config !== null && deviceInfo.sdkApiVersion >= 23, 'mesh_work_secure_transport_required');
    const pinned: WorkConfig = config as WorkConfig;
    validateWorkConfig(pinned, this.native.snapshot.node_id);
    const credential: NodeCredential | null = await this.credentials.load();
    requireValue(credential !== null && credential.nodeId === pinned.node_id &&
      (credential.expiresAt === undefined || credential.expiresAt > this.now()) &&
      credential.scope !== undefined && credential.scope.includes('phone:node'), 'mesh_work_node_credential_required');
    const request: http.HttpRequest = http.createHttp();
    try {
      const response: http.HttpResponse = await request.request(pinned.resource_url, {
        method: http.RequestMethod.POST,
        header: { 'Authorization': 'Bearer ' + (credential as NodeCredential).token,
          'Accept': 'application/json', 'Content-Type': 'application/json' },
        extraData: JSON.stringify({ action: action, arguments: args }),
        expectDataType: http.HttpDataType.STRING, connectTimeout: 8000, readTimeout: 8000,
        usingCache: false, maxLimit: 1048576, maxRedirects: 0
      });
      requireValue(response.responseCode === 200 && typeof response.result === 'string' &&
        (response.result as string).length <= 1048576, 'mesh_work_authority_unavailable');
      return JSON.parse(response.result as string) as PhoneResponse;
    } finally { request.destroy(); }
  }
  async bindBootstrap(raw: string): Promise<void> {
    requireValue(this.foreground && !this.busy && raw.length <= 131072 && this.engine !== null,
      'mesh_work_foreground_configuration_required');
    const value: WorkBootstrap = JSON.parse(raw) as WorkBootstrap;
    requireValue(value.format === 'mesh-phone-bootstrap/1', 'mesh_work_bootstrap_invalid');
    validateWorkConfig(value.config, this.native.snapshot.node_id);
    const binding: BootstrapEnrollment = value.enrollment;
    const receipt: RegistrationReceipt = binding.registration_receipt;
    requireValue(binding.node_id === value.config.node_id && binding.mesh_id === value.config.mesh_id &&
      binding.authority_id === value.config.authority_id && /^[0-9a-f]{64}$/.test(binding.request_sha256) &&
      receipt.format === 'mesh-enrollment-registration/1' && receipt.operation_id === binding.operation_id &&
      receipt.authority_id === binding.authority_id && /^[0-9a-f]{64}$/.test(receipt.publication_sha256) &&
      typeof receipt.registry_ref === 'string' && receipt.registry_ref.length > 0,
      'mesh_work_original_registration_required');
    this.native.verifyWorkBinding(value.config, binding.operation_id, binding.request_sha256);
    const old: WorkConfig | null = (this.engine as WorkEngine).snapshot.config;
    requireValue(old === null || old.node_id === value.config.node_id && old.session_id === value.config.session_id &&
      old.mesh_id === value.config.mesh_id && old.authority_id === value.config.authority_id,
      'mesh_work_original_session_changed');
    requireValue(value.credential.kind === 'bearer' && typeof value.credential.token === 'string' &&
      value.credential.token.length >= 32 && !/\s/.test(value.credential.token) &&
      Array.isArray(value.credential.scope) && value.credential.scope.includes('phone:node'),
      'mesh_work_node_credential_invalid');
    const credential: NodeCredential = { nodeId: value.config.node_id, token: value.credential.token,
      scope: value.credential.scope };
    if (value.credential.expires_at !== undefined) {
      requireValue(Number.isFinite(value.credential.expires_at) && value.credential.expires_at > this.now(),
        'mesh_work_node_credential_expired');
      credential.expiresAt = value.credential.expires_at;
    }
    this.busy = true;
    try {
      await this.credentials.save(credential);
      (this.engine as WorkEngine).snapshot.config = value.config;
      await this.save((this.engine as WorkEngine).snapshot);
      this.connected = false; this.state.error = ''; this.publish();
    } finally { this.busy = false; }
    await this.tick();
  }
  setForeground(value: boolean): void {
    this.foreground = value; this.native.setForeground(value);
    if (!value) { this.state.connection = 'suspended'; }
    this.publish(); this.tick();
  }
  refresh(): void { this.tick(); }
  close(): void {
    this.closed = true; this.foreground = false; this.native.setForeground(false);
    if (this.timer !== -1) { clearInterval(this.timer); this.timer = -1; }
    this.tick();
  }
  status(): string { return JSON.stringify(this.state); }
  private publish(): void {
    const engine: WorkEngine | null = this.engine;
    this.state.configured = engine !== null && engine.snapshot.config !== null;
    if (engine !== null) {
      this.state.tasks = engine.snapshot.records.length;
      this.state.settled = engine.snapshot.records.filter((row: WorkRecord): boolean => row.state === 'settled').length;
      this.state.unknown = engine.snapshot.records.filter((row: WorkRecord): boolean => row.state === 'unknown').length;
      this.state.pending_results = engine.snapshot.records.filter((row: WorkRecord): boolean => row.result_json.length > 0 && row.state !== 'settled').length;
      this.state.task_states = engine.snapshot.records.map((row: WorkRecord): WorkTaskStatus => {
        const dispatch: ProviderDispatch = JSON.parse(row.dispatch_json) as ProviderDispatch;
        return { task_id: row.task_id, task_epoch: row.task_epoch, operation_id: row.operation_id,
          state: row.state, remote_state: row.last_remote_state, lease_deadline: dispatch.deadline as number };
      });
    }
    this.notify(this.status());
  }
  private observe(response: PhoneResponse): void {
    if (response.session === undefined || response.session.network === undefined ||
      !Number.isFinite(response.session.network.expires_at)) {
      throw new Error('mesh_work_presence_response_required');
    }
    this.state.presence_expires_at = response.session.network.expires_at;
    this.state.connection = response.session.network.state;
  }
  async tick(): Promise<void> {
    if (this.busy || this.engine === null || this.engine.snapshot.config === null) { return; }
    this.busy = true;
    const engine: WorkEngine = this.engine;
    try {
      if (!this.foreground || this.closed) {
        // Best effort withdrawal; local execution stops immediately even offline.
        if (this.connected || engine.snapshot.pending_control !== null) { this.observe(await engine.control(false, [])); }
        this.connected = false; this.state.connection = 'suspended'; return;
      }
      const capabilities: string[] = this.native.workCapabilities();
      this.observe(this.connected ? await engine.control(true, capabilities) : await engine.connect(capabilities));
      this.connected = true; this.announced = capabilities.join('|');
      if (this.now() - this.state.discovery_at >= 30) { await engine.discover(); this.state.discovery_at = this.now(); }
      // A stale journal can only inspect/report its original receipt, never execute.
      let reconciled: number = 0;
      for (let i: number = 0; i < engine.snapshot.records.length && this.foreground && reconciled < 4; i++) {
        this.reconcileCursor = this.reconcileCursor % engine.snapshot.records.length;
        const row: WorkRecord = engine.snapshot.records[this.reconcileCursor++];
        if (row.state !== 'settled') { await engine.reconcile(row); reconciled++; }
      }
      if (this.foreground && this.native.workCapabilities().length > 0 && this.state.presence_expires_at > this.now()) {
        const dispatch: ProviderDispatch[] = await engine.pull();
        for (let i: number = 0; i < dispatch.length && i < 4 && this.foreground; i++) {
          try { await engine.run(dispatch[i]); }
          catch (error) { this.state.error = 'mesh_work_dispatch_unavailable_original_preserved'; }
        }
      }
      this.state.error = '';
    } catch (error) {
      this.connected = false; this.state.connection = this.foreground ? 'unreachable' : 'suspended';
      this.state.error = 'mesh_work_connection_or_receipt_unconfirmed';
    } finally {
      this.busy = false; this.publish();
      // Catch a visibility/permission change that arrived during an HTTP call.
      if ((!this.foreground || this.closed) && this.connected) { this.tick(); }
      else if (this.foreground && this.connected && this.announced !== this.native.workCapabilities().join('|')) { this.tick(); }
    }
  }
}
