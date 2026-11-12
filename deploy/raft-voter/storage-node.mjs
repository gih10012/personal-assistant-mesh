import { constants, closeSync, existsSync, fstatSync, fsyncSync, lstatSync, mkdirSync, openSync,
  readFileSync, renameSync, unlinkSync, writeFileSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
import { isAbsolute, join } from 'node:path';
import { need } from './codec.mjs';

function privateStat(path, directory = false) {
  const stat = lstatSync(path);
  need(!stat.isSymbolicLink() && (directory ? stat.isDirectory() : stat.isFile()) &&
    stat.uid === process.getuid() && (stat.mode & 0o077) === 0, 'private_owned_storage');
  return stat;
}
class LockedStore {
  constructor(directory) {
    need(isAbsolute(directory), 'absolute_private_storage');
    mkdirSync(directory, { recursive: true, mode: 0o700 }); privateStat(directory, true);
    this.directory = directory; this.lockPath = join(directory, 'voter.lock');
    // Never steal an old/unknown process lock. Root reconciles original owner.
    this.lock = openSync(this.lockPath, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600);
    writeFileSync(this.lock, JSON.stringify({ pid: process.pid, format: 'amita-raft-storage-lock/1' })); fsyncSync(this.lock);
  }
  close() {
    if (this.lock === null) return;
    const original = fstatSync(this.lock);
    if (existsSync(this.lockPath) && lstatSync(this.lockPath).ino === original.ino) unlinkSync(this.lockPath);
    closeSync(this.lock); this.lock = null;
  }
}
export class FileStore extends LockedStore {
  constructor(directory) { super(directory); this.path = join(directory, 'voter.json'); }
  async read() {
    if (!existsSync(this.path)) return null;
    need(privateStat(this.path).size <= 4 * 1024 * 1024, 'storage_size_budget');
    return JSON.parse(readFileSync(this.path, 'utf8'));
  }
  async save(value) {
    if (existsSync(this.path)) privateStat(this.path);
    const temporary = join(this.directory, `voter.${randomUUID()}.tmp`);
    const descriptor = openSync(temporary, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600);
    try { writeFileSync(descriptor, JSON.stringify(value)); fsyncSync(descriptor); }
    finally { closeSync(descriptor); }
    renameSync(temporary, this.path);
    const directory = openSync(this.directory, constants.O_RDONLY | constants.O_DIRECTORY);
    try { fsyncSync(directory); } finally { closeSync(directory); }
  }
}
export async function sqliteStore(directory) {
  const { DatabaseSync } = await import('node:sqlite');
  class SQLiteStore extends LockedStore {
    constructor() {
      super(directory); this.path = join(directory, 'voter.sqlite');
      if (existsSync(this.path)) privateStat(this.path);
      this.db = new DatabaseSync(this.path);
      // FULL commits synchronize the WAL before the driver can emit messages.
      this.db.exec('PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; CREATE TABLE IF NOT EXISTS raft_state (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)');
      privateStat(this.path);
    }
    async read() { const row = this.db.prepare('SELECT payload FROM raft_state WHERE id=1').get(); return row ? JSON.parse(row.payload) : null; }
    async save(value) {
      this.db.exec('BEGIN IMMEDIATE');
      try {
        this.db.prepare('INSERT INTO raft_state VALUES(1,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload').run(JSON.stringify(value));
        this.db.exec('COMMIT');
      } catch (error) { this.db.exec('ROLLBACK'); throw error; }
    }
    close() { this.db?.close(); super.close(); }
  }
  return new SQLiteStore();
}
