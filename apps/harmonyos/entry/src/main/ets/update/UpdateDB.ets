import { common } from '@kit.AbilityKit';
import { relationalStore } from '@kit.ArkData';
import { util } from '@kit.ArkTS';
import { DownloadRecord, requireUpdate } from './Manifest';

export class UpdateState {
  trust: string = '';
  active: number = 0;
  previous: number = 0;
  highest: number = 0;
  active_json: string = '';
  previous_json: string = '';
  pending: string = '';
  last_check: number = 0;
  last_success: number = 0;
  scheduled: number = 0;
  error: string = '';
  lease: string = '';
}

// Separate from the work/operation journal. Single-statement compare-and-set
// commits serialize foreground and Extension processes; no network in a DB transaction.
export class UpdateDB {
  private db: relationalStore.RdbStore;
  private constructor(db: relationalStore.RdbStore) { this.db = db; }
  static async open(context: common.Context): Promise<UpdateDB> {
    const db: relationalStore.RdbStore = await relationalStore.getRdbStore(context, {
      name: 'amita_updates_v1.db', securityLevel: relationalStore.SecurityLevel.S2
    });
    await db.executeSql('CREATE TABLE IF NOT EXISTS update_state (id INTEGER PRIMARY KEY CHECK(id=1), trust TEXT NOT NULL DEFAULT \'\', active INTEGER NOT NULL DEFAULT 0, previous INTEGER NOT NULL DEFAULT 0, highest INTEGER NOT NULL DEFAULT 0, active_json TEXT NOT NULL DEFAULT \'\', previous_json TEXT NOT NULL DEFAULT \'\', pending TEXT NOT NULL DEFAULT \'\', last_check INTEGER NOT NULL DEFAULT 0, last_success INTEGER NOT NULL DEFAULT 0, scheduled INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT \'\', lease TEXT NOT NULL DEFAULT \'\', lease_until INTEGER NOT NULL DEFAULT 0)');
    await db.executeSql('INSERT OR IGNORE INTO update_state(id) VALUES(1)');
    await db.executeSql('CREATE TABLE IF NOT EXISTS downloads (id TEXT PRIMARY KEY, record TEXT NOT NULL)');
    return new UpdateDB(db);
  }
  async state(): Promise<UpdateState> {
    const rows: relationalStore.ResultSet = await this.db.querySql('SELECT * FROM update_state WHERE id=1');
    try {
      requireUpdate(rows.goToFirstRow(), 'update_state_missing');
      const state: UpdateState = new UpdateState();
      state.trust = rows.getString(rows.getColumnIndex('trust'));
      state.active = rows.getLong(rows.getColumnIndex('active'));
      state.previous = rows.getLong(rows.getColumnIndex('previous'));
      state.highest = rows.getLong(rows.getColumnIndex('highest'));
      state.active_json = rows.getString(rows.getColumnIndex('active_json'));
      state.previous_json = rows.getString(rows.getColumnIndex('previous_json'));
      state.pending = rows.getString(rows.getColumnIndex('pending'));
      state.last_check = rows.getLong(rows.getColumnIndex('last_check'));
      state.last_success = rows.getLong(rows.getColumnIndex('last_success'));
      state.scheduled = rows.getLong(rows.getColumnIndex('scheduled'));
      state.error = rows.getString(rows.getColumnIndex('error'));
      state.lease = rows.getString(rows.getColumnIndex('lease'));
      return state;
    } finally { rows.close(); }
  }
  async acquire(): Promise<string> {
    const owner: string = util.generateRandomUUID();
    const now: number = Date.now();
    await this.db.executeSql('UPDATE update_state SET lease=?,lease_until=? WHERE id=1 AND lease_until<?', [owner, now + 110000, now]);
    requireUpdate((await this.state()).lease === owner, 'update_busy');
    return owner;
  }
  async release(owner: string): Promise<void> {
    await this.db.executeSql('UPDATE update_state SET lease=\'\',lease_until=0 WHERE id=1 AND lease=?', [owner]);
  }
  async configure(raw: string, owner: string): Promise<void> {
    await this.db.executeSql('UPDATE update_state SET trust=?,error=\'\' WHERE id=1 AND lease=?', [raw, owner]);
  }
  async checked(owner: string, error: string = ''): Promise<void> {
    await this.db.executeSql('UPDATE update_state SET last_check=?,error=? WHERE id=1 AND lease=?', [Date.now(), error, owner]);
  }
  async pending(raw: string, owner: string): Promise<void> {
    await this.db.executeSql('UPDATE update_state SET pending=? WHERE id=1 AND lease=?', [raw, owner]);
  }
  async activate(raw: string, version: number, owner: string): Promise<void> {
    await this.db.executeSql('UPDATE update_state SET previous=active,previous_json=active_json,active=?,active_json=?,highest=MAX(highest,?),pending=\'\',last_success=?,error=\'\' WHERE id=1 AND lease=? AND lease_until>? AND pending=? AND active<?',
      [version, raw, version, Date.now(), owner, Date.now(), raw, version]);
    requireUpdate((await this.state()).active === version, 'update_activation_not_committed');
  }
  async rollback(owner: string): Promise<void> {
    // highest is deliberately retained: the next network manifest cannot downgrade.
    await this.db.executeSql('UPDATE update_state SET active=previous,active_json=previous_json,previous=0,previous_json=\'\',pending=\'\',error=\'rolled_back\' WHERE id=1 AND lease=? AND lease_until>? AND previous>0', [owner, Date.now()]);
  }
  async scheduled(value: boolean): Promise<void> {
    await this.db.executeSql('UPDATE update_state SET scheduled=? WHERE id=1', [value ? 1 : 0]);
  }
  async downloads(): Promise<DownloadRecord[]> {
    const rows: relationalStore.ResultSet = await this.db.querySql('SELECT record FROM downloads ORDER BY id');
    const result: DownloadRecord[] = [];
    try {
      while (rows.goToNextRow()) { result.push(JSON.parse(rows.getString(0)) as DownloadRecord); }
      return result;
    } finally { rows.close(); }
  }
  async saveDownload(row: DownloadRecord): Promise<void> {
    await this.db.executeSql('INSERT OR REPLACE INTO downloads(id,record) VALUES(?,?)', [row.id, JSON.stringify(row)]);
  }
  async close(): Promise<void> { await this.db.close(); }
}
