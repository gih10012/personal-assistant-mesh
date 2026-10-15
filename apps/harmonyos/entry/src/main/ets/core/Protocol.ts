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
export interface RelayBinding {
  mesh_id: string; node_id: string; owner_binding: string; enrollment_sha256: string;
  operation_id: string; request_sha256: string; nonce: string; expires_at: number;
  task_id: string; task_epoch: number; entry_request_id: string;
}
export interface RelayClientConfig { format: string; resource_url: string; binding: RelayBinding; }
export interface RelayRecord {
  resource_url: string; binding: RelayBinding; challenge_state: string; result_state: string;
  challenge_json: string; status_json: string; result_sha256: string;
}
export interface RelayStatus {
  format: string; mesh_id: string; node_id: string; runtime_kind: string; operation_id: string;
  request_sha256: string; expires_at: number; state: string; result_sha256: string | null;
  managed_use_authorized: boolean; native_execution_attested: boolean; automatic_replay: boolean;
}
export interface RelayResponse {
  format: string; binding: RelayBinding; action: string; status: RelayStatus;
  may_execute_original_challenge: boolean; automatic_retry: boolean; automatic_replay: boolean;
  relay_performed_verification: boolean; relay_performed_registration: boolean;
  managed_use_authorized: boolean; native_execution_attested: boolean; native_features_restricted: boolean;
  challenge?: Challenge; identical_existing_result?: boolean; original_result_retained?: boolean;
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

// Same owner-pinned /enrollment/node wire as EnrollmentRelay, not a second
// enrollment protocol. These fields contain NO credential or picker-grant URI.
export function validateRelayUrl(value: string): void {
  const suffix: string = '/enrollment/node';
  requireValue(typeof value === 'string' && value.endsWith(suffix), 'mesh_relay_https_required');
  const origin: string = value.slice(0, -suffix.length);
  validateJoinUrl(origin);
  const port: string = origin.slice(8).split(':')[1];
  requireValue(port === undefined || port === '443', 'mesh_relay_https_required');
}
export function validateRelayBinding(v: RelayBinding): void {
  shape(v, ['mesh_id', 'node_id', 'owner_binding', 'enrollment_sha256', 'operation_id',
    'request_sha256', 'nonce', 'expires_at', 'task_id', 'task_epoch', 'entry_request_id']);
  text(v.mesh_id); text(v.node_id); text(v.owner_binding); text(v.operation_id);
  text(v.task_id); text(v.entry_request_id);
  hash(v.enrollment_sha256); hash(v.request_sha256); hash(v.nonce);
  integer(v.expires_at, 1); integer(v.task_epoch, 1);
}
export function canonicalRelayBinding(v: RelayBinding): string {
  validateRelayBinding(v);
  return '{"enrollment_sha256":' + quote(v.enrollment_sha256) + ',"entry_request_id":' + quote(v.entry_request_id) +
    ',"expires_at":' + String(v.expires_at) + ',"mesh_id":' + quote(v.mesh_id) + ',"node_id":' + quote(v.node_id) +
    ',"nonce":' + quote(v.nonce) + ',"operation_id":' + quote(v.operation_id) + ',"owner_binding":' + quote(v.owner_binding) +
    ',"request_sha256":' + quote(v.request_sha256) + ',"task_epoch":' + String(v.task_epoch) + ',"task_id":' + quote(v.task_id) + '}';
}
export function validateRelayClientConfig(v: RelayClientConfig, enrollment: Enrollment, enrollmentHash: string): void {
  shape(v, ['format', 'resource_url', 'binding']);
  requireValue(v.format === 'mesh-enrollment-relay-client/1', 'mesh_relay_config_invalid');
  validateRelayUrl(v.resource_url); validateRelayBinding(v.binding);
  requireValue(v.binding.mesh_id === enrollment.mesh_id && v.binding.node_id === enrollment.node_id &&
    v.binding.owner_binding === enrollment.owner_binding && v.binding.enrollment_sha256 === enrollmentHash,
    'mesh_relay_original_enrollment_mismatch');
}
export function newRelayRecord(v: RelayClientConfig): RelayRecord {
  return { resource_url: v.resource_url, binding: v.binding, challenge_state: 'not_requested',
    result_state: 'not_submitted', challenge_json: '', status_json: '', result_sha256: '' };
}
export function coldResumeRelays(records: RelayRecord[]): void {
  requireValue(Array.isArray(records) && records.length <= 64, 'mesh_relay_journal_invalid');
  for (let i: number = 0; i < records.length; i++) {
    const row: RelayRecord = records[i];
    shape(row, ['resource_url', 'binding', 'challenge_state', 'result_state', 'challenge_json', 'status_json', 'result_sha256']);
    validateRelayUrl(row.resource_url); validateRelayBinding(row.binding);
    requireValue(['not_requested', 'attempted', 'granted', 'observed', 'consumed', 'unknown'].includes(row.challenge_state) &&
      ['not_submitted', 'attempted', 'acknowledged', 'remote_reported', 'unknown'].includes(row.result_state),
      'mesh_relay_journal_invalid');
    requireValue(typeof row.challenge_json === 'string' && row.challenge_json.length <= 131072 &&
      typeof row.status_json === 'string' && row.status_json.length <= 32768, 'mesh_relay_journal_invalid');
    if (row.result_sha256 !== '') { hash(row.result_sha256); }
    for (let j: number = 0; j < i; j++) {
      requireValue(records[j].binding.operation_id !== row.binding.operation_id, 'mesh_relay_original_operation_changed');
    }
    if (row.challenge_state === 'attempted' || row.challenge_state === 'granted') { row.challenge_state = 'unknown'; }
    if (row.result_state === 'attempted') { row.result_state = 'unknown'; }
  }
}
export function beginRelayChallenge(row: RelayRecord): void {
  requireValue(row.challenge_state === 'not_requested', 'mesh_relay_challenge_observe_only');
  row.challenge_state = 'attempted'; // Caller MUST durably flush before HTTP.
}
export function validateRelayResponse(v: RelayResponse, binding: RelayBinding, action: string): void {
  const fields: string[] = ['format', 'binding', 'action', 'status', 'may_execute_original_challenge',
    'automatic_retry', 'automatic_replay', 'relay_performed_verification', 'relay_performed_registration',
    'managed_use_authorized', 'native_execution_attested', 'native_features_restricted'];
  if (action === 'challenge') { fields.push('challenge'); }
  if (action === 'result') {
    if (v.identical_existing_result === true) { fields.push('identical_existing_result'); }
    else { requireValue(v.original_result_retained === true, 'mesh_relay_result_not_confirmed'); fields.push('original_result_retained'); }
  }
  shape(v, fields);
  requireValue(v.format === 'mesh-enrollment-relay-response/1' && v.action === action &&
    canonicalRelayBinding(v.binding) === canonicalRelayBinding(binding), 'mesh_relay_original_response_mismatch');
  requireValue(typeof v.may_execute_original_challenge === 'boolean' && v.automatic_retry === false &&
    v.automatic_replay === false && v.relay_performed_verification === false && v.relay_performed_registration === false &&
    v.managed_use_authorized === false && v.native_execution_attested === false && v.native_features_restricted === false,
    'mesh_relay_response_authority_invalid');
  requireValue(action === 'challenge' || v.may_execute_original_challenge === false, 'mesh_relay_response_authority_invalid');
  const status: RelayStatus = v.status;
  requireValue(status !== null && typeof status === 'object' && status.format === 'mesh-enrollment-status/1' &&
    status.mesh_id === binding.mesh_id && status.node_id === binding.node_id && status.runtime_kind === 'interactive' &&
    status.operation_id === binding.operation_id && status.request_sha256 === binding.request_sha256 &&
    status.expires_at === binding.expires_at && status.managed_use_authorized === false &&
    status.native_execution_attested === false && status.automatic_replay === false &&
    ['intent', 'attempted', 'unknown', 'result_reported', 'verified'].includes(status.state), 'mesh_relay_original_status_mismatch');
  if (status.result_sha256 !== null) { hash(status.result_sha256); }
  requireValue(!v.may_execute_original_challenge || status.state === 'attempted', 'mesh_relay_response_authority_invalid');
}
export function acceptRelayChallenge(row: RelayRecord, v: RelayResponse, enrollment: Enrollment,
    enrollmentHash: string, requestHash: string, now: number): void {
  requireValue(row.challenge_state === 'attempted', 'mesh_relay_challenge_observe_only');
  validateRelayResponse(v, row.binding, 'challenge');
  const value: Challenge = v.challenge as Challenge;
  validateChallenge(value, enrollment, enrollmentHash, v.may_execute_original_challenge === true ? now : value.created_at);
  requireValue(value.operation_id === row.binding.operation_id && value.nonce === row.binding.nonce &&
    value.expires_at === row.binding.expires_at && requestHash === row.binding.request_sha256,
    'mesh_relay_original_challenge_mismatch');
  row.challenge_json = canonicalChallenge(value);
  row.status_json = JSON.stringify(v.status);
  row.challenge_state = v.may_execute_original_challenge === true ? 'granted' : 'observed';
}
export function requireManualChallengeUnclaimed(records: RelayRecord[], operationId: string): void {
  for (let i: number = 0; i < records.length; i++) {
    requireValue(records[i].binding.operation_id !== operationId, 'mesh_relay_original_operation_observe_only');
  }
}
export function validateResultEnvelope(v: ResultEnvelope, binding: RelayBinding, enrollment: Enrollment): void {
  shape(v, ['format', 'operation_id', 'request_sha256', 'nonce', 'result', 'continuation', 'proxy']);
  requireValue(v.format === 'mesh-enrollment-result/1' && v.operation_id === binding.operation_id &&
    v.request_sha256 === binding.request_sha256 && v.nonce === binding.nonce && v.proxy === null,
    'mesh_relay_original_result_mismatch');
  continuation(v.continuation);
  requireValue(canonicalContinuation(v.continuation) === canonicalContinuation(enrollment.continuation), 'mesh_relay_original_result_mismatch');
  shape(v.result, ['bytes', 'sha256', 'base64']); integer(v.result.bytes); hash(v.result.sha256);
  requireValue(v.result.bytes <= MAX_BYTES && typeof v.result.base64 === 'string' && v.result.base64.length <= 87384,
    'mesh_relay_original_result_mismatch');
}
export function canonicalResult(v: ResultEnvelope): string {
  return '{"continuation":' + canonicalContinuation(v.continuation) + ',"format":' + quote(v.format) +
    ',"nonce":' + quote(v.nonce) + ',"operation_id":' + quote(v.operation_id) + ',"proxy":null,"request_sha256":' +
    quote(v.request_sha256) + ',"result":{"base64":' + quote(v.result.base64) + ',"bytes":' + String(v.result.bytes) +
    ',"sha256":' + quote(v.result.sha256) + '}}';
}
export function beginRelayResult(row: RelayRecord, resultHash: string): void {
  hash(resultHash);
  requireValue(row.result_state === 'not_submitted', 'mesh_relay_original_result_observe_only');
  row.result_sha256 = resultHash; row.result_state = 'attempted'; // Flush BEFORE POST, never re-POST unknown.
}
export function observeRelayStatus(row: RelayRecord, v: RelayResponse, action: string): void {
  validateRelayResponse(v, row.binding, action);
  row.status_json = JSON.stringify(v.status);
  if (v.status.result_sha256 !== null) {
    if (row.result_sha256 === '') { row.result_state = 'remote_reported'; }
    else if (row.result_sha256 === v.status.result_sha256) { row.result_state = 'acknowledged'; }
    else { row.result_state = 'unknown'; throw new Error('mesh_relay_original_result_conflict'); }
  }
}
