export const MAX_ARTIFACT_BYTES: number = 32 * 1024 * 1024;
export const MAX_UPDATE_BYTES: number = 64 * 1024 * 1024;
export const SMALL_ARTIFACT_BYTES: number = 256 * 1024;
export const BACKGROUND_BYTES: number = 1024 * 1024;
export const SHELL_VERSION: number = 1000000;

export interface UpdateTrust {
  format: string;
  node_id: string;
  manifest_url: string;
  key_id: string;
  public_key_spki_base64: string;
  enabled: boolean;
}
export interface ManifestEnvelope { format: string; key_id: string; algorithm: string; payload: string; signature: string; }
export interface Artifact { path: string; kind: string; size: number; sha256: string; url: string; }
export interface DataManifest { format: string; version: number; min_shell_version: number; artifacts: Artifact[]; }
export interface DownloadRecord {
  id: string; version: number; sha256: string; file: string; tid: string; state: string;
}
export interface UpdateStatus {
  state: string; activeVersion: number; highestVersion: number; lastCheck: number;
  lastSuccess: number; pendingDownloads: number; scheduled: boolean; enabled: boolean; unknown: number; error: string;
}
export function requireUpdate(value: boolean, message: string): void { if (!value) { throw new Error(message); } }
export function origin(url: string): string {
  requireUpdate(/^https:\/\/[A-Za-z0-9.-]+(?::[0-9]{1,5})?\//.test(url) && !/[\s\\#@]/.test(url), 'update_https_url_invalid');
  return url.substring(0, url.indexOf('/', 8));
}
export function validateTrust(value: UpdateTrust, nodeId: string): void {
  requireUpdate(value.format === 'amita-update-trust/1' && value.node_id === nodeId &&
    typeof value.enabled === 'boolean' && /^[A-Za-z0-9._-]{1,128}$/.test(value.key_id) &&
    /^[A-Za-z0-9+/]+={0,2}$/.test(value.public_key_spki_base64) && value.public_key_spki_base64.length <= 2048,
    'update_trust_invalid');
  requireUpdate(value.manifest_url === origin(value.manifest_url) + '/v1/harmony/updates/manifest', 'update_manifest_route_invalid');
}
export function validateManifest(value: DataManifest, trust: UpdateTrust, highest: number, installedLocal: boolean = false): void {
  requireUpdate(value.format === 'amita-data-manifest/1' && Number.isSafeInteger(value.version) && value.version > 0 &&
    value.version >= highest && Number.isSafeInteger(value.min_shell_version) && value.min_shell_version > 0 &&
    value.min_shell_version <= SHELL_VERSION, 'update_version_incompatible_or_rollback');
  requireUpdate(Array.isArray(value.artifacts) && value.artifacts.length > 0 && value.artifacts.length <= 32, 'update_file_count_invalid');
  const paths: string[] = [];
  let total: number = 0;
  for (let index: number = 0; index < value.artifacts.length; index++) {
    const item: Artifact = value.artifacts[index];
    requireUpdate(typeof item.path === 'string' && item.path.length <= 180 &&
      /^[A-Za-z0-9_-][A-Za-z0-9._/-]*$/.test(item.path) &&
      item.path.split('/').every((part: string): boolean => part.length > 0 && part !== '.' && part !== '..') &&
      paths.indexOf(item.path) < 0 && (item.kind === 'config' || item.kind === 'skill' || item.kind === 'file'), 'update_path_or_kind_invalid');
    // Never treat remote data as a native module or executable installation.
    requireUpdate(!/\.(hap|hsp|har|abc|so|ets|js|mjs|cjs|wasm|dex|sh)$/i.test(item.path), 'update_native_code_forbidden');
    requireUpdate(Number.isSafeInteger(item.size) && item.size >= 0 && item.size <= MAX_ARTIFACT_BYTES &&
      /^[a-f0-9]{64}$/.test(item.sha256), 'update_artifact_budget_invalid');
    // Installed data may retain a previous trusted route after explicit owner
    // migration. This flag must never be used to authorize a network request.
    const allowedOrigin: string = installedLocal ? origin(item.url) : origin(trust.manifest_url);
    requireUpdate(item.url === allowedOrigin + '/v1/harmony/updates/artifacts/' + item.sha256,
      'update_artifact_origin_or_hash_route_invalid');
    paths.push(item.path); total += item.size;
  }
  requireUpdate(total <= MAX_UPDATE_BYTES, 'update_total_budget_exceeded');
}
