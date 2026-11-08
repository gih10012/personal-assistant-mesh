import { common } from '@kit.AbilityKit';
import { relationalStore } from '@kit.ArkData';

export class MaintenanceRun {
  id: string = '';
  trigger: string = '';
  started: number = 0;
  ended: number = 0;
  outcome: string = '';
  stopRequested: boolean = false;
  activeVersion: number = 0;
  pending: boolean = false;
  errorCode: number = 0;
}

// Separate maintenance observation, never the business/operation journal.
// An unfinished row survives process death as running/unknown, never success.
export class MaintenanceJournal {
  private db: relationalStore.RdbStore;
  private constructor(db: relationalStore.RdbStore) { this.db = db; }
  static async open(context: common.Context): Promise<MaintenanceJournal> {
    const db: relationalStore.RdbStore = await relationalStore.getRdbStore(context, {
      name: 'amita_maintenance_v1.db', securityLevel: relationalStore.SecurityLevel.S2
    });
    await db.executeSql('CREATE TABLE IF NOT EXISTS maintenance_runs (id TEXT PRIMARY KEY, trigger TEXT NOT NULL, started INTEGER NOT NULL, ended INTEGER NOT NULL DEFAULT 0, outcome TEXT NOT NULL, stop_requested INTEGER NOT NULL DEFAULT 0, active_version INTEGER NOT NULL DEFAULT 0, pending INTEGER NOT NULL DEFAULT 0, error_code INTEGER NOT NULL DEFAULT 0)');
    return new MaintenanceJournal(db);
  }
  async begin(id: string, trigger: string): Promise<void> {
    await this.db.executeSql('INSERT INTO maintenance_runs(id,trigger,started,outcome) VALUES(?,?,?,?)',
      [id, trigger, Date.now(), 'running_or_interrupted_unknown']);
  }
  async finish(id: string, outcome: string, stopped: boolean, version: number, pending: boolean, errorCode: number): Promise<void> {
    await this.db.executeSql('UPDATE maintenance_runs SET ended=?,outcome=?,stop_requested=?,active_version=?,pending=?,error_code=? WHERE id=? AND ended=0',
      [Date.now(), outcome, stopped ? 1 : 0, version, pending ? 1 : 0, errorCode, id]);
    // Only completed observation rows are bounded; unfinished/unknown survives.
    await this.db.executeSql('DELETE FROM maintenance_runs WHERE ended>0 AND id NOT IN (SELECT id FROM maintenance_runs WHERE ended>0 ORDER BY started DESC LIMIT 128)');
  }
  async recent(): Promise<MaintenanceRun[]> {
    const result: relationalStore.ResultSet = await this.db.querySql('SELECT * FROM maintenance_runs ORDER BY started DESC LIMIT 16');
    const runs: MaintenanceRun[] = [];
    try {
      while (result.goToNextRow()) {
        const run: MaintenanceRun = new MaintenanceRun();
        run.id = result.getString(result.getColumnIndex('id'));
        run.trigger = result.getString(result.getColumnIndex('trigger'));
        run.started = result.getLong(result.getColumnIndex('started'));
        run.ended = result.getLong(result.getColumnIndex('ended'));
        run.outcome = result.getString(result.getColumnIndex('outcome'));
        run.stopRequested = result.getLong(result.getColumnIndex('stop_requested')) === 1;
        run.activeVersion = result.getLong(result.getColumnIndex('active_version'));
        run.pending = result.getLong(result.getColumnIndex('pending')) === 1;
        run.errorCode = result.getLong(result.getColumnIndex('error_code'));
        runs.push(run);
      }
      return runs;
    } finally { result.close(); }
  }
  async close(): Promise<void> { await this.db.close(); }
}
