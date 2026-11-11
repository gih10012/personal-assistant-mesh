import { util } from '@kit.ArkTS';
import { SelfHdcClient, SelfHdcShellResult } from './SelfHdcClient';

export interface SelfHdcCapture {
  bytes: Uint8Array;
  mediaType: string;
}

export interface SelfHdcInput {
  action: string;
  x: number;
  y: number;
  toX: number;
  toY: number;
  width: number;
  height: number;
  velocity: number;
  text: string;
  key: string;
}

// Root must bind this to the existing live operation/approval/fence checks.
// A connected HDC session alone cannot provide either of these authorizations.
export interface SelfHdcInputGate {
  operationId: string;
  humanInputObservable: boolean;
  valid: () => boolean;
}

function requireSelfHdc(value: boolean, code: string): void {
  if (!value) { throw new Error(code); }
}

function coordinate(value: number, limit: number): string {
  requireSelfHdc(Number.isSafeInteger(limit) && limit > 0 && limit <= 16384 &&
    Number.isSafeInteger(value) && value >= 0 && value < limit, 'selfhdc_input_coordinates_invalid');
  return value.toString();
}

function quoteText(value: string): string {
  const bytes: Uint8Array = new util.TextEncoder().encodeInto(value);
  requireSelfHdc(bytes.length > 0 && bytes.length <= 1024 &&
    !/[\u0000-\u001f\u007f]/.test(value), 'selfhdc_input_text_invalid');
  // POSIX single-quote escaping: no substitutions, operators or options escape.
  return "'" + value.replace(/'/g, "'\\''") + "'";
}

// Finite operation vocabulary; never accepts a command supplied by Mesh/UI.
export function selfHdcInputCommand(input: SelfHdcInput): string {
  if (input.action === 'key') {
    requireSelfHdc(input.key === 'Back' || input.key === 'Home', 'selfhdc_input_key_invalid');
    return 'uitest uiInput keyEvent ' + input.key;
  }
  const x: string = coordinate(input.x, input.width);
  const y: string = coordinate(input.y, input.height);
  if (input.action === 'click' || input.action === 'doubleClick' || input.action === 'longClick') {
    return 'uitest uiInput ' + input.action + ' ' + x + ' ' + y;
  }
  if (input.action === 'swipe') {
    const toX: string = coordinate(input.toX, input.width);
    const toY: string = coordinate(input.toY, input.height);
    requireSelfHdc(Number.isSafeInteger(input.velocity) && input.velocity >= 200 && input.velocity <= 40000,
      'selfhdc_input_velocity_invalid');
    return 'uitest uiInput swipe ' + x + ' ' + y + ' ' + toX + ' ' + toY + ' ' + input.velocity;
  }
  if (input.action === 'inputText') {
    return 'uitest uiInput inputText ' + x + ' ' + y + ' ' + quoteText(input.text);
  }
  throw new Error('selfhdc_input_action_invalid');
}

export class SelfHdcDriver {
  private client: SelfHdcClient;
  private available: () => boolean;
  private busy: boolean = false;

  constructor(client: SelfHdcClient, available: () => boolean) {
    this.client = client;
    this.available = available;
  }

  private ensureAvailable(): void {
    requireSelfHdc(this.available() && this.client.status().authenticated, 'selfhdc_foreground_connection_required');
  }

  private async checked(command: string, maxBytes: number, timeoutMs: number): Promise<SelfHdcShellResult> {
    this.ensureAvailable();
    const result: SelfHdcShellResult = await this.client.shell(command, maxBytes, timeoutMs);
    this.ensureAvailable();
    requireSelfHdc(result.completed, result.dispatched ? 'selfhdc_execution_unknown' : 'selfhdc_not_dispatched');
    requireSelfHdc(result.exitCode !== undefined, 'selfhdc_exit_status_unknown');
    requireSelfHdc(result.exitCode === 0, 'selfhdc_command_failed_' + result.exitCode);
    requireSelfHdc(result.stdout.length <= maxBytes, 'selfhdc_result_budget_exceeded');
    return result;
  }

  private begin(): void {
    this.ensureAvailable();
    requireSelfHdc(!this.busy, 'selfhdc_probe_busy');
    this.busy = true;
  }

  async identity(): Promise<string> {
    this.begin();
    try {
      const result: SelfHdcShellResult = await this.checked('id', 4096, 5000);
      const text: string = new util.TextDecoder('utf-8', { fatal: true }).decodeToString(result.stdout).trim();
      requireSelfHdc(text.length > 0 && text.length <= 4096 && /^uid=\d+/.test(text), 'selfhdc_identity_unconfirmed');
      return text;
    } finally { this.busy = false; }
  }

  private temporary(extension: string): string {
    return '/data/local/tmp/amita_selfhdc_' + util.generateRandomUUID().replace(/-/g, '') + extension;
  }

  // Only our own unique temporary file is removed. EXIT traps run within the
  // same shell, including ordinary command errors; a killed daemon can still
  // leave a file behind, so failed/unknown execution never claims cleanup.
  private fileCommand(path: string, capture: boolean): string {
    const quoted: string = "'" + path + "'";
    const limit: number = capture ? 1048576 : 262144;
    const prepare: string = capture ? 'command -v base64 >/dev/null 2>&1 || exit 45; ' : '';
    const create: string = capture ? 'snapshot_display -f ' : 'uitest dumpLayout -p ';
    return 'p=' + quoted + '; trap \'rm -f -- "$p"\' EXIT; ' + prepare +
      create + '"$p" >/dev/null 2>&1 || exit 41; ' +
      'n=$(wc -c < "$p") || exit 42; ' +
      '[ "$n" -gt 0 ] && [ "$n" -le ' + limit + ' ] || exit 43; ' +
      (capture ? 'base64 "$p" || exit 44' : 'cat "$p" || exit 44');
  }

  async layout(): Promise<string> {
    this.begin();
    try {
      const result: SelfHdcShellResult = await this.checked(this.fileCommand(this.temporary('.json'), false), 262144, 15000);
      const text: string = new util.TextDecoder('utf-8', { fatal: true }).decodeToString(result.stdout).trim();
      requireSelfHdc(text.length > 1 && (text.startsWith('{') || text.startsWith('[')), 'selfhdc_layout_unconfirmed');
      // Validate completeness rather than presenting truncated JSON as a tree.
      JSON.parse(text);
      return text;
    } finally { this.busy = false; }
  }

  async screenshot(): Promise<SelfHdcCapture> {
    this.begin();
    try {
      const result: SelfHdcShellResult = await this.checked(this.fileCommand(this.temporary('.jpeg'), true), 1500000, 15000);
      const raw: string = new util.TextDecoder('utf-8', { fatal: true }).decodeToString(result.stdout);
      const encoded: string = raw.replace(/[\r\n\t ]/g, '');
      requireSelfHdc(encoded.length > 0 && encoded.length <= 1398104 &&
        /^[A-Za-z0-9+/]+={0,2}$/.test(encoded), 'selfhdc_capture_encoding_invalid');
      const codec: util.Base64Helper = new util.Base64Helper();
      const bytes: Uint8Array = codec.decodeSync(encoded);
      requireSelfHdc(bytes.length > 3 && bytes.length <= 1048576 &&
        codec.encodeToStringSync(bytes) === encoded, 'selfhdc_capture_encoding_invalid');
      requireSelfHdc(bytes[0] === 255 && bytes[1] === 216 && bytes[2] === 255 &&
        bytes[bytes.length - 2] === 255 && bytes[bytes.length - 1] === 217, 'selfhdc_capture_format_unconfirmed');
      return { bytes: bytes, mediaType: 'image/jpeg' };
    } finally { this.busy = false; }
  }

  // Not wired to the owner probe panel or any remote route in this delivery.
  // The guard must observe real human input and bind the original operation.
  async inputApproved(input: SelfHdcInput, gate: SelfHdcInputGate): Promise<SelfHdcShellResult> {
    this.begin();
    try {
      requireSelfHdc(gate.operationId.length > 0 && gate.humanInputObservable && gate.valid(),
        'selfhdc_input_approval_and_human_priority_required');
      const command: string = selfHdcInputCommand(input);
      requireSelfHdc(gate.valid(), 'selfhdc_input_approval_and_human_priority_required');
      const result: SelfHdcShellResult = await this.client.shell(command, 8192, 10000);
      // Once dispatched, a late revocation/disconnect is unknown, never retry.
      requireSelfHdc(result.completed && result.exitCode === 0 && gate.valid() && this.available(),
        result.dispatched ? 'selfhdc_execution_unknown' : 'selfhdc_not_dispatched');
      return result;
    } finally { this.busy = false; }
  }
}
