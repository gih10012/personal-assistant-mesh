import { common } from '@kit.AbilityKit';
import { util } from '@kit.ArkTS';
import { Artifact, DataManifest, UpdateTrust, requireUpdate, validateTrust } from './Manifest';
import { UpdateDB, UpdateState } from './UpdateDB';
import { UpdateFiles } from './UpdateFiles';
import { digest, verifyEnvelope } from './UpdateCrypto';

export interface InstalledConfig { dataVersion: number; raw: string; }

// Fixed configuration consumers only. No pending files, remote URLs, arbitrary
// filesystem paths, new key or trust roots; complete bytes are reverified.
export async function readInstalledConfig(context: common.Context, nodeId: string, path: string): Promise<InstalledConfig | null> {
  requireUpdate(['config/runtime.json', 'config/native-distribution.json'].includes(path), 'update_config_path_not_allowed');
  const db: UpdateDB = await UpdateDB.open(context);
  try {
    const state: UpdateState = await db.state();
    if (state.active === 0 || state.trust.length === 0) { return null; }
    const trust: UpdateTrust = JSON.parse(state.trust) as UpdateTrust;
    validateTrust(trust, nodeId);
    const manifest: DataManifest = await verifyEnvelope(state.active_json, trust, 0, true);
    requireUpdate(manifest.version === state.active, 'update_config_active_pointer_changed');
    let artifact: Artifact | null = null;
    for (let index: number = 0; index < manifest.artifacts.length; index++) {
      if (manifest.artifacts[index].path === path) { artifact = manifest.artifacts[index]; }
    }
    if (artifact === null) { return null; }
    requireUpdate(artifact.kind === 'config' && artifact.size <= 16384, 'update_config_budget_or_kind');
    const files: UpdateFiles = new UpdateFiles(context);
    const bytes: Uint8Array = files.read(files.path(artifact.sha256), artifact.size);
    requireUpdate(await digest(bytes) === artifact.sha256, 'update_config_complete_hash_required');
    const raw: string = new util.TextDecoder('utf-8', { fatal: true }).decodeToString(bytes);
    const after: UpdateState = await db.state();
    requireUpdate(after.active === state.active && after.active_json === state.active_json && after.trust === state.trust,
      'update_config_capture_changed');
    return { dataVersion: state.active, raw: raw };
  } finally { await db.close(); }
}
