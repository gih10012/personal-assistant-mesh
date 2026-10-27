import { common } from '@kit.AbilityKit';
import { fileIo } from '@kit.CoreFileKit';
import { util } from '@kit.ArkTS';
import { Artifact, MAX_ARTIFACT_BYTES, requireUpdate } from './Manifest';
import { digest } from './UpdateCrypto';

export class UpdateFiles {
  root: string;
  constructor(context: common.Context) {
    this.root = context.filesDir + '/amita-data-v1';
    if (!fileIo.accessSync(this.root)) { fileIo.mkdirSync(this.root, true); }
  }
  path(hash: string): string {
    requireUpdate(/^[a-f0-9]{64}$/.test(hash), 'update_hash_invalid');
    return this.root + '/' + hash;
  }
  read(path: string, expected: number): Uint8Array {
    requireUpdate(Number.isSafeInteger(expected) && expected >= 0 && expected <= MAX_ARTIFACT_BYTES &&
      path.startsWith(this.root + '/'), 'update_private_path_invalid');
    const file: fileIo.File = fileIo.openSync(path, fileIo.OpenMode.READ_ONLY);
    try {
      requireUpdate(fileIo.statSync(file.fd).size === expected, 'update_size_mismatch');
      const data: ArrayBuffer = new ArrayBuffer(expected);
      let read: number = 0;
      while (read < expected) {
        const block: ArrayBuffer = new ArrayBuffer(Math.min(expected - read, 262144));
        const length: number = fileIo.readSync(file.fd, block, { offset: read, length: block.byteLength });
        requireUpdate(length > 0, 'update_incomplete_read');
        new Uint8Array(data).set(new Uint8Array(block, 0, length), read);
        read += length;
      }
      requireUpdate(fileIo.statSync(file.fd).size === expected, 'update_file_changed');
      return new Uint8Array(data);
    } finally { fileIo.closeSync(file); }
  }
  async valid(item: Artifact): Promise<boolean> {
    if (!fileIo.accessSync(this.path(item.sha256))) { return false; }
    try { return await digest(this.read(this.path(item.sha256), item.size)) === item.sha256; }
    catch (_) { return false; }
  }
  async accept(item: Artifact, bytes: Uint8Array): Promise<void> {
    requireUpdate(bytes.length === item.size && await digest(bytes) === item.sha256, 'update_hash_or_size_mismatch');
    const destination: string = this.path(item.sha256);
    const temporary: string = destination + '.' + util.generateRandomUUID() + '.tmp';
    const file: fileIo.File = fileIo.openSync(temporary, fileIo.OpenMode.CREATE | fileIo.OpenMode.WRITE_ONLY | fileIo.OpenMode.TRUNC);
    try {
      const exact: ArrayBuffer = new ArrayBuffer(bytes.length);
      new Uint8Array(exact).set(bytes);
      requireUpdate(bytes.length === 0 || fileIo.writeSync(file.fd, exact) === bytes.length, 'update_incomplete_write');
      fileIo.fsyncSync(file.fd);
    } finally { fileIo.closeSync(file); }
    fileIo.renameSync(temporary, destination);
  }
  async acceptDownload(item: Artifact, path: string): Promise<void> {
    // A completed system transfer still needs full byte/hash verification.
    await this.accept(item, this.read(path, item.size));
  }
}
