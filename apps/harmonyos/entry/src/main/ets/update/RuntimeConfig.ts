import type { WorkConfig } from '../core/WorkProtocol';
import { validateWorkConfig } from '../core/WorkProtocol';

export interface RuntimeConfig {
  format: string; node_id: string; mesh_id: string; authority_id: string;
  session_id: string; resource_url: string; liveview_event: string;
}
export interface TrustedRuntimeValues { dataVersion: number; work: WorkConfig; liveviewEvent: string; }

// Pure validation; only TrustedRuntimeConfig may supply authenticated bytes.
// None of these fields grants local owner control, screenshot or permissions.
export function validateRuntimeConfig(value: RuntimeConfig, original: WorkConfig, version: number): TrustedRuntimeValues {
  validateWorkConfig(original, original.node_id);
  if (value === null || typeof value !== 'object' ||
    Object.keys(value).sort().join('|') !== 'authority_id|format|liveview_event|mesh_id|node_id|resource_url|session_id' ||
    value.format !== 'amita-trusted-runtime/1' || value.node_id !== original.node_id ||
    value.mesh_id !== original.mesh_id || value.authority_id !== original.authority_id ||
    value.session_id !== original.session_id || !Number.isSafeInteger(version) || version <= 0 ||
    typeof value.liveview_event !== 'string' || (value.liveview_event !== '' &&
    !['TAXI', 'DELIVERY', 'FLIGHT', 'TRAIN', 'QUEUE', 'PICK_UP', 'SCORE', 'RENT', 'TIMER', 'WORKOUT',
      'NAVIGATION', 'CHECK_IN', 'EXPRESS', 'PROGRESS', 'TRADE'].includes(value.liveview_event))) {
    throw new Error('update_runtime_original_identity_or_fields_required');
  }
  const work: WorkConfig = { format: original.format, node_id: original.node_id, mesh_id: original.mesh_id,
    authority_id: original.authority_id, session_id: original.session_id, resource_url: value.resource_url };
  validateWorkConfig(work, original.node_id);
  return { dataVersion: version, work: work, liveviewEvent: value.liveview_event };
}
