import { UIAbility } from '@kit.AbilityKit';
import { window } from '@kit.ArkUI';

export default class EntryAbility extends UIAbility {
  onCreate(): void { AppStorage.setOrCreate<boolean>('meshForeground', false); }
  onWindowStageCreate(stage: window.WindowStage): void { stage.loadContent('pages/Index'); }
  onForeground(): void { AppStorage.setOrCreate<boolean>('meshForeground', true); }
  onBackground(): void { AppStorage.setOrCreate<boolean>('meshForeground', false); }
  onDestroy(): void { AppStorage.setOrCreate<boolean>('meshForeground', false); }
}
