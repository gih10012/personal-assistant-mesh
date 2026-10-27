import { asset } from '@kit.AssetStoreKit';
import { util } from '@kit.ArkTS';
import { BusinessError } from '@kit.BasicServicesKit';

export interface NodeCredential {
  nodeId: string;
  token: string;
  expiresAt?: number;
  scope?: string[];
}

// App-scoped Asset, available to its WorkScheduler process after first unlock.
// Never synchronise credentials to another device or retain them on uninstall.
export class NodeCredentialStore {
  private bytes(value: string): Uint8Array { return new util.TextEncoder().encodeInto(value); }
  private query(): asset.AssetMap {
    const query: asset.AssetMap = new Map();
    query.set(asset.Tag.ALIAS, this.bytes('amita.node.credential.v1'));
    return query;
  }
  async load(): Promise<NodeCredential | null> {
    const query: asset.AssetMap = this.query();
    query.set(asset.Tag.RETURN_TYPE, asset.ReturnType.ALL);
    try {
      const rows: asset.AssetMap[] = await asset.query(query);
      if (rows.length === 0) { return null; }
      const secret: Uint8Array = rows[0].get(asset.Tag.SECRET) as Uint8Array;
      const value: NodeCredential = JSON.parse(new util.TextDecoder('utf-8', { fatal: true }).decodeToString(secret)) as NodeCredential;
      this.validate(value);
      if (value.expiresAt !== undefined && value.expiresAt <= Math.floor(Date.now() / 1000)) { return null; }
      return value;
    } catch (error) {
      if ((error as BusinessError).code === asset.ErrorCode.NOT_FOUND) { return null; }
      throw new Error('mesh_asset_credential_unavailable');
    }
  }
  private validate(value: NodeCredential): void {
    if (typeof value.nodeId !== 'string' || typeof value.token !== 'string' ||
      !/^[A-Za-z0-9._:-]{1,160}$/.test(value.nodeId) || !/^[\x21-\x7e]{32,768}$/.test(value.token) ||
      (value.expiresAt !== undefined && !Number.isSafeInteger(value.expiresAt)) ||
      (value.scope !== undefined && (!Array.isArray(value.scope) || value.scope.length > 16 ||
        !value.scope.every((scope: string): boolean => typeof scope === 'string' && /^[A-Za-z0-9:._-]{1,64}$/.test(scope))))) {
      throw new Error('mesh_node_credential_invalid');
    }
  }
  async save(value: NodeCredential): Promise<void> {
    this.validate(value);
    const secret: Uint8Array = this.bytes(JSON.stringify(value));
    if (secret.length >= 1024) { throw new Error('mesh_asset_secret_too_large'); }
    const attributes: asset.AssetMap = this.query();
    attributes.set(asset.Tag.SECRET, secret);
    attributes.set(asset.Tag.ACCESSIBILITY, asset.Accessibility.DEVICE_FIRST_UNLOCKED);
    attributes.set(asset.Tag.SYNC_TYPE, asset.SyncType.NEVER);
    attributes.set(asset.Tag.IS_PERSISTENT, false);
    attributes.set(asset.Tag.CONFLICT_RESOLUTION, asset.ConflictResolution.OVERWRITE);
    await asset.add(attributes);
  }
  async clear(): Promise<void> {
    try { await asset.remove(this.query()); }
    catch (error) {
      if ((error as BusinessError).code !== asset.ErrorCode.NOT_FOUND) { throw new Error('mesh_asset_clear_failed'); }
    }
  }
}
