import { common } from '@kit.AbilityKit';
import { fileIo, fileUri, picker } from '@kit.CoreFileKit';
import { util } from '@kit.ArkTS';
import { CHAT_MAX_FILE_BYTES, ChatAttachment, ChatJournal, freshChatJournal } from './ChatState';

const JOURNAL_LIMIT: number = 32 * 1024 * 1024;

function chatId(id: string): boolean {
  return typeof id === 'string' && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(id);
}

function attachMime(name: string): string {
  const extension: string = name.split('.').pop()?.toLowerCase() || '';
  switch (extension) {
    case 'jpg':
    case 'jpeg': return 'image/jpeg';
    case 'png': return 'image/png';
    case 'webp': return 'image/webp';
    case 'gif': return 'image/gif';
    case 'pdf': return 'application/pdf';
    case 'txt': return 'text/plain';
    case 'mp3': return 'audio/mpeg';
    case 'mp4': return 'video/mp4';
    default: return 'application/octet-stream';
  }
}

export class PhoneChatStore {
  private context: common.UIAbilityContext;
  private root: string;
  private path: string;

  constructor(context: common.UIAbilityContext) {
    this.context = context;
    this.root = context.filesDir + '/amita-chat-v1';
    this.path = this.root + '/conversation.json';
    if (!fileIo.accessSync(this.root)) { fileIo.mkdirSync(this.root); }
  }

  load(nodeId: string): ChatJournal {
    if (!fileIo.accessSync(this.path)) { return freshChatJournal(nodeId, ''); }
    const source: fileIo.File = fileIo.openSync(this.path, fileIo.OpenMode.READ_ONLY);
    try {
      const size: number = fileIo.statSync(source.fd).size;
      if (size <= 0 || size > JOURNAL_LIMIT) { throw new Error('chat_journal_size'); }
      const bytes: Uint8Array = this.readFile(source.fd, 0, size);
      const raw: string = new util.TextDecoder('utf-8', { fatal: true }).decodeToString(bytes);
      const journal: ChatJournal = JSON.parse(raw) as ChatJournal;
      if (journal.node_id !== nodeId || !Array.isArray(journal.queue) || !Array.isArray(journal.events) ||
          !Number.isSafeInteger(journal.cursor) || !Array.isArray(journal.approvals) ||
          typeof journal.session_id !== 'string' || typeof journal.branch_id !== 'string') {
        throw new Error('chat_journal_binding');
      }
      return journal;
    } finally { fileIo.closeSync(source); }
  }

  save(journal: ChatJournal): void {
    const bytes: Uint8Array = new util.TextEncoder().encodeInto(JSON.stringify(journal));
    if (bytes.byteLength > JOURNAL_LIMIT) { throw new Error('chat_journal_full'); }
    this.writeAtomic(this.path, bytes);
  }

  async pick(count: number): Promise<ChatAttachment[]> {
    const options: picker.DocumentSelectOptions = new picker.DocumentSelectOptions();
    options.maxSelectNumber = count;
    const selector: picker.DocumentViewPicker = new picker.DocumentViewPicker(this.context);
    const urls: string[] = await selector.select(options);
    if (urls.length > count) { throw new Error('chat_attachment_count'); }
    const added: ChatAttachment[] = [];
    try {
      for (const uri of urls) {
        const source: fileIo.File = fileIo.openSync(uri, fileIo.OpenMode.READ_ONLY);
        try {
          const size: number = fileIo.statSync(source.fd).size;
          if (!Number.isSafeInteger(size) || size <= 0 || size > CHAT_MAX_FILE_BYTES) {
            throw new Error('chat_attachment_size');
          }
          const bytes: Uint8Array = this.readFile(source.fd, 0, size);
          if (fileIo.statSync(source.fd).size !== size) { throw new Error('chat_attachment_changed'); }
          const name: string = this.fileName(new fileUri.FileUri(uri).name);
          const attachment: ChatAttachment = { attachment_id: 'phone-file-' + util.generateRandomUUID(),
            name: name, mime: attachMime(name), bytes: size };
          this.writeAtomic(this.attachmentPath(attachment.attachment_id), bytes);
          added.push(attachment);
        } finally { fileIo.closeSync(source); }
      }
      return added;
    } catch (_) {
      for (const attachment of added) { this.remove(attachment.attachment_id); }
      throw new Error('chat_attachment_import_failed');
    }
  }

  readAttachment(attachment: ChatAttachment, offset: number, count: number): Uint8Array {
    const source: fileIo.File = fileIo.openSync(this.attachmentPath(attachment.attachment_id), fileIo.OpenMode.READ_ONLY);
    try {
      if (fileIo.statSync(source.fd).size !== attachment.bytes) { throw new Error('chat_attachment_changed'); }
      return this.readFile(source.fd, offset, count);
    } finally { fileIo.closeSync(source); }
  }

  hasAttachment(attachment: ChatAttachment): boolean {
    const path: string = this.attachmentPath(attachment.attachment_id);
    return fileIo.accessSync(path) && fileIo.statSync(path).size === attachment.bytes;
  }

  cacheAttachment(attachment: ChatAttachment, bytes: Uint8Array): void {
    if (bytes.length !== attachment.bytes) { throw new Error('chat_attachment_incomplete'); }
    this.writeAtomic(this.attachmentPath(attachment.attachment_id), bytes);
  }

  async exportAttachment(attachment: ChatAttachment): Promise<boolean> {
    const options: picker.DocumentSaveOptions = new picker.DocumentSaveOptions();
    options.newFileNames = [this.fileName(attachment.name)];
    const selector: picker.DocumentViewPicker = new picker.DocumentViewPicker(this.context);
    const destinations: string[] = await selector.save(options);
    if (destinations.length === 0) { return false; }
    const source: Uint8Array = this.readAttachment(attachment, 0, attachment.bytes);
    const file: fileIo.File = fileIo.openSync(destinations[0], fileIo.OpenMode.WRITE_ONLY | fileIo.OpenMode.TRUNC);
    try {
      this.writeFile(file.fd, source);
      fileIo.fsyncSync(file.fd);
    } finally { fileIo.closeSync(file); }
    return true;
  }

  remove(id: string): void {
    const path: string = this.attachmentPath(id);
    if (fileIo.accessSync(path)) { fileIo.unlinkSync(path); }
  }

  private fileName(raw: string): string {
    const safe: string = raw.replace(/[\\/\x00-\x1f\x7f]/g, '_').trim().slice(0, 80);
    return safe === '' || safe === '.' || safe === '..' ? '附件' : safe;
  }

  private attachmentPath(id: string): string {
    if (!chatId(id)) { throw new Error('chat_attachment_identity'); }
    return this.root + '/' + id + '.bin';
  }

  private readFile(fd: number, start: number, size: number): Uint8Array {
    const result: Uint8Array = new Uint8Array(size);
    let position: number = 0;
    while (position < size) {
      const block: ArrayBuffer = new ArrayBuffer(Math.min(size - position, 256 * 1024));
      const count: number = fileIo.readSync(fd, block, { offset: start + position, length: block.byteLength });
      if (count <= 0) { throw new Error('chat_file_incomplete'); }
      result.set(new Uint8Array(block, 0, count), position);
      position += count;
    }
    return result;
  }

  private writeFile(fd: number, bytes: Uint8Array): void {
    let position: number = 0;
    while (position < bytes.length) {
      const block: ArrayBuffer = new ArrayBuffer(Math.min(bytes.length - position, 256 * 1024));
      new Uint8Array(block).set(bytes.subarray(position, position + block.byteLength));
      const count: number = fileIo.writeSync(fd, block);
      if (count <= 0) { throw new Error('chat_file_incomplete'); }
      position += count;
    }
  }

  private writeAtomic(destination: string, bytes: Uint8Array): void {
    const temporary: string = destination + '.tmp';
    try {
      const file: fileIo.File = fileIo.openSync(temporary, fileIo.OpenMode.CREATE | fileIo.OpenMode.WRITE_ONLY |
        fileIo.OpenMode.TRUNC);
      try {
        this.writeFile(file.fd, bytes);
        fileIo.fsyncSync(file.fd);
      } finally { fileIo.closeSync(file); }
      fileIo.renameSync(temporary, destination);
    } finally {
      if (fileIo.accessSync(temporary)) { fileIo.unlinkSync(temporary); }
    }
  }
}
