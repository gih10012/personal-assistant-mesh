// Same mesh-enrollment/1 / challenge/1 / result/1 as assistant_mesh.enrollment.
// No Harmony imports: conformance tests run this exact module on Node's TS loader.
export interface Continuation { project_id: string; session_ref: string; state_ref: string; }
export interface RepoPin { repository: string; commit: string; }
export interface Runtime { kind: string; expires_at: number | null; state_ref: string; }
export interface Join { peer_id: string; authority_id: string; route_ref: string; credential_ref: string; trust_ref: string; }
export interface Enrollment {
  format: string; mesh_id: string; node_id: string; owner_binding: string;
  repo_pin: RepoPin; runtime: Runtime; join: Join; continuation: Continuation;
}
export interface Blob { bytes: number; sha256: string; base64: string; }
export interface Expected { bytes: number; sha256: string; }
export interface Challenge {
  format: string; operation_id: string; mesh_id: string; node_id: string; owner_binding: string;
  enrollment_sha256: string; continuation: Continuation; nonce: string; created_at: number;
  expires_at: number; capability_id: string; action: string; revision: number;
  input: Blob; expected: Expected; ttl_seconds: number; proxy: null;
}
export interface ResultEnvelope {
  format: string; operation_id: string; request_sha256: string; nonce: string;
  result: Blob; continuation: Continuation; proxy: null;
}
export interface Hello { protocol: string; node: string; authority: string; a2a_idempotent_receive: boolean; }
export interface JournalRecord {
  operation_id: string; request_sha256: string; nonce: string; action: string;
  state: string; result_json: string; created_at: number;
}
export interface Capability { id: string; revision: number; actions: string[]; }
export interface SystemInfo { name: string; version: string; }
export interface HarnessInfo { name: string; version: string; available: boolean; }
export interface StorageInfo { mode: string; state_ref: string; }
export interface AdaptationReport {
  format: string; mesh_id: string; node_id: string; system: SystemInfo; harness: HarnessInfo;
  storage: StorageInfo; capabilities: Capability[]; gaps: string[]; native_features_restricted: boolean;
}
export interface CapabilityState {
  capability_class: string; instance_id: string; source: string; scope: string;
  available: boolean; requires_foreground: boolean; permission: string;
  shared_resource_pool: string; fault_domain: string; max_artifact_bytes: number;
}
export interface DeviceState {
  state: string; capabilities: CapabilityState[]; native_features_restricted: boolean;
  xiaoyi_adapter: string; gui_adapter: string; background_residency: string;
}
export const MAX_BYTES: number = 65536;
export function exactByteView(data: ArrayBuffer, offset: number, length: number): Uint8Array {
  requireValue(Number.isSafeInteger(offset) && Number.isSafeInteger(length) && offset >= 0 &&
    length >= 0 && offset + length <= data.byteLength, 'mesh_byte_range_invalid');
  // Buffer may be a view of a larger allocator slab. Never hash its backing
  // ArrayBuffer outside the original UTF8 view's byteOffset/byteLength.
  return new Uint8Array(data, offset, length);
}
export function requireValue(ok: boolean, code: string = 'mesh_contract_invalid'): void {
  if (!ok) { throw new Error(code); }
}
function shape(value: Object, names: string[]): void {
  requireValue(value !== null && typeof value === 'object' && !Array.isArray(value));
  requireValue(Object.keys(value).sort().join('|') === names.slice().sort().join('|'));
}
function text(value: string): void {
  requireValue(typeof value === 'string' && value.trim().length > 0 && value.length <= 1024 && !/[\x00-\x1f]/.test(value));
}
function integer(value: number, minimum: number = 0): void {
  requireValue(Number.isSafeInteger(value) && value >= minimum);
}
function hash(value: string): void { requireValue(typeof value === 'string' && /^[0-9a-f]{64}$/.test(value)); }
function continuation(value: Continuation): void {
  shape(value, ['project_id', 'session_ref', 'state_ref']);
  text(value.project_id); text(value.session_ref); text(value.state_ref);
}
export function validateEnrollment(value: Enrollment, nodeId: string): void {
  shape(value, ['format', 'mesh_id', 'node_id', 'owner_binding', 'repo_pin', 'runtime', 'join', 'continuation']);
  requireValue(value.format === 'mesh-enrollment/1' && value.node_id === nodeId);
  text(value.mesh_id); text(value.node_id); text(value.owner_binding);
  shape(value.repo_pin, ['repository', 'commit']); text(value.repo_pin.repository);
  requireValue(/^[0-9a-f]{40}([0-9a-f]{24})?$/.test(value.repo_pin.commit) && !/^0+$/.test(value.repo_pin.commit));
  shape(value.runtime, ['kind', 'expires_at', 'state_ref']);
  // First phone release is interactive, not a promise of background residency.
  requireValue(value.runtime.kind === 'interactive'); text(value.runtime.state_ref);
  if (value.runtime.expires_at !== null) { integer(value.runtime.expires_at, 1); }
  shape(value.join, ['peer_id', 'authority_id', 'route_ref', 'credential_ref', 'trust_ref']);
  text(value.join.peer_id); text(value.join.authority_id); text(value.join.route_ref);
  text(value.join.credential_ref); text(value.join.trust_ref); continuation(value.continuation);
}
export function validateChallenge(value: Challenge, enrollment: Enrollment, enrollmentHash: string, now: number): void {
  shape(value, ['format', 'operation_id', 'mesh_id', 'node_id', 'owner_binding', 'enrollment_sha256', 'continuation',
    'nonce', 'created_at', 'expires_at', 'capability_id', 'action', 'revision', 'input', 'expected', 'ttl_seconds', 'proxy']);
  requireValue(value.format === 'mesh-enrollment-challenge/1' && value.proxy === null);
  text(value.operation_id); text(value.capability_id); hash(value.enrollment_sha256); hash(value.nonce);
  requireValue(value.mesh_id === enrollment.mesh_id && value.node_id === enrollment.node_id &&
    value.owner_binding === enrollment.owner_binding && value.enrollment_sha256 === enrollmentHash);
  continuation(value.continuation);
  requireValue(canonicalContinuation(value.continuation) === canonicalContinuation(enrollment.continuation));
  integer(value.created_at); integer(value.expires_at, 1); integer(value.revision, 1); integer(value.ttl_seconds, 1);
  requireValue(value.created_at <= now && now < value.expires_at && value.ttl_seconds <= 3600);
  requireValue(value.expires_at <= value.created_at + value.ttl_seconds);
  requireValue(enrollment.runtime.expires_at === null ||
    now < enrollment.runtime.expires_at && value.expires_at <= enrollment.runtime.expires_at);
  shape(value.input, ['bytes', 'sha256', 'base64']); integer(value.input.bytes); hash(value.input.sha256);
  requireValue(typeof value.input.base64 === 'string' && value.input.bytes <= MAX_BYTES);
  shape(value.expected, ['bytes', 'sha256']); integer(value.expected.bytes); hash(value.expected.sha256);
  requireValue(value.expected.bytes <= MAX_BYTES);
  requireValue(value.action === 'challenge.bytes.repeat' || value.action === 'file.read.selected');
}
function quote(value: string): string {
  return JSON.stringify(value).replace(/[\u007f-\uffff]/g,
    (letter: string): string => '\\u' + letter.charCodeAt(0).toString(16).padStart(4, '0'));
}
export function canonicalContinuation(value: Continuation): string {
  return '{"project_id":' + quote(value.project_id) + ',"session_ref":' + quote(value.session_ref) + ',"state_ref":' + quote(value.state_ref) + '}';
}
export function canonicalEnrollment(v: Enrollment): string {
  return '{"continuation":' + canonicalContinuation(v.continuation) + ',"format":' + quote(v.format) +
    ',"join":{"authority_id":' + quote(v.join.authority_id) + ',"credential_ref":' + quote(v.join.credential_ref) +
    ',"peer_id":' + quote(v.join.peer_id) + ',"route_ref":' + quote(v.join.route_ref) + ',"trust_ref":' + quote(v.join.trust_ref) +
    '},"mesh_id":' + quote(v.mesh_id) + ',"node_id":' + quote(v.node_id) + ',"owner_binding":' + quote(v.owner_binding) +
    ',"repo_pin":{"commit":' + quote(v.repo_pin.commit) + ',"repository":' + quote(v.repo_pin.repository) +
    '},"runtime":{"expires_at":' + String(v.runtime.expires_at) + ',"kind":' + quote(v.runtime.kind) + ',"state_ref":' + quote(v.runtime.state_ref) + '}}';
}
export function canonicalChallenge(v: Challenge): string {
  return '{"action":' + quote(v.action) + ',"capability_id":' + quote(v.capability_id) + ',"continuation":' + canonicalContinuation(v.continuation) +
    ',"created_at":' + String(v.created_at) + ',"enrollment_sha256":' + quote(v.enrollment_sha256) +
    ',"expected":{"bytes":' + String(v.expected.bytes) + ',"sha256":' + quote(v.expected.sha256) + '},"expires_at":' + String(v.expires_at) +
    ',"format":' + quote(v.format) + ',"input":{"base64":' + quote(v.input.base64) + ',"bytes":' + String(v.input.bytes) + ',"sha256":' + quote(v.input.sha256) +
    '},"mesh_id":' + quote(v.mesh_id) + ',"node_id":' + quote(v.node_id) + ',"nonce":' + quote(v.nonce) +
    ',"operation_id":' + quote(v.operation_id) + ',"owner_binding":' + quote(v.owner_binding) + ',"proxy":null,"revision":' + String(v.revision) +
    ',"ttl_seconds":' + String(v.ttl_seconds) + '}';
}
export function validateHello(value: Hello, enrollment: Enrollment): void {
  requireValue(value.protocol === 'mesh-a2a/1' && value.node === enrollment.join.peer_id &&
    value.authority === enrollment.join.authority_id && value.a2a_idempotent_receive === true, 'mesh_peer_mismatch');
}
export function validateJoinUrl(value: string): void {
  requireValue(/^https:\/\/[a-z0-9.-]+(?::[0-9]{1,5})?$/i.test(value), 'mesh_https_join_required');
  const port: string = value.slice(8).split(':')[1];
  if (port !== undefined) { requireValue(Number(port) >= 1 && Number(port) <= 65535, 'mesh_https_join_required'); }
}
export function beginOperation(records: JournalRecord[], challenge: Challenge, requestHash: string, now: number): JournalRecord {
  hash(requestHash);
  for (let index = 0; index < records.length; index++) {
    const old: JournalRecord = records[index];
    if (old.operation_id === challenge.operation_id) {
      requireValue(old.request_sha256 === requestHash && old.nonce === challenge.nonce, 'mesh_original_operation_changed');
      // Even a read-only effect is never retried because of an app restart.
      throw new Error('mesh_original_operation_read_only');
    }
  }
  requireValue(records.length < 64, 'mesh_journal_capacity_reached');
  const record: JournalRecord = { operation_id: challenge.operation_id, request_sha256: requestHash,
    nonce: challenge.nonce, action: challenge.action, state: 'attempted', result_json: '', created_at: now };
  records.push(record);
  return record;
}
export function coldResume(records: JournalRecord[]): void {
  for (let index = 0; index < records.length; index++) {
    if (records[index].state === 'attempted') { records[index].state = 'unknown'; }
  }
}
export function report(enrollment: Enrollment, hasSelectedGrant: boolean): AdaptationReport {
  const capabilities: Capability[] = [{ id: enrollment.node_id + ':bytes-v1', revision: 1, actions: ['challenge.bytes.repeat'] }];
  if (hasSelectedGrant) { capabilities.push({ id: enrollment.node_id + ':selected-file-v1', revision: 1, actions: ['file.read.selected'] }); }
  return { format: 'mesh-adaptation-report/1', mesh_id: enrollment.mesh_id, node_id: enrollment.node_id,
    system: { name: 'HarmonyOS', version: 'API12-compatible; device-unverified' },
    harness: { name: 'native-capability-provider', version: '0.1.0', available: false },
    storage: { mode: 'local', state_ref: 'private:application-sandbox-journal' }, capabilities: capabilities,
    gaps: ['interactive only; no guaranteed background service', 'native model not configured',
      'XiaoYi/A2A platform adapter not connected', 'GUI control and delegated Bluetooth devices not verified'],
    native_features_restricted: false };
}
export function deviceState(nodeId: string, foreground: boolean, shared: boolean, selected: boolean): DeviceState {
  const bytes: CapabilityState = { capability_class: 'challenge.bytes.repeat', instance_id: nodeId + ':bytes-v1',
    source: 'native-application-byte-recipe', scope: 'one approved original challenge',
    available: foreground && shared, requires_foreground: true, permission: 'local owner opt-in',
    shared_resource_pool: nodeId + ':app-runtime', fault_domain: nodeId, max_artifact_bytes: MAX_BYTES };
  const file: CapabilityState = { capability_class: 'file.read.selected', instance_id: nodeId + ':selected-file-v1',
    source: 'system-document-picker-temporary-grant', scope: 'one owner-selected document; no all-files access',
    available: foreground && shared && selected, requires_foreground: true, permission: 'picker URI plus local owner opt-in',
    shared_resource_pool: nodeId + ':app-runtime', fault_domain: nodeId, max_artifact_bytes: MAX_BYTES };
  return { state: foreground ? 'ACTIVE' : 'SUSPENDED', capabilities: [bytes, file],
    native_features_restricted: false, xiaoyi_adapter: 'not_connected', gui_adapter: 'not_verified',
    background_residency: 'not_promised_interactive_node' };
}
