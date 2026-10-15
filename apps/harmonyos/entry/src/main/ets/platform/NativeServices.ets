import { common } from '@kit.AbilityKit';
import { deviceInfo } from '@kit.BasicServicesKit';
import { preferences } from '@kit.ArkData';
import { buffer, util } from '@kit.ArkTS';
import { cryptoFramework } from '@kit.CryptoArchitectureKit';
import { fileIo, picker } from '@kit.CoreFileKit';
import { http } from '@kit.NetworkKit';
import {
  AdaptationReport, Blob, Challenge, DeviceState, Enrollment, Hello, JournalRecord, MAX_BYTES,
  RelayClientConfig, RelayRecord, RelayResponse, ResultEnvelope, acceptRelayChallenge,
  beginOperation, beginRelayChallenge, beginRelayResult, canonicalChallenge, canonicalEnrollment,
  canonicalRelayBinding, canonicalResult, coldResume, coldResumeRelays, deviceState, exactByteView,
  newRelayRecord, observeRelayStatus, report, requireManualChallengeUnclaimed, requireValue,
  validateChallenge, validateEnrollment, validateHello, validateJoinUrl, validateRelayClientConfig,
  validateRelayResponse, validateRelayUrl, validateResultEnvelope
} from '../core/Protocol';

export class NodeSnapshot {
  node_id: string = '';
  enrollment_json: string = '';
  records: JournalRecord[] = [];
  relays: RelayRecord[] = []; // Original operation transport checkpoints, never credentials.
}

export class NativeServices {
  private context: common.UIAbilityContext;
  private store: preferences.Preferences;
  snapshot: NodeSnapshot = new NodeSnapshot();
  private enrollment: Enrollment | null = null;
  private enrollmentHash: string = '';
  private selectedUri: string = ''; // Picker's temporary grant: NEVER persisted/advertised.
  private busy: boolean = false;
  private foreground: boolean = true;
  sharingEnabled: boolean = false; // App restarts require explicit local permission again.

  constructor(context: common.UIAbilityContext) {
    this.context = context;
    this.store = preferences.getPreferencesSync(context, { name: 'mesh_node_v1' });
  }
  async initialize(): Promise<void> {
    const saved: string = this.store.getSync('snapshot', '') as string;
    if (saved.length > 0) {
      this.snapshot = JSON.parse(saved) as NodeSnapshot;
      requireValue(typeof this.snapshot.node_id === 'string' && Array.isArray(this.snapshot.records), 'mesh_private_state_invalid');
      coldResume(this.snapshot.records);
      if (this.snapshot.relays === undefined) { this.snapshot.relays = []; }
      coldResumeRelays(this.snapshot.relays);
    } else {
      // App-scoped random node identity, not IMEI/serial/MAC or hardware identity attestation.
      this.snapshot.node_id = 'harmony-' + util.generateRandomUUID();
    }
    if (this.snapshot.enrollment_json.length > 0) {
      const value: Enrollment = JSON.parse(this.snapshot.enrollment_json) as Enrollment;
      validateEnrollment(value, this.snapshot.node_id);
      this.enrollment = value;
      this.enrollmentHash = await this.digestText(canonicalEnrollment(value));
      for (let i: number = 0; i < this.snapshot.relays.length; i++) {
        const row: RelayRecord = this.snapshot.relays[i];
        validateRelayClientConfig({ format: 'mesh-enrollment-relay-client/1', resource_url: row.resource_url,
          binding: row.binding }, value, this.enrollmentHash);
      }
    }
    requireValue(this.enrollment !== null || this.snapshot.relays.length === 0, 'mesh_relay_original_enrollment_required');
    await this.persist();
  }
  private async persist(): Promise<void> {
    this.store.putSync('snapshot', JSON.stringify(this.snapshot));
    // Intent is not allowed to execute until this durable flush resolves.
    await this.store.flush();
  }
  setForeground(value: boolean): void {
    this.foreground = value;
    if (!value) { this.sharingEnabled = false; }
  }
  setSharing(value: boolean): void { this.sharingEnabled = value && this.foreground; }
  hasSelectedGrant(): boolean { return this.foreground && this.sharingEnabled && this.selectedUri.length > 0; }
  async bindEnrollment(raw: string): Promise<void> {
    requireValue(!this.busy && this.foreground && raw.length <= 65536, 'mesh_owner_binding_unavailable');
    this.busy = true;
    try {
      const value: Enrollment = JSON.parse(raw) as Enrollment;
      validateEnrollment(value, this.snapshot.node_id);
      const encoded: string = canonicalEnrollment(value);
      // A new route/repo/owner never silently replaces old operation bindings.
      requireValue(this.snapshot.enrollment_json.length === 0 || this.snapshot.enrollment_json === encoded,
        'mesh_original_enrollment_changed');
      const hash: string = await this.digestText(encoded);
      this.enrollment = value;
      this.enrollmentHash = hash;
      this.snapshot.enrollment_json = encoded;
      await this.persist();
    } finally { this.busy = false; }
  }
  adaptationReport(): AdaptationReport {
    requireValue(this.enrollment !== null, 'mesh_owner_enrollment_required');
    return report(this.enrollment as Enrollment, this.hasSelectedGrant());
  }
  dynamicState(): DeviceState {
    return deviceState(this.snapshot.node_id, this.foreground, this.sharingEnabled, this.selectedUri.length > 0);
  }
  async connect(endpoint: string, scopedToken: string): Promise<void> {
    requireValue(this.enrollment !== null && this.foreground && !this.busy, 'mesh_owner_enrollment_required');
    validateJoinUrl(endpoint);
    // API23+ provides maxRedirects=0. Older devices must not silently forward
    // the scoped credential to an unapproved redirected origin.
    requireValue(deviceInfo.sdkApiVersion >= 23, 'mesh_no_redirect_transport_requires_api23');
    requireValue(scopedToken.length >= 32 && !/\s/.test(scopedToken), 'mesh_scoped_credential_required');
    const transport: http.HttpRequest = http.createHttp();
    this.busy = true;
    try {
      const response: http.HttpResponse = await transport.request(endpoint + '/v1/mesh/hello', {
        method: http.RequestMethod.GET,
        header: { 'Authorization': 'Bearer ' + scopedToken, 'Accept': 'application/json' },
        expectDataType: http.HttpDataType.STRING, connectTimeout: 8000, readTimeout: 8000,
        usingCache: false, maxLimit: 65536, maxRedirects: 0
      });
      requireValue(response.responseCode === 200 && typeof response.result === 'string', 'mesh_join_unavailable');
      const raw: string = response.result as string;
      requireValue(raw.length <= 65536, 'mesh_join_response_too_large');
      validateHello(JSON.parse(raw) as Hello, this.enrollment as Enrollment);
      // This authenticates an existing join path only. No registration/grant claim.
    } finally { this.busy = false; transport.destroy(); }
    // Credential intentionally remains caller memory only, never Preferences/logs.
  }
  async chooseDocument(): Promise<void> {
    requireValue(this.foreground && !this.busy, 'mesh_foreground_required');
    this.busy = true;
    this.sharingEnabled = false;
    try {
      const options: picker.DocumentSelectOptions = new picker.DocumentSelectOptions();
      options.maxSelectNumber = 1;
      const view: picker.DocumentViewPicker = new picker.DocumentViewPicker(this.context);
      const selected: string[] = await view.select(options);
      this.selectedUri = selected.length === 1 && this.foreground ? selected[0] : '';
      // Selecting is not consent to send; permission toggle remains separate.
    } finally { this.busy = false; this.sharingEnabled = false; }
  }
  private readSelectedComplete(): Uint8Array {
    requireValue(this.hasSelectedGrant(), 'mesh_selected_file_consent_required');
    const file: fileIo.File = fileIo.openSync(this.selectedUri, fileIo.OpenMode.READ_ONLY);
    try {
      const size: number = fileIo.statSync(file.fd).size;
      requireValue(Number.isSafeInteger(size) && size >= 0 && size <= MAX_BYTES, 'mesh_file_budget_exceeded');
      const bytes: ArrayBuffer = new ArrayBuffer(size);
      const read: number = size === 0 ? 0 : fileIo.readSync(file.fd, bytes);
      requireValue(read === size && fileIo.statSync(file.fd).size === size, 'mesh_file_changed_or_incomplete');
      return new Uint8Array(bytes);
    } finally { fileIo.closeSync(file); }
  }
  async digest(bytes: Uint8Array): Promise<string> {
    const md: cryptoFramework.Md = cryptoFramework.createMd('SHA256');
    await md.update({ data: bytes });
    const result: cryptoFramework.DataBlob = await md.digest();
    let hex: string = '';
    for (let index: number = 0; index < result.data.length; index++) {
      hex += result.data[index].toString(16).padStart(2, '0');
    }
    return hex;
  }
  private async digestText(text: string): Promise<string> {
    const utf8: buffer.Buffer = buffer.from(text, 'utf-8');
    return this.digest(exactByteView(utf8.buffer, utf8.byteOffset, utf8.length));
  }
  async executeApprovedChallenge(raw: string): Promise<string> {
    requireValue(this.enrollment !== null && this.sharingEnabled && this.foreground && !this.busy,
      'mesh_local_owner_consent_required');
    requireValue(raw.length <= 131072, 'mesh_challenge_too_large');
    this.busy = true;
    try { return await this.executeChallenge(raw, null); }
    finally { this.busy = false; }
  }
  private async executeChallenge(raw: string, relay: RelayRecord | null): Promise<string> {
    requireValue(this.enrollment !== null && this.sharingEnabled && this.foreground,
      'mesh_local_owner_consent_required');
    let record: JournalRecord | null = null;
    try {
      const challenge: Challenge = JSON.parse(raw) as Challenge;
      validateChallenge(challenge, this.enrollment as Enrollment, this.enrollmentHash, Math.floor(Date.now() / 1000));
      const requestHash: string = await this.digestText(canonicalChallenge(challenge));
      if (relay === null) { requireManualChallengeUnclaimed(this.snapshot.relays, challenge.operation_id); }
      else {
        requireValue(relay.challenge_state === 'granted' && relay.binding.operation_id === challenge.operation_id &&
          relay.binding.nonce === challenge.nonce && relay.binding.request_sha256 === requestHash,
          'mesh_relay_original_operation_observe_only');
      }
      const instance: string = this.snapshot.node_id +
        (challenge.action === 'file.read.selected' ? ':selected-file-v1' : ':bytes-v1');
      requireValue(challenge.capability_id === instance, 'mesh_capability_instance_mismatch');
      const codec: util.Base64Helper = new util.Base64Helper();
      const input: Uint8Array = codec.decodeSync(challenge.input.base64);
      requireValue(input.length === challenge.input.bytes && codec.encodeToStringSync(input) === challenge.input.base64 &&
        await this.digest(input) === challenge.input.sha256, 'mesh_challenge_input_changed');
      record = beginOperation(this.snapshot.records, challenge, requestHash,
        Math.floor(Date.now() / 1000));
      if (relay !== null) { relay.challenge_state = 'consumed'; }
      await this.persist(); // Original ID/nonce/request committed BEFORE file access/execution.
      requireValue(this.foreground && this.sharingEnabled, 'mesh_local_permission_revoked');
      let output: Uint8Array = new Uint8Array(0);
      if (challenge.action === 'file.read.selected') {
        output = this.readSelectedComplete();
      } else {
        const repeat: number = input.length === 0 ? 1 : challenge.expected.bytes / input.length;
        requireValue(Number.isSafeInteger(repeat) && repeat >= 1 && repeat <= 64 &&
          input.length * repeat <= MAX_BYTES, 'mesh_recipe_budget_invalid');
        output = new Uint8Array(input.length * repeat);
        for (let index: number = 0; index < repeat; index++) { output.set(input, index * input.length); }
      }
      const result: Blob = { bytes: output.length, sha256: await this.digest(output), base64: codec.encodeToStringSync(output) };
      const envelope: ResultEnvelope = { format: 'mesh-enrollment-result/1', operation_id: challenge.operation_id,
        request_sha256: record.request_sha256, nonce: challenge.nonce, result: result,
        continuation: challenge.continuation, proxy: null };
      record.result_json = JSON.stringify(envelope);
      record.state = 'result_reported'; // Never self-verified/registered/native_execution_attested.
      await this.persist();
      return record.result_json;
    } catch (error) {
      if (record !== null && record.state === 'attempted') {
        record.state = 'unknown';
        await this.persist();
      }
      throw new Error('mesh_challenge_unavailable_original_identity_preserved');
    }
  }
  async bindRelay(raw: string): Promise<string> {
    requireValue(this.enrollment !== null && this.foreground && !this.busy && raw.length <= 65536,
      'mesh_relay_owner_config_required');
    this.busy = true;
    try {
      const value: RelayClientConfig = JSON.parse(raw) as RelayClientConfig;
      validateRelayClientConfig(value, this.enrollment as Enrollment, this.enrollmentHash);
      for (let i: number = 0; i < this.snapshot.relays.length; i++) {
        const old: RelayRecord = this.snapshot.relays[i];
        if (old.binding.operation_id === value.binding.operation_id) {
          requireValue(old.resource_url === value.resource_url &&
            canonicalRelayBinding(old.binding) === canonicalRelayBinding(value.binding), 'mesh_relay_original_operation_changed');
          return old.binding.operation_id; // Reimport never resets unknown/attempt.
        }
      }
      for (let i: number = 0; i < this.snapshot.records.length; i++) {
        const old: JournalRecord = this.snapshot.records[i];
        if (old.operation_id === value.binding.operation_id) {
          requireValue(old.request_sha256 === value.binding.request_sha256 && old.nonce === value.binding.nonce,
            'mesh_relay_original_operation_changed');
        }
      }
      requireValue(this.snapshot.relays.length < 64, 'mesh_relay_journal_capacity_reached');
      this.snapshot.relays.push(newRelayRecord(value));
      await this.persist();
      return value.binding.operation_id;
    } finally { this.busy = false; }
  }
  private relayRecord(operationId: string): RelayRecord {
    for (let i: number = 0; i < this.snapshot.relays.length; i++) {
      if (this.snapshot.relays[i].binding.operation_id === operationId) { return this.snapshot.relays[i]; }
    }
    throw new Error('mesh_relay_original_operation_not_found');
  }
  private relayReady(row: RelayRecord, scopedToken: string): void {
    requireValue(this.enrollment !== null && this.foreground, 'mesh_foreground_required');
    validateRelayUrl(row.resource_url);
    requireValue(deviceInfo.sdkApiVersion >= 23, 'mesh_no_redirect_transport_requires_api23');
    requireValue(/^[\x21-\x7e]{32,4096}$/.test(scopedToken), 'mesh_scoped_credential_required');
  }
  private async relayRequest(row: RelayRecord, scopedToken: string, action: string, argumentsJson: string): Promise<RelayResponse> {
    this.relayReady(row, scopedToken);
    requireValue(action === 'challenge' || action === 'result' || action === 'status', 'mesh_relay_action_invalid');
    // Recheck after async digest/flush: a revoked local sharing toggle must
    // prevent a fresh managed request even while the app stays foreground.
    if (action !== 'status') { requireValue(this.sharingEnabled, 'mesh_local_permission_revoked'); }
    const raw: string = '{"format":"mesh-enrollment-relay-request/1","binding":' + canonicalRelayBinding(row.binding) +
      ',"action":' + JSON.stringify(action) + ',"arguments":' + argumentsJson + '}';
    requireValue(raw.length <= 262144, 'mesh_relay_request_too_large');
    const transport: http.HttpRequest = http.createHttp();
    try {
      const response: http.HttpResponse = await transport.request(row.resource_url, {
        method: http.RequestMethod.POST,
        header: { 'Authorization': 'Bearer ' + scopedToken, 'Accept': 'application/json', 'Content-Type': 'application/json' },
        extraData: raw, expectDataType: http.HttpDataType.STRING, connectTimeout: 8000, readTimeout: 15000,
        usingCache: false, maxLimit: 262144, maxRedirects: 0
      });
      requireValue(response.responseCode === 200 && typeof response.result === 'string', 'mesh_relay_response_unconfirmed');
      const result: string = response.result as string;
      requireValue(result.length <= 262144, 'mesh_relay_response_too_large');
      const value: RelayResponse = JSON.parse(result) as RelayResponse;
      validateRelayResponse(value, row.binding, action);
      return value;
    } finally { transport.destroy(); } // Token lives only in this call's memory.
  }
  private async relayStatus(row: RelayRecord, scopedToken: string): Promise<string> {
    const value: RelayResponse = await this.relayRequest(row, scopedToken, 'status', '{}');
    observeRelayStatus(row, value, 'status');
    await this.persist();
    return JSON.stringify(value);
  }
  async queryRelayStatus(operationId: string, scopedToken: string): Promise<string> {
    requireValue(!this.busy, 'mesh_operation_busy');
    const row: RelayRecord = this.relayRecord(operationId);
    this.relayReady(row, scopedToken);
    this.busy = true;
    try { return await this.relayStatus(row, scopedToken); }
    finally { this.busy = false; }
  }
  private async relayResult(row: RelayRecord, scopedToken: string): Promise<string> {
    // An attempted/unknown POST is NEVER sent again, not even with the same ID
    // or bytes. Only query the original status/full-result hash thereafter.
    if (row.result_state !== 'not_submitted') { return this.relayStatus(row, scopedToken); }
    requireValue(this.sharingEnabled && this.foreground, 'mesh_local_owner_consent_required');
    let raw: string = '';
    for (let i: number = 0; i < this.snapshot.records.length; i++) {
      const local: JournalRecord = this.snapshot.records[i];
      if (local.operation_id === row.binding.operation_id) {
        requireValue(local.request_sha256 === row.binding.request_sha256 && local.nonce === row.binding.nonce,
          'mesh_relay_original_result_mismatch');
        raw = local.result_json;
      }
    }
    requireValue(raw.length > 0 && raw.length <= 131072, 'mesh_original_complete_result_required');
    const result: ResultEnvelope = JSON.parse(raw) as ResultEnvelope;
    validateResultEnvelope(result, row.binding, this.enrollment as Enrollment);
    const codec: util.Base64Helper = new util.Base64Helper();
    const bytes: Uint8Array = codec.decodeSync(result.result.base64);
    requireValue(bytes.length === result.result.bytes && codec.encodeToStringSync(bytes) === result.result.base64 &&
      await this.digest(bytes) === result.result.sha256, 'mesh_relay_original_result_bytes_changed');
    const canonical: string = canonicalResult(result);
    beginRelayResult(row, await this.digestText(canonical));
    await this.persist(); // Durable submission intent before the only result POST.
    try {
      const value: RelayResponse = await this.relayRequest(row, scopedToken, 'result', canonical);
      observeRelayStatus(row, value, 'result');
      requireValue(row.result_state === 'acknowledged', 'mesh_relay_original_result_not_confirmed');
      await this.persist();
      return JSON.stringify(value);
    } catch (error) {
      if (row.result_state === 'attempted') { row.result_state = 'unknown'; }
      await this.persist();
      throw new Error('mesh_relay_result_unconfirmed_observe_original_status_only');
    }
  }
  async submitRelayOriginalResult(operationId: string, scopedToken: string): Promise<string> {
    requireValue(!this.busy, 'mesh_operation_busy');
    const row: RelayRecord = this.relayRecord(operationId);
    this.relayReady(row, scopedToken);
    this.busy = true;
    try { return await this.relayResult(row, scopedToken); }
    finally { this.busy = false; }
  }
  async runRelay(operationId: string, scopedToken: string): Promise<string> {
    requireValue(!this.busy, 'mesh_operation_busy');
    const row: RelayRecord = this.relayRecord(operationId);
    this.relayReady(row, scopedToken);
    if (row.challenge_state !== 'not_requested') { return this.queryRelayStatus(operationId, scopedToken); }
    requireValue(this.sharingEnabled, 'mesh_local_owner_consent_required');
    this.busy = true;
    try {
      beginRelayChallenge(row);
      await this.persist(); // Before HTTP admission, not merely before local execution.
      const value: RelayResponse = await this.relayRequest(row, scopedToken, 'challenge', '{}');
      const challenge: Challenge = value.challenge as Challenge;
      const requestHash: string = await this.digestText(canonicalChallenge(challenge));
      acceptRelayChallenge(row, value, this.enrollment as Enrollment, this.enrollmentHash,
        requestHash, Math.floor(Date.now() / 1000));
      await this.persist();
      if (value.may_execute_original_challenge !== true) { return JSON.stringify(value); }
      await this.executeChallenge(row.challenge_json, row);
      return await this.relayResult(row, scopedToken); // Complete result, one original POST.
    } catch (error) {
      if (row.challenge_state === 'attempted' || row.challenge_state === 'granted') { row.challenge_state = 'unknown'; }
      await this.persist();
      throw new Error('mesh_relay_unconfirmed_original_identity_and_result_preserved');
    } finally { this.busy = false; }
  }
  originalResult(operationId: string): string {
    for (let index: number = 0; index < this.snapshot.records.length; index++) {
      if (this.snapshot.records[index].operation_id === operationId) {
        return JSON.stringify(this.snapshot.records[index]);
      }
    }
    throw new Error('mesh_original_operation_not_found');
  }
}
