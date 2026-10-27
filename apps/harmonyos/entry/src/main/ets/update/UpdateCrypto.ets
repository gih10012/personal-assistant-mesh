import { cryptoFramework } from '@kit.CryptoArchitectureKit';
import { util } from '@kit.ArkTS';
import { DataManifest, ManifestEnvelope, UpdateTrust, requireUpdate, validateManifest } from './Manifest';

export function decode64(raw: string): Uint8Array {
  requireUpdate(typeof raw === 'string' && /^[A-Za-z0-9+/]*={0,2}$/.test(raw), 'update_base64_invalid');
  const codec: util.Base64Helper = new util.Base64Helper();
  const bytes: Uint8Array = codec.decodeSync(raw);
  requireUpdate(codec.encodeToStringSync(bytes) === raw, 'update_base64_noncanonical');
  return bytes;
}
export async function digest(bytes: Uint8Array): Promise<string> {
  const md: cryptoFramework.Md = cryptoFramework.createMd('SHA256');
  await md.update({ data: bytes });
  const value: cryptoFramework.DataBlob = await md.digest();
  let result: string = '';
  for (let index: number = 0; index < value.data.length; index++) { result += value.data[index].toString(16).padStart(2, '0'); }
  return result;
}
export async function verifyEnvelope(raw: string, trust: UpdateTrust, highest: number, installedLocal: boolean = false): Promise<DataManifest> {
  requireUpdate(raw.length <= 65536, 'update_manifest_too_large');
  const envelope: ManifestEnvelope = JSON.parse(raw) as ManifestEnvelope;
  requireUpdate(envelope.format === 'amita-data-manifest-envelope/1' && envelope.key_id === trust.key_id &&
    envelope.algorithm === 'RSA-SHA256', 'update_signature_envelope_invalid');
  const bytes: Uint8Array = decode64(envelope.payload);
  const signature: Uint8Array = decode64(envelope.signature);
  requireUpdate(bytes.length <= 32768 && signature.length === 256, 'update_signature_size_invalid');
  const generator: cryptoFramework.AsyKeyGenerator = cryptoFramework.createAsyKeyGenerator('RSA2048');
  const keys: cryptoFramework.KeyPair = await generator.convertKey({ data: decode64(trust.public_key_spki_base64) }, null);
  const verifier: cryptoFramework.Verify = cryptoFramework.createVerify('RSA2048|PKCS1|SHA256');
  await verifier.init(keys.pubKey);
  requireUpdate(await verifier.verify({ data: bytes }, { data: signature }), 'update_signature_invalid');
  const value: DataManifest = JSON.parse(new util.TextDecoder('utf-8', { fatal: true }).decodeToString(bytes)) as DataManifest;
  validateManifest(value, trust, highest, installedLocal);
  return value;
}
