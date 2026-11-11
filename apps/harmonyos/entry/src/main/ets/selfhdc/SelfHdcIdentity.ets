// SPDX-License-Identifier: Apache-2.0
import { common } from '@kit.AbilityKit';
import { cryptoFramework } from '@kit.CryptoArchitectureKit';
import { fileIo } from '@kit.CoreFileKit';
import { JSON as ArkJSON, util } from '@kit.ArkTS';
import { hdcRequire, hdcText, hdcUtf8 } from './SelfHdcProtocol';

interface IdentityRecord { format: number; publicDer: string; privateDer: string; }

// No device/PC identity, certificates or authentication material leave this class.
export class SelfHdcIdentity {
  private static loading: Promise<SelfHdcIdentity> | undefined = undefined;
  private static directory: string = '';
  private key: cryptoFramework.KeyPair;
  readonly publicPem: string;

  private constructor(key: cryptoFramework.KeyPair) {
    this.key = key;
    const encoded = new util.Base64Helper().encodeToStringSync(key.pubKey.getEncoded().data);
    const lines: string[] = [];
    for (let offset = 0; offset < encoded.length; offset += 64) { lines.push(encoded.slice(offset, offset + 64)); }
    this.publicPem = '-----BEGIN PUBLIC KEY-----\n' + lines.join('\n') + '\n-----END PUBLIC KEY-----\n';
  }

  static load(context: common.Context): Promise<SelfHdcIdentity> {
    const root = context.filesDir + '/amita-selfhdc-v1';
    if (SelfHdcIdentity.loading !== undefined) {
      hdcRequire(root === SelfHdcIdentity.directory, 'selfhdc_identity_context_changed');
      return SelfHdcIdentity.loading;
    }
    SelfHdcIdentity.directory = root;
    SelfHdcIdentity.loading = SelfHdcIdentity.readOrCreate(root);
    // A corrupt/failed key stays an explicit error: do not silently generate another trusted identity.
    return SelfHdcIdentity.loading;
  }

  private static async readOrCreate(root: string): Promise<SelfHdcIdentity> {
    if (!fileIo.accessSync(root)) { fileIo.mkdirSync(root, true); }
    const path = root + '/identity.json';
    const generator = cryptoFramework.createAsyKeyGenerator('RSA3072');
    const base64 = new util.Base64Helper();
    if (fileIo.accessSync(path)) {
      const file = fileIo.openSync(path, fileIo.OpenMode.READ_ONLY);
      let bytes = new Uint8Array(0);
      try {
        const length = fileIo.statSync(file.fd).size;
        hdcRequire(length > 0 && length <= 16384, 'selfhdc_identity_size');
        const buffer = new ArrayBuffer(length);
        hdcRequire(fileIo.readSync(file.fd, buffer) === length, 'selfhdc_identity_read');
        bytes = new Uint8Array(buffer);
      } finally { fileIo.closeSync(file); }
      const record = ArkJSON.parse(hdcText(bytes)) as IdentityRecord;
      hdcRequire(record.format === 1 && typeof record.publicDer === 'string' &&
        typeof record.privateDer === 'string', 'selfhdc_identity_invalid');
      const pair = await generator.convertKey({ data: base64.decodeSync(record.publicDer) },
        { data: base64.decodeSync(record.privateDer) });
      return new SelfHdcIdentity(pair);
    }
    const pair = await generator.generateKeyPair();
    const record: IdentityRecord = { format: 1,
      publicDer: base64.encodeToStringSync(pair.pubKey.getEncoded().data),
      privateDer: base64.encodeToStringSync(pair.priKey.getEncoded().data) };
    const bytes = hdcUtf8(ArkJSON.stringify(record));
    const temporary = path + '.new';
    const file = fileIo.openSync(temporary,
      fileIo.OpenMode.CREATE | fileIo.OpenMode.WRITE_ONLY | fileIo.OpenMode.TRUNC);
    try {
      hdcRequire(fileIo.writeSync(file.fd, bytes.buffer) === bytes.length, 'selfhdc_identity_write');
      fileIo.fsyncSync(file.fd);
    } finally { fileIo.closeSync(file); }
    // Atomic publication before any public key is sent. App filesDir supplies the sandbox boundary.
    fileIo.renameSync(temporary, path);
    return new SelfHdcIdentity(pair);
  }

  async sign(challenge: Uint8Array, modern: boolean): Promise<string> {
    hdcRequire(challenge.length > 0 && challenge.length <= (modern ? 1024 : 373), 'selfhdc_challenge_size');
    const signer = cryptoFramework.createSign(modern ?
      'RSA3072|PSS|SHA512|MGF1_SHA512' : 'RSA3072|PKCS1|NoHash|OnlySign');
    await signer.init(this.key.priKey);
    if (modern) { signer.setSignSpec(cryptoFramework.SignSpecItem.PSS_SALT_LEN_NUM, 64); }
    const result = await signer.sign({ data: challenge });
    return new util.Base64Helper().encodeToStringSync(result.data);
  }

  static async randomBytes(length: number): Promise<Uint8Array> {
    return (await cryptoFramework.createRandom().generateRandom(length)).data;
  }
}
