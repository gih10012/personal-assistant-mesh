import { common } from '@kit.AbilityKit';
import { preferences } from '@kit.ArkData';
import { util } from '@kit.ArkTS';
import { ControlHost, ControlState, freshControlState } from './ControlSession';

// One instance in the process that owns the phone screen resource. UI and
// extension processes must send decisions to that owner, not open second loops.
export class ControlStore implements ControlHost {
  private store: preferences.Preferences;
  private nodeId: string;
  private saving: boolean = false;
  constructor(context: common.Context, nodeId: string) {
    freshControlState(nodeId); // validate without creating a replacement identity
    this.nodeId = nodeId;
    this.store = preferences.getPreferencesSync(context, { name: 'amita_control_v1' });
  }
  now(): number { return Date.now(); }
  id(): string { return util.generateRandomUUID(); }
  load(): ControlState {
    const raw: string = this.store.getSync('snapshot', '') as string;
    if (raw.length === 0) { return freshControlState(this.nodeId); }
    try {
      const value: ControlState = JSON.parse(raw) as ControlState;
      if (value.node_id !== this.nodeId || value.format !== 'amita-phone-control-state/1') {
        throw new Error('phone_control_original_identity_required');
      }
      return value; // ControlSession validates every original record before use.
    } catch (error) { throw new Error('phone_control_private_state_unavailable'); }
  }
  async save(value: ControlState): Promise<void> {
    if (this.saving || value.node_id !== this.nodeId || value.format !== 'amita-phone-control-state/1') {
      throw new Error('phone_control_single_writer_required');
    }
    this.saving = true;
    try {
      const raw: string = JSON.stringify(value);
      if (raw.length > 1048576) { throw new Error('phone_control_journal_budget_exceeded'); }
      this.store.putSync('snapshot', raw);
      await this.store.flush();
    } finally { this.saving = false; }
  }
}
