import { common } from '@kit.AbilityKit';
import { NodeCredential, NodeCredentialStore } from '../security/NodeCredentialStore';
import { scheduleUpdates } from '../background/Schedule';
import { DataManifest, DownloadRecord, UpdateStatus, UpdateTrust, requireUpdate, validateTrust } from './Manifest';
import { ManagedDownload } from './ManagedDownload';
import { UpdateCoordinator } from './UpdateCoordinator';
import { UpdateDB, UpdateState } from './UpdateDB';
import { UpdateFiles } from './UpdateFiles';
import { verifyEnvelope } from './UpdateCrypto';

// UIAbility façade. Only trusted owner bootstrap import can set/replace a route.
export class MaintenanceService {
  private context: common.UIAbilityContext;
  private foreground: () => boolean;
  private busy: boolean = false;
  private coordinator: UpdateCoordinator | null = null;
  constructor(context: common.UIAbilityContext, foreground: () => boolean = (): boolean => AppStorage.get<boolean>('meshForeground') === true) {
    this.context = context; this.foreground = foreground;
  }
  stop(): void { if (this.coordinator !== null) { this.coordinator.cancel(); } }
  async configureTrusted(raw: string, nodeId: string): Promise<void> {
    requireUpdate(this.foreground() && raw.length <= 8192, 'update_trusted_foreground_import_required');
    const value: UpdateTrust = JSON.parse(raw) as UpdateTrust;
    validateTrust(value, nodeId);
    const db: UpdateDB = await UpdateDB.open(this.context);
    let owner: string = '';
    try {
      owner = await db.acquire();
      const state: UpdateState = await db.state();
      if (state.trust.length > 0) {
        const old: UpdateTrust = JSON.parse(state.trust) as UpdateTrust;
        requireUpdate(old.node_id === nodeId && old.key_id === value.key_id &&
          old.public_key_spki_base64 === value.public_key_spki_base64, 'update_pinned_identity_or_key_changed');
        // Pending downloads retain their original durable IDs, but an explicit
        // route replacement requires a freshly signed candidate at the new route.
        if (old.manifest_url !== value.manifest_url) { await db.pending('', owner); }
      }
      requireUpdate(this.foreground(), 'update_foreground_required');
      await db.configure(JSON.stringify(value), owner);
    } finally { if (owner.length > 0) { await db.release(owner); } await db.close(); }
    await scheduleUpdates(this.context, value.enabled);
  }
  async setEnabled(enabled: boolean): Promise<void> {
    const db: UpdateDB = await UpdateDB.open(this.context);
    let raw: string = '';
    try { raw = (await db.state()).trust; } finally { await db.close(); }
    requireUpdate(raw.length > 0, 'update_trust_required');
    const trust: UpdateTrust = JSON.parse(raw) as UpdateTrust;
    trust.enabled = enabled;
    await this.configureTrusted(JSON.stringify(trust), trust.node_id);
    if (!enabled) { this.stop(); }
  }
  async syncForeground(): Promise<void> {
    requireUpdate(this.foreground() && !this.busy, 'update_foreground_or_idle_required');
    this.busy = true;
    try {
      this.coordinator = new UpdateCoordinator(this.context);
      await this.coordinator.syncSmall();
      await this.reconcile();
    } finally { this.coordinator = null; this.busy = false; }
  }
  async reconcileForeground(): Promise<void> {
    if (!this.foreground() || this.busy) { return; }
    this.busy = true;
    try { await this.reconcile(); } finally { this.busy = false; }
  }
  private async reconcile(): Promise<void> {
    requireUpdate(this.foreground(), 'update_foreground_required');
    const db: UpdateDB = await UpdateDB.open(this.context);
    let owner: string = '';
    try {
      owner = await db.acquire();
      const state: UpdateState = await db.state();
      if (state.trust.length === 0) { return; }
      const trust: UpdateTrust = JSON.parse(state.trust) as UpdateTrust;
      if (!trust.enabled) { return; }
      const credential: NodeCredential | null = await new NodeCredentialStore().load();
      requireUpdate(credential !== null, 'update_node_credential_required');
      validateTrust(trust, (credential as NodeCredential).nodeId);
      if (state.pending.length === 0) { return; }
      const manifest: DataManifest = await verifyEnvelope(state.pending, trust, state.highest);
      await new ManagedDownload(this.context, this.foreground).reconcile(db, manifest, credential as NodeCredential);
      requireUpdate(this.foreground(), 'update_foreground_required');
      await new UpdateCoordinator(this.context).activateIfComplete(db, owner, state.pending, manifest, new UpdateFiles(this.context));
    } finally { if (owner.length > 0) { await db.release(owner); } await db.close(); }
  }
  async restoreSchedule(): Promise<void> {
    const db: UpdateDB = await UpdateDB.open(this.context);
    let enabled: boolean = false;
    try {
      const raw: string = (await db.state()).trust;
      if (raw.length > 0) { enabled = (JSON.parse(raw) as UpdateTrust).enabled; }
    } finally { await db.close(); }
    await scheduleUpdates(this.context, enabled);
  }
  async status(): Promise<UpdateStatus> {
    const db: UpdateDB = await UpdateDB.open(this.context);
    try {
      const state: UpdateState = await db.state();
      const rows: DownloadRecord[] = await db.downloads();
      let pending: number = 0; let unknown: number = 0;
      for (let index: number = 0; index < rows.length; index++) {
        if (rows[index].state !== 'verified' && rows[index].state !== 'failed') { pending++; }
        if (rows[index].state === 'unknown' || rows[index].state === 'creating') { unknown++; }
      }
      return { state: state.trust.length === 0 ? 'unconfigured' :
        !(JSON.parse(state.trust) as UpdateTrust).enabled ? 'disabled' :
        state.pending.length > 0 ? 'pending' : state.active > 0 ? 'active' : 'ready',
        activeVersion: state.active, highestVersion: state.highest, lastCheck: state.last_check,
        lastSuccess: state.last_success, pendingDownloads: pending, unknown: unknown,
        scheduled: state.scheduled === 1, enabled: state.trust.length > 0 && (JSON.parse(state.trust) as UpdateTrust).enabled,
        error: state.error };
    } finally { await db.close(); }
  }
  async rollback(): Promise<void> {
    requireUpdate(this.foreground() && !this.busy, 'update_foreground_or_idle_required');
    this.busy = true;
    try { await new UpdateCoordinator(this.context).rollback(); } finally { this.busy = false; }
  }
  async resolveDataPath(path: string): Promise<string> {
    const db: UpdateDB = await UpdateDB.open(this.context);
    try {
      const state: UpdateState = await db.state();
      requireUpdate(state.active > 0 && state.trust.length > 0, 'update_active_data_missing');
      const manifest: DataManifest = await verifyEnvelope(state.active_json, JSON.parse(state.trust) as UpdateTrust, 0, true);
      const files: UpdateFiles = new UpdateFiles(this.context);
      for (let index: number = 0; index < manifest.artifacts.length; index++) {
        if (manifest.artifacts[index].path === path) {
          requireUpdate(await files.valid(manifest.artifacts[index]), 'update_active_artifact_corrupt');
          return files.path(manifest.artifacts[index].sha256);
        }
      }
      throw new Error('update_data_path_not_installed');
    } finally { await db.close(); }
  }
}
