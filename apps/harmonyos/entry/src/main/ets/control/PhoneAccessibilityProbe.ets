import { AccessibilityElement, AccessibilityExtensionAbility } from '@kit.AccessibilityKit';
import { common } from '@kit.AbilityKit';
import { BusinessError } from '@kit.BasicServicesKit';
import { preferences } from '@kit.ArkData';
import { KeyEvent } from '@kit.InputKit';

export class AccessibilityProbeStatus {
  format: string = 'amita-accessibility-probe/1';
  connected: boolean = false;
  root_readable: boolean = false;
  self_bundle: boolean = false;
  self_root_role_readable: boolean = false;
  phase: string = 'not_observed';
  error_code: number = 0;
  checked_at: number = 0;
  actions_enabled: boolean = false;
}
export function readAccessibilityProbe(context: common.Context): AccessibilityProbeStatus {
  const store: preferences.Preferences = preferences.getPreferencesSync(context, { name: 'amita_control_probe_v1' });
  const raw: string = store.getSync('status', '') as string;
  if (raw.length === 0) { return new AccessibilityProbeStatus(); }
  try { return JSON.parse(raw) as AccessibilityProbeStatus; }
  catch (error) { const status: AccessibilityProbeStatus = new AccessibilityProbeStatus(); status.phase = 'private_state_unavailable'; return status; }
}

// A compatibility probe for public (deprecated since 12) APIs retained in the
// actual commercial API26 SDK. It requests NO system/ACL permission, never
// enables itself, injects input, or exports a remote action. Only the owner may
// enable this extension through the system's accessibility settings.
export default class PhoneAccessibilityProbe extends AccessibilityExtensionAbility {
  private status: AccessibilityProbeStatus = new AccessibilityProbeStatus();
  private async save(): Promise<void> {
    const store: preferences.Preferences = preferences.getPreferencesSync(this.context, { name: 'amita_control_probe_v1' });
    store.putSync('status', JSON.stringify(this.status));
    await store.flush();
  }
  onConnect(): void {
    this.status = new AccessibilityProbeStatus();
    this.status.connected = true;
    this.status.phase = 'connected_probe_pending';
    this.probe().catch((): void => {}); // No private content in logs.
  }
  private async probe(): Promise<void> {
    await this.save(); // A root-query crash must not be confused with no callback.
    try {
      const root: AccessibilityElement = await this.context.getWindowRootElement();
      this.status.root_readable = true;
      // Do not collect labels/text/children from whichever app is currently
      // active (normally system Settings while the owner enables this probe).
      const bundle: string = await root.attributeValue('bundleName');
      this.status.self_bundle = bundle === 'org.personalassistant.mesh';
      if (this.status.self_bundle) {
        const role: string = await root.attributeValue('componentType');
        this.status.self_root_role_readable = typeof role === 'string';
      }
      this.status.phase = 'read_only_probe_completed';
    } catch (error) {
      const code: number = (error as BusinessError).code;
      this.status.error_code = [201, 202, 401, 801, 9300003, 9300004].includes(code) ? code : 0;
      this.status.phase = 'read_only_probe_unavailable';
    }
    this.status.checked_at = Date.now();
    await this.save();
  }
  onDisconnect(): void {
    this.status.connected = false;
    this.status.phase = 'disconnected';
    this.status.checked_at = Date.now();
    this.save().catch((): void => {});
  }
  onKeyEvent(keyEvent: KeyEvent): boolean { return false; } // Always let real owner keys through.
}
