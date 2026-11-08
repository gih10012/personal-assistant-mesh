import { common } from '@kit.AbilityKit';
import { WorkConfig, validateWorkConfig } from '../core/WorkProtocol';
import { InstalledConfig, readInstalledConfig } from './InstalledConfig';
import { RuntimeConfig, TrustedRuntimeValues, validateRuntimeConfig } from './RuntimeConfig';

export class TrustedRuntimeConfig {
  private context: common.Context;
  constructor(context: common.Context) { this.context = context; }
  async load(original: WorkConfig): Promise<TrustedRuntimeValues | null> {
    validateWorkConfig(original, original.node_id);
    const installed: InstalledConfig | null = await readInstalledConfig(this.context, original.node_id, 'config/runtime.json');
    if (installed === null) { return null; }
    return validateRuntimeConfig(JSON.parse(installed.raw) as RuntimeConfig, original, installed.dataVersion);
  }
}
