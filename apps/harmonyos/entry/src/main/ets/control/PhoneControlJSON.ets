import { util } from '@kit.ArkTS';
import { cryptoFramework } from '@kit.CryptoArchitectureKit';
import { Blob, requireValue } from '../core/Protocol';
import { validateWorkBlob } from '../core/WorkProtocol';

export interface PhoneControlInput {
  format: string;
  command: string;
  max_elements?: number;
  element_ref?: string;
  action?: string;
  text?: string;
}
export async function controlDigest(bytes: Uint8Array): Promise<string> {
  const md: cryptoFramework.Md = cryptoFramework.createMd('SHA256');
  await md.update({ data: bytes });
  const result: cryptoFramework.DataBlob = await md.digest();
  let hex: string = '';
  for (let i: number = 0; i < result.data.length; i++) { hex += result.data[i].toString(16).padStart(2, '0'); }
  return hex;
}
export async function controlBlob(value: Object): Promise<Blob> {
  const bytes: Uint8Array = new util.TextEncoder().encodeInto(JSON.stringify(value));
  requireValue(bytes.length > 0 && bytes.length <= 65536, 'phone_control_complete_json_result_budget_exceeded');
  const codec: util.Base64Helper = new util.Base64Helper();
  return { bytes: bytes.length, sha256: await controlDigest(bytes), base64: codec.encodeToStringSync(bytes) };
}
export async function decodeControlInput(value: Blob, kind: string): Promise<PhoneControlInput> {
  try {
    validateWorkBlob(value);
    const codec: util.Base64Helper = new util.Base64Helper();
    const bytes: Uint8Array = codec.decodeSync(value.base64);
    requireValue(bytes.length > 0 && bytes.length === value.bytes && bytes.length <= 65536 &&
      codec.encodeToStringSync(bytes) === value.base64 && await controlDigest(bytes) === value.sha256,
      'phone_control_original_input_changed');
    const raw: string = new util.TextDecoder('utf-8', { fatal: true }).decodeToString(bytes);
    const body: PhoneControlInput = JSON.parse(raw) as PhoneControlInput;
    requireValue(body !== null && typeof body === 'object' && !Array.isArray(body) &&
      body.format === 'amita-phone-ui-control/1', 'phone_control_input_contract_invalid');
    const fields: string = Object.keys(body).sort().join('|');
    if (kind === 'inspect') {
      requireValue(body.command === 'screenshot' && fields === 'command|format' ||
        body.command === 'inspect_ui' && fields === 'command|format|max_elements' &&
        Number.isSafeInteger(body.max_elements) && (body.max_elements as number) > 0 &&
        (body.max_elements as number) <= 64,
        'phone_control_inspect_input_invalid');
    } else {
      requireValue(kind === 'action' && body.command === 'perform_action' && fields === 'action|command|element_ref|format|text' &&
        typeof body.element_ref === 'string' && /^[A-Za-z0-9._:-]{1,180}$/.test(body.element_ref) &&
        ['tap', 'set_text', 'scroll_forward', 'scroll_backward'].includes(body.action as string) &&
        typeof body.text === 'string' && body.text.length <= 4096 && (body.action === 'set_text' || body.text === ''),
        'phone_control_action_input_invalid');
    }
    return body;
  } catch (error) { throw new Error('phone_control_input_unavailable_or_invalid'); }
}
