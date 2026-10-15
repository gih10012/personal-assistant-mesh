import { common } from '@kit.AbilityKit';
import { deviceInfo } from '@kit.BasicServicesKit';
import { preferences } from '@kit.ArkData';
import { buffer, util } from '@kit.ArkTS';
import { cryptoFramework } from '@kit.CryptoArchitectureKit';
import { fileIo, picker } from '@kit.CoreFileKit';
import { http } from '@kit.NetworkKit';
import {
  AdaptationReport, Blob, Challenge, DeviceState, Enrollment, Hello, JournalRecord, MAX_BYTES,
  ResultEnvelope, beginOperation, canonicalChallenge, canonicalEnrollment, coldResume,
  deviceState, exactByteView, report, requireValue, validateChallenge, validateEnrollment, validateHello, validateJoinUrl
} from '../core/Protocol';

export class NodeSnapshot {
  node_id: string = '';
  enrollment_json: string = '';
  records: JournalRecord[] = [];
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
    } else {
      // App-scoped random node identity, not IMEI/serial/MAC or hardware identity attestation.
      this.snapshot.node_id = 'harmony-' + util.generateRandomUUID();
    }
    if (this.snapshot.enrollment_json.length > 0) {
      const value: Enrollment = JSON.parse(this.snapshot.enrollment_json) as Enrollment;
      validateEnrollment(value, this.snapshot.node_id);
      this.enrollment = value;
      this.enrollmentHash = await this.digestText(canonicalEnrollment(value));
    }
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
    return this.digest(exactByteView(utf8.buffer, utf8.byteOffset, utf8.byteLength));
  }
  async executeApprovedChallenge(raw: string): Promise<string> {
    requireValue(this.enrollment !== null && this.sharingEnabled && this.foreground && !this.busy,
      'mesh_local_owner_consent_required');
    requireValue(raw.length <= 131072, 'mesh_challenge_too_large');
    this.busy = true;
    let record: JournalRecord | null = null;
    try {
      const challenge: Challenge = JSON.parse(raw) as Challenge;
      validateChallenge(challenge, this.enrollment as Enrollment, this.enrollmentHash, Math.floor(Date.now() / 1000));
      const instance: string = this.snapshot.node_id +
        (challenge.action === 'file.read.selected' ? ':selected-file-v1' : ':bytes-v1');
      requireValue(challenge.capability_id === instance, 'mesh_capability_instance_mismatch');
      const codec: util.Base64Helper = new util.Base64Helper();
      const input: Uint8Array = codec.decodeSync(challenge.input.base64);
      requireValue(input.length === challenge.input.bytes && codec.encodeToStringSync(input) === challenge.input.base64 &&
        await this.digest(input) === challenge.input.sha256, 'mesh_challenge_input_changed');
      record = beginOperation(this.snapshot.records, challenge, await this.digestText(canonicalChallenge(challenge)),
        Math.floor(Date.now() / 1000));
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
