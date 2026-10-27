import { common } from '@kit.AbilityKit';
import { workScheduler } from '@kit.BackgroundTasksKit';
import { UpdateDB } from '../update/UpdateDB';

export const UPDATE_WORK_ID: number = 3101;
export function updateWork(): workScheduler.WorkInfo {
  return { workId: UPDATE_WORK_ID, bundleName: 'org.personalassistant.mesh', abilityName: 'AmitaUpdateWork',
    isPersisted: true, networkType: workScheduler.NetworkType.NETWORK_TYPE_ANY,
    batteryStatus: workScheduler.BatteryStatus.BATTERY_STATUS_OKAY,
    storageRequest: workScheduler.StorageRequest.STORAGE_LEVEL_OKAY,
    isRepeat: true, repeatCycleTime: 4 * 60 * 60 * 1000 };
}
export async function scheduleUpdates(context: common.Context, enabled: boolean): Promise<void> {
  const db: UpdateDB = await UpdateDB.open(context);
  try {
    const works: workScheduler.WorkInfo[] = await workScheduler.obtainAllWorks();
    let present: boolean = false;
    for (let index: number = 0; index < works.length; index++) { if (works[index].workId === UPDATE_WORK_ID) { present = true; } }
    if (enabled && !present) { workScheduler.startWork(updateWork()); }
    if (!enabled && present) { workScheduler.stopWork(updateWork(), true); }
    await db.scheduled(enabled);
  } catch (_) { await db.scheduled(false); throw new Error('update_system_schedule_unavailable'); }
  finally { await db.close(); }
}
