import { common } from '@kit.AbilityKit';
import { http } from '@kit.NetworkKit';
import { util } from '@kit.ArkTS';
import { NodeCredential, NodeCredentialStore } from '../security/NodeCredentialStore';
import { Artifact, BACKGROUND_BYTES, DataManifest, ManifestEnvelope, SMALL_ARTIFACT_BYTES, UpdateTrust,
  requireUpdate, validateTrust } from './Manifest';
import { UpdateDB, UpdateState } from './UpdateDB';
import { UpdateFiles } from './UpdateFiles';
import { verifyEnvelope } from './UpdateCrypto';

// No request.agent dependency: safe to share with WorkScheduler Extension.
export class UpdateCoordinator {
  private context: common.Context;
  private activeRequest: http.HttpRequest | null = null;
  private stopped: boolean = false;
  private deadline: number = 0;
  private used: number = 0;
  constructor(context: common.Context) { this.context = context; }
  cancel(): void {
    this.stopped = true;
    if (this.activeRequest !== null) { this.activeRequest.destroy(); this.activeRequest = null; }
  }
  private available(): void { requireUpdate(!this.stopped && Date.now() < this.deadline, 'update_budget_or_stop'); }
  private async get(url: string, credential: NodeCredential, limit: number): Promise<Uint8Array> {
    this.available();
    requireUpdate(this.used + limit <= BACKGROUND_BYTES, 'update_small_budget_exceeded');
    const transport: http.HttpRequest = http.createHttp();
    this.activeRequest = transport;
    try {
      const remaining: number = Math.max(1, Math.min(8000, this.deadline - Date.now()));
      const response: http.HttpResponse = await transport.request(url, {
        method: http.RequestMethod.GET, header: { 'Authorization': 'Bearer ' + credential.token, 'Accept': 'application/octet-stream' },
        expectDataType: http.HttpDataType.ARRAY_BUFFER, connectTimeout: remaining, readTimeout: remaining,
        usingCache: false, maxLimit: Math.max(1, limit), maxRedirects: 0
      });
      this.available();
      requireUpdate(response.responseCode === 200 && response.result instanceof ArrayBuffer, 'update_http_unavailable');
      const bytes: Uint8Array = new Uint8Array(response.result as ArrayBuffer);
      requireUpdate(bytes.length <= limit, 'update_http_budget_exceeded');
      this.used += bytes.length;
      return bytes;
    } finally {
      transport.destroy();
      if (this.activeRequest === transport) { this.activeRequest = null; }
    }
  }
  async syncSmall(): Promise<void> {
    this.stopped = false; this.deadline = Date.now() + 90000; this.used = 0;
    const db: UpdateDB = await UpdateDB.open(this.context);
    let owner: string = '';
    const timeout: number = setTimeout((): void => this.cancel(), 90000);
    try {
      owner = await db.acquire();
      const state: UpdateState = await db.state();
      if (state.trust.length === 0) { return; }
      const credential: NodeCredential | null = await new NodeCredentialStore().load();
      requireUpdate(credential !== null, 'update_node_credential_required');
      const trust: UpdateTrust = JSON.parse(state.trust) as UpdateTrust;
      validateTrust(trust, (credential as NodeCredential).nodeId);
      if (!trust.enabled) { return; }
      await db.checked(owner);
      const bytes: Uint8Array = await this.get(trust.manifest_url, credential as NodeCredential, 65536);
      const raw: string = new util.TextDecoder('utf-8', { fatal: true }).decodeToString(bytes);
      const manifest: DataManifest = await verifyEnvelope(raw, trust, state.highest);
      if (manifest.version === state.active) {
        requireUpdate(this.samePayload(raw, state.active_json), 'update_version_equivocation');
        return;
      }
      if (state.pending.length > 0) {
        const pending: DataManifest = await verifyEnvelope(state.pending, trust, state.highest);
        requireUpdate(manifest.version >= pending.version &&
          (manifest.version !== pending.version || this.samePayload(raw, state.pending)), 'update_pending_version_changed');
      }
      if (manifest.version === state.highest && state.active < state.highest) {
        // Explicit local rollback remains in force until a strictly newer version.
        return;
      }
      await db.pending(raw, owner);
      const files: UpdateFiles = new UpdateFiles(this.context);
      for (let index: number = 0; index < manifest.artifacts.length; index++) {
        this.available();
        const item: Artifact = manifest.artifacts[index];
        if (item.size > SMALL_ARTIFACT_BYTES) { continue; }
        if (await files.valid(item)) { continue; }
        if (this.used + Math.max(1, item.size) > BACKGROUND_BYTES) { continue; }
        await files.accept(item, await this.get(item.url, credential as NodeCredential, Math.max(1, item.size)));
      }
      const small: boolean = manifest.artifacts.every((item: Artifact): boolean => item.size <= SMALL_ARTIFACT_BYTES);
      const total: number = manifest.artifacts.reduce((sum: number, item: Artifact): number => sum + item.size, 0);
      if (small && total <= BACKGROUND_BYTES) { await this.activateIfComplete(db, owner, raw, manifest, files); }
    } catch (error) {
      if (owner.length > 0) { await db.checked(owner, 'update_failed_old_version_retained'); }
      throw new Error('update_failed_old_version_retained');
    } finally {
      clearTimeout(timeout);
      if (owner.length > 0) { await db.release(owner); }
      await db.close();
    }
  }
  private samePayload(left: string, right: string): boolean {
    return right.length > 0 && (JSON.parse(left) as ManifestEnvelope).payload === (JSON.parse(right) as ManifestEnvelope).payload;
  }
  async activateIfComplete(db: UpdateDB, owner: string, raw: string, manifest: DataManifest, files: UpdateFiles): Promise<boolean> {
    for (let index: number = 0; index < manifest.artifacts.length; index++) {
      if (!await files.valid(manifest.artifacts[index])) { return false; }
    }
    if (this.deadline > 0) { this.available(); }
    await db.activate(raw, manifest.version, owner);
    return true;
  }
  async rollback(): Promise<void> {
    const db: UpdateDB = await UpdateDB.open(this.context);
    let owner: string = '';
    try {
      owner = await db.acquire();
      const state: UpdateState = await db.state();
      requireUpdate(state.previous > 0 && state.trust.length > 0, 'update_no_previous_version');
      const trust: UpdateTrust = JSON.parse(state.trust) as UpdateTrust;
      const manifest: DataManifest = await verifyEnvelope(state.previous_json, trust, 0, true);
      const files: UpdateFiles = new UpdateFiles(this.context);
      for (let index: number = 0; index < manifest.artifacts.length; index++) {
        requireUpdate(await files.valid(manifest.artifacts[index]), 'update_previous_incomplete');
      }
      await db.rollback(owner);
    } finally { if (owner.length > 0) { await db.release(owner); } await db.close(); }
  }
}
