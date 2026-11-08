import { AccessibilityEvent, AccessibilityExtensionAbility } from '@kit.AccessibilityKit';
import { KeyEvent } from '@kit.InputKit';
import { preferences } from '@kit.ArkData';
import { PhoneControlBridge, PhoneControlRuntimeStatus } from './PhoneControlBridge';
import { PhoneControlRuntime } from './PhoneControlRuntime';

// Declared by Root as type=accessibility. Only the real OS lifecycle starts the
// runtime; no launch-on-UI, fake foreground, auto-enable, or background keeper.
export default class PhoneControlExtension extends AccessibilityExtensionAbility {
  private runtime: PhoneControlRuntime | null = null;
  private connected: boolean = false;
  private initialising: boolean = false;
  onConnect(): void {
    this.connected = true;
    if (this.initialising || this.runtime !== null) { return; }
    this.initialising = true;
    this.start().catch((): void => {});
  }
  private async start(): Promise<void> {
    try {
      const runtime: PhoneControlRuntime = await PhoneControlRuntime.create(this.context);
      if (!this.connected) { await runtime.onDisconnect(); return; }
      this.runtime = runtime;
      await runtime.onConnect(this.context);
    } catch (error) {
      let status: PhoneControlRuntimeStatus = new PhoneControlRuntimeStatus();
      try { status = PhoneControlBridge.status(this.context); } catch (readError) {}
      status.service_connected = this.connected;
      status.error = 'phone_control_extension_initialisation_unavailable';
      const store: preferences.Preferences = preferences.getPreferencesSync(this.context, { name: 'amita_control_status_v1' });
      store.putSync('status', JSON.stringify(status)); await store.flush();
    } finally { this.initialising = false; }
  }
  onAccessibilityEvent(event: AccessibilityEvent): void {
    if (this.runtime !== null) { this.runtime.onAccessibilityEvent(event); }
  }
  onKeyEvent(event: KeyEvent): boolean { return this.runtime === null ? false : this.runtime.onKeyEvent(event); }
  onDisconnect(): void {
    this.connected = false;
    const runtime: PhoneControlRuntime | null = this.runtime;
    this.runtime = null;
    if (runtime !== null) { runtime.onDisconnect().catch((): void => {}); }
  }
}
