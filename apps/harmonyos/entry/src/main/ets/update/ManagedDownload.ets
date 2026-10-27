import { common } from '@kit.AbilityKit';
import { request } from '@kit.BasicServicesKit';
import { NodeCredential } from '../security/NodeCredentialStore';
import { Artifact, DataManifest, DownloadRecord, requireUpdate } from './Manifest';
import { UpdateDB } from './UpdateDB';
import { UpdateFiles } from './UpdateFiles';

// Instantiate only on the UIAbility main thread while foreground. The system
// owns a started transfer; no JS daemon or WorkScheduler request.agent use.
export class ManagedDownload {
  private context: common.UIAbilityContext;
  private foreground: () => boolean;
  constructor(context: common.UIAbilityContext, foreground: () => boolean) {
    this.context = context; this.foreground = foreground;
  }
  private ready(): void { requireUpdate(this.foreground(), 'update_foreground_required'); }
  private bound(task: request.agent.Task, size: number): void {
    task.on('progress', (progress: request.agent.Progress): void => {
      if (progress.processed > size || progress.sizes.some((value: number): boolean => value > size)) {
        task.stop().catch((): void => {});
      }
    });
  }
  private find(records: DownloadRecord[], id: string): DownloadRecord | null {
    for (let index: number = 0; index < records.length; index++) { if (records[index].id === id) { return records[index]; } }
    return null;
  }
  async reconcile(db: UpdateDB, manifest: DataManifest, credential: NodeCredential): Promise<void> {
    this.ready();
    const records: DownloadRecord[] = await db.downloads();
    const files: UpdateFiles = new UpdateFiles(this.context);
    for (let index: number = 0; index < manifest.artifacts.length; index++) {
      this.ready();
      const item: Artifact = manifest.artifacts[index];
      if (await files.valid(item)) { continue; }
      if (item.size === 0) { await files.accept(item, new Uint8Array(0)); continue; }
      const id: string = 'amita-data-' + manifest.version.toString() + '-' + item.sha256;
      let row: DownloadRecord | null = this.find(records, id);
      if (row === null) {
        row = { id: id, version: manifest.version, sha256: item.sha256, file: files.path(item.sha256) + '.download', tid: '', state: 'creating' };
        // Intent precedes task creation. A crash here cannot trigger duplicate creation.
        await db.saveDownload(row);
        records.push(row);
        this.ready();
        try {
          const task: request.agent.Task = await request.agent.create(this.context, {
            action: request.agent.Action.DOWNLOAD, mode: request.agent.Mode.BACKGROUND,
            method: 'GET', url: item.url, headers: { 'Authorization': 'Bearer ' + credential.token },
            saveas: row.file, title: id, description: 'AMITA application data',
            overwrite: false, redirect: false, retry: false, network: request.agent.Network.ANY,
            roaming: false, gauge: true, precise: true,
            // Fetch at most the signed byte range; full file hash/size is still mandatory.
            begins: 0, ends: Math.max(0, item.size - 1),
            timeout: { connectionTimeout: 15, totalTimeout: 600 }
          });
          row.tid = task.tid; row.state = 'created';
          await db.saveDownload(row); // Original tid durable BEFORE start.
          this.bound(task, item.size);
          this.ready();
          await task.start();
          row.state = 'running'; await db.saveDownload(row);
        } catch (_) {
          row.state = 'unknown'; await db.saveDownload(row);
        }
        continue;
      }
      if (row.state === 'verified') { continue; }
      try {
        if (row.tid.length === 0) {
          // Reconcile a create whose reply/persistence was interrupted by title,
          // without guessing a new task ID or replaying creation.
          const tids: string[] = await request.agent.search({ action: request.agent.Action.DOWNLOAD });
          const matches: string[] = [];
          for (let t: number = 0; t < tids.length && t < 256; t++) {
            const info: request.agent.TaskInfo = await request.agent.show(tids[t]);
            if (info.title === row.id) { matches.push(info.tid); }
          }
          requireUpdate(matches.length === 1, 'update_original_download_unresolved');
          row.tid = matches[0]; await db.saveDownload(row);
        }
        const info: request.agent.TaskInfo = await request.agent.show(row.tid);
        requireUpdate(info.title === row.id && info.action === request.agent.Action.DOWNLOAD &&
          (info.url === undefined || info.url === item.url) &&
          (info.saveas === undefined || info.saveas === row.file), 'update_original_download_changed');
        if (info.progress.state === request.agent.State.COMPLETED) {
          await files.acceptDownload(item, row.file);
          row.state = 'verified';
        } else if (info.progress.state === request.agent.State.PAUSED) {
          this.ready();
          const task: request.agent.Task = await request.agent.getTask(this.context, row.tid);
          this.bound(task, item.size);
          await task.resume(); row.state = 'running';
        } else if (info.progress.state === request.agent.State.INITIALIZED) {
          this.ready();
          const task: request.agent.Task = await request.agent.getTask(this.context, row.tid);
          this.bound(task, item.size);
          await task.start(); row.state = 'running';
        } else if (info.progress.state === request.agent.State.FAILED || info.progress.state === request.agent.State.REMOVED ||
          info.progress.state === request.agent.State.STOPPED) { row.state = 'failed'; }
        else { row.state = 'running'; }
      } catch (_) { row.state = 'unknown'; }
      await db.saveDownload(row);
    }
  }
}
