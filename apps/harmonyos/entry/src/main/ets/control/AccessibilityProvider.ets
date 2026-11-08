import {
  AccessibilityElement, AccessibilityEvent, AccessibilityExtensionContext, ElementAttributeKeys,
  GesturePath, GesturePoint, Rect
} from '@kit.AccessibilityKit';
import { BusinessError } from '@kit.BasicServicesKit';
import { display } from '@kit.ArkUI';
import { util } from '@kit.ArkTS';
import { KeyEvent } from '@kit.InputKit';
import {
  ControlAvailability, ControlInvocation, ControlProvider, ProviderElement, ProviderView
} from './ControlSession';

export interface AccessibilityProviderStatus {
  principal: string; connected: boolean; root_verified: boolean; owner_authorised: boolean;
  gesture_fallback_authorised: boolean; accessibility_events: number; key_events: number;
  input_observation: string; full_physical_touch_coverage: boolean; last_error: string; last_error_code: number;
}
class NodeFacts {
  componentId: number = -1;
  windowId: number = -1;
  role: string = '';
  label: string = '';
  password: boolean = true;
  enabled: boolean = false;
  visible: boolean = false;
  clickable: boolean = false;
  scrollable: boolean = false;
  rect: Rect = { left: 0, top: 0, width: 0, height: 0 };
  nativeActions: string[] = [];
}
interface NativeHandle { element: AccessibilityElement; facts: NodeFacts; }
interface PendingNode { element: AccessibilityElement; depth: number; }
function requireProvider(value: boolean, code: string): void { if (!value) { throw new Error(code); } }

// Uses only public APIs retained in the real API26 SDK (deprecated since 12).
// It neither enables the extension nor requests a system/ACL permission. Root
// forwards the actual extension lifecycle/events into this single owner object.
export class AccessibilityProvider implements ControlProvider {
  private nodeId: string;
  private context: AccessibilityExtensionContext | null = null;
  private connected: boolean = false;
  private rootVerified: boolean = false;
  private ownerAuthorised: boolean = false;
  private gestureAuthorised: boolean = false;
  private eventCount: number = 0;
  private keyCount: number = 0;
  private generation: number = 0;
  private viewToken: string = '';
  private bundle: string = '';
  private windowId: number = -1;
  private screenWidth: number = 0;
  private screenHeight: number = 0;
  private handles: Map<string, NativeHandle> = new Map();
  private busy: boolean = false;
  private lastError: string = 'accessibility_not_connected';
  private lastErrorCode: number = 0;
  private ownerActivity: (reason: string) => void;
  private admissionPolicy: () => boolean = (): boolean => true;

  constructor(nodeId: string, ownerActivity: (reason: string) => void) {
    requireProvider(/^[A-Za-z0-9._:-]{1,160}$/.test(nodeId), 'phone_accessibility_node_invalid');
    this.nodeId = nodeId; this.ownerActivity = ownerActivity;
  }
  principal(): string { return 'node:' + this.nodeId; }
  status(): AccessibilityProviderStatus {
    return { principal: this.principal(), connected: this.connected, root_verified: this.rootVerified,
      owner_authorised: this.ownerAuthorised, gesture_fallback_authorised: this.gestureAuthorised,
      accessibility_events: this.eventCount, key_events: this.keyCount,
      input_observation: 'all_delivered_accessibility_events_and_keys_conservative',
      full_physical_touch_coverage: false, last_error: this.lastError, last_error_code: this.lastErrorCode };
  }
  availability(): ControlAvailability {
    const inspect: boolean = this.connected && this.rootVerified;
    const callbacksObserved: boolean = this.eventCount + this.keyCount > 0;
    return { provider: 'harmony-accessibility-public-deprecated', inspect: inspect,
      act: inspect && this.ownerAuthorised && callbacksObserved && this.admissionPolicy(),
      // Means actual callbacks have been observed, not complete physical-touch
      // coverage. status() explicitly discloses the conservative limitation.
      human_input_observable: callbacksObserved,
      reason: !inspect ? this.lastError : !this.ownerAuthorised ? 'owner_control_authorisation_required' :
        !callbacksObserved ? 'accessibility_activity_callback_not_observed' : 'conservative_event_fence' };
  }
  private invalidate(reason: string): void {
    this.generation++; this.viewToken = ''; this.handles.clear();
    this.ownerActivity(reason);
  }
  private failure(code: string, error: BusinessError): void {
    this.lastError = code;
    this.lastErrorCode = [201, 202, 401, 801, 9300003, 9300004, 9300005].includes(error.code) ? error.code : 0;
  }
  async onConnect(context: AccessibilityExtensionContext): Promise<void> {
    this.context = context; this.connected = true; this.rootVerified = false;
    this.ownerAuthorised = false; this.gestureAuthorised = false; this.eventCount = 0; this.keyCount = 0;
    this.invalidate('accessibility_connected');
    try {
      const root: AccessibilityElement = await context.getWindowRootElement();
      await root.attributeValue('bundleName');
      await root.attributeValue('windowId');
      requireProvider(this.connected && this.context === context, 'phone_accessibility_disconnected');
      this.rootVerified = true; this.lastError = ''; this.lastErrorCode = 0;
    } catch (error) {
      this.rootVerified = false;
      this.failure('accessibility_root_probe_unavailable', error as BusinessError);
      throw new Error('phone_accessibility_root_probe_unavailable');
    }
  }
  onDisconnect(): void {
    this.connected = false; this.rootVerified = false; this.ownerAuthorised = false; this.gestureAuthorised = false;
    this.context = null; this.lastError = 'accessibility_disconnected'; this.lastErrorCode = 0;
    this.invalidate('accessibility_disconnected');
  }
  // Forward every received event, including events whose physical/automatic
  // origin is indistinguishable. Do not inspect or persist event text/extraInfo.
  onAccessibilityEvent(event: AccessibilityEvent): void {
    this.eventCount++;
    // A notification-content update is not a physical input event. Publishing
    // our approval notification must not itself invalidate its pending ticket.
    if (event.eventType !== 'notificationUpdate') { this.invalidate('accessibility_event'); }
  }
  noteOwnerDecisionKey(): void { this.keyCount++; } // Only after the runtime matches an exact live decision key.
  setAdmissionPolicy(policy: () => boolean): void { this.admissionPolicy = policy; }
  onKeyEvent(event: KeyEvent): boolean {
    this.keyCount++; this.invalidate('accessibility_key_event');
    return false; // Never intercept the owner's real keys.
  }
  setOwnerControlAllowed(value: boolean): void {
    requireProvider(!value || this.connected && this.rootVerified, 'phone_accessibility_owner_authorisation_unavailable');
    if (value === this.ownerAuthorised) { return; }
    this.ownerAuthorised = value;
    if (!value) { this.gestureAuthorised = false; this.invalidate('owner_control_revoked'); }
  }
  setGestureFallbackAllowed(value: boolean): void {
    requireProvider(!value || this.ownerAuthorised && this.rootVerified, 'phone_accessibility_gesture_authorisation_required');
    if (value === this.gestureAuthorised) { return; }
    this.gestureAuthorised = value;
    this.invalidate('gesture_policy_changed');
  }
  private current(epoch: number): void {
    requireProvider(this.connected && this.rootVerified && epoch === this.generation, 'phone_accessibility_observation_changed');
  }
  private async facts(element: AccessibilityElement): Promise<NodeFacts> {
    const result: NodeFacts = new NodeFacts();
    const names: ElementAttributeKeys[] = await element.attributeNames<ElementAttributeKeys>();
    requireProvider(['componentId', 'windowId', 'componentType', 'rect', 'isEnable', 'isVisible'].every(
      (name: string): boolean => names.some((key: ElementAttributeKeys): boolean => key === name)),
      'phone_accessibility_required_attributes_unavailable');
    await Promise.all([
      element.attributeValue('componentId').then((v: number): void => { result.componentId = v; }),
      element.attributeValue('windowId').then((v: number): void => { result.windowId = v; }),
      element.attributeValue('componentType').then((v: string): void => { result.role = v; }),
      element.attributeValue('rect').then((v: Rect): void => { result.rect = v; }),
      element.attributeValue('isEnable').then((v: boolean): void => { result.enabled = v; }),
      element.attributeValue('isVisible').then((v: boolean): void => { result.visible = v; }),
      element.actionNames().then((v: string[]): void => { result.nativeActions = v; })
    ]);
    if (names.includes('isPassword')) { result.password = await element.attributeValue('isPassword'); }
    if (names.includes('clickable')) { result.clickable = await element.attributeValue('clickable'); }
    if (names.includes('scrollable')) { result.scrollable = await element.attributeValue('scrollable'); }
    // Missing password information remains sensitive; never read its contents.
    if (!result.password) {
      if (names.includes('description')) { result.label = await element.attributeValue('description'); }
      if (result.label.length === 0 && names.includes('contents')) {
        const contents: string[] = await element.attributeValue('contents');
        result.label = contents.slice(0, 8).join(' ').slice(0, 1024);
      }
    }
    return result;
  }
  private semanticActions(facts: NodeFacts): string[] {
    if (!facts.enabled || !facts.visible || facts.password || facts.componentId < 0) { return []; }
    const result: string[] = [];
    if (facts.nativeActions.includes('click') || this.gestureAuthorised && facts.clickable) { result.push('tap'); }
    if (facts.nativeActions.includes('setText')) { result.push('set_text'); }
    if (facts.nativeActions.includes('scrollForward') || this.gestureAuthorised && facts.scrollable) { result.push('scroll_forward'); }
    if (facts.nativeActions.includes('scrollBackward') || this.gestureAuthorised && facts.scrollable) { result.push('scroll_backward'); }
    return result; // Current ControlSession accepts these four approved verbs.
  }
  async inspect(maxElements: number): Promise<ProviderView> {
    requireProvider(this.availability().inspect && !this.busy && Number.isSafeInteger(maxElements) &&
      maxElements > 0 && maxElements <= 128, 'phone_accessibility_inspect_unavailable');
    this.busy = true;
    const epoch: number = this.generation;
    this.handles.clear(); this.viewToken = '';
    try {
      const context: AccessibilityExtensionContext = this.context as AccessibilityExtensionContext;
      let root: AccessibilityElement;
      try { root = await context.getWindowRootElement(); }
      catch (error) {
        this.rootVerified = false; this.failure('accessibility_root_unavailable', error as BusinessError);
        throw new Error('phone_accessibility_root_unavailable');
      }
      const currentDisplay: display.Display = display.getDefaultDisplaySync();
      requireProvider(currentDisplay.id === 0 && currentDisplay.width > 0 && currentDisplay.height > 0,
        'phone_accessibility_primary_display_required');
      const bundle: string = await root.attributeValue('bundleName');
      const windowId: number = await root.attributeValue('windowId');
      this.current(epoch);
      const token: string = util.generateRandomUUID();
      const elements: ProviderElement[] = [];
      const pending: PendingNode[] = [{ element: root, depth: 0 }];
      const seen: Set<AccessibilityElement> = new Set();
      let visited: number = 0; let truncated: boolean = false;
      while (pending.length > 0 && elements.length < maxElements && visited < 256) {
        const next: PendingNode = pending.shift() as PendingNode;
        if (seen.has(next.element)) { continue; }
        seen.add(next.element); visited++;
        const facts: NodeFacts = await this.facts(next.element);
        this.current(epoch);
        const box: Rect = facts.rect;
        const left: number = Math.max(0, Math.floor(box.left));
        const top: number = Math.max(0, Math.floor(box.top));
        const right: number = Math.min(currentDisplay.width, Math.ceil(box.left + box.width));
        const bottom: number = Math.min(currentDisplay.height, Math.ceil(box.top + box.height));
        if (facts.visible && facts.windowId === windowId && right > left && bottom > top) {
          const key: string = 'native:' + visited + ':' + facts.componentId;
          elements.push({ key: key, role: facts.role.slice(0, 80), label: facts.password ? '[private]' : facts.label.slice(0, 1024),
            sensitive: facts.password, enabled: facts.enabled, actions: this.semanticActions(facts),
            bounds: { left: left, top: top, width: right - left, height: bottom - top } });
          this.handles.set(key, { element: next.element, facts: facts });
        }
        const children: AccessibilityElement[] = await next.element.attributeValue('children');
        this.current(epoch);
        if (next.depth >= 12) { truncated = truncated || children.length > 0; continue; }
        const budget: number = Math.max(0, Math.min(children.length, 256 - pending.length - visited));
        for (let i: number = 0; i < budget; i++) { pending.push({ element: children[i], depth: next.depth + 1 }); }
        truncated = truncated || budget < children.length;
      }
      this.current(epoch);
      this.bundle = bundle; this.windowId = windowId; this.screenWidth = currentDisplay.width;
      this.screenHeight = currentDisplay.height; this.viewToken = token;
      this.lastError = ''; this.lastErrorCode = 0;
      return { bundle_name: bundle, window_id: String(windowId), view_token: token,
        width: currentDisplay.width, height: currentDisplay.height, truncated: truncated || pending.length > 0,
        elements: elements };
    } catch (error) {
      this.handles.clear(); this.viewToken = '';
      if (this.rootVerified) { this.failure('accessibility_inspect_unconfirmed', error as BusinessError); }
      throw new Error('phone_accessibility_inspect_unconfirmed');
    } finally { this.busy = false; }
  }
  private nativeAction(action: string): string {
    switch (action) {
      case 'tap': return 'click';
      case 'set_text': return 'setText';
      case 'scroll_forward': return 'scrollForward';
      case 'scroll_backward': return 'scrollBackward';
      case 'focus': return 'focus'; // Dormant until the host approval protocol explicitly supports focus.
      case 'accessibility_focus': return 'accessibilityFocus';
      default: throw new Error('phone_accessibility_action_not_supported');
    }
  }
  private admission(invocation: ControlInvocation, epoch: number): void {
    this.current(epoch);
    requireProvider(this.availability().act && invocation.fence.valid && invocation.view_token === this.viewToken &&
      invocation.bundle_name === this.bundle && invocation.window_id === String(this.windowId),
      'phone_accessibility_action_revoked_or_stale');
  }
  async act(invocation: ControlInvocation): Promise<boolean> {
    requireProvider(!this.busy, 'phone_accessibility_busy');
    const epoch: number = this.generation;
    this.admission(invocation, epoch);
    const handle: NativeHandle | undefined = this.handles.get(invocation.element_key);
    requireProvider(handle !== undefined && typeof invocation.text === 'string' && invocation.text.length <= 4096,
      'phone_accessibility_element_unavailable');
    const native: NativeHandle = handle as NativeHandle;
    const action: string = this.nativeAction(invocation.action);
    this.busy = true;
    try {
      const context: AccessibilityExtensionContext = this.context as AccessibilityExtensionContext;
      let root: AccessibilityElement;
      try { root = await context.getWindowRootElement(); }
      catch (error) {
        this.rootVerified = false; this.failure('accessibility_root_unavailable', error as BusinessError);
        throw new Error('phone_accessibility_root_unavailable');
      }
      const bundle: string = await root.attributeValue('bundleName');
      const windowId: number = await root.attributeValue('windowId');
      const currentDisplay: display.Display = display.getDefaultDisplaySync();
      const facts: NodeFacts = await this.facts(native.element);
      this.admission(invocation, epoch);
      requireProvider(bundle === this.bundle && windowId === this.windowId && currentDisplay.id === 0 &&
        currentDisplay.width === this.screenWidth && currentDisplay.height === this.screenHeight &&
        facts.componentId === native.facts.componentId && facts.windowId === native.facts.windowId &&
        facts.role === native.facts.role && facts.label === native.facts.label && facts.enabled && facts.visible &&
        !facts.password && facts.componentId >= 0 && facts.rect.left === native.facts.rect.left &&
        facts.rect.top === native.facts.rect.top && facts.rect.width === native.facts.rect.width &&
        facts.rect.height === native.facts.rect.height,
        'phone_accessibility_target_changed');
      if (facts.nativeActions.includes(action)) {
        this.admission(invocation, epoch); // Last check before the only native effect.
        if (action === 'setText') { await native.element.performAction(action, { setText: invocation.text }); }
        else { await native.element.performAction(action); }
      } else {
        // Select fallback BEFORE the first effect. A thrown/uncertain semantic
        // action is never followed by a gesture or another attempt.
        requireProvider(this.gestureAuthorised && ((invocation.action === 'tap' && facts.clickable) ||
          ((invocation.action === 'scroll_forward' || invocation.action === 'scroll_backward') && facts.scrollable)),
          'phone_accessibility_native_action_unavailable');
        const box: Rect = facts.rect;
        requireProvider(box.left >= 0 && box.top >= 0 && box.width >= 4 && box.height >= 4 &&
          box.left + box.width <= this.screenWidth && box.top + box.height <= this.screenHeight,
          'phone_accessibility_gesture_geometry_unavailable');
        const x: number = Math.floor(box.left + box.width / 2);
        const y: number = Math.floor(box.top + box.height / 2);
        const path: GesturePath = new GesturePath(invocation.action === 'tap' ? 80 : 300);
        if (invocation.action === 'tap') { path.points = [new GesturePoint(x, y), new GesturePoint(x, y)]; }
        else {
          const upper: number = Math.floor(box.top + box.height * 0.2);
          const lower: number = Math.floor(box.top + box.height * 0.8);
          path.points = invocation.action === 'scroll_forward' ? [new GesturePoint(x, lower), new GesturePoint(x, upper)] :
            [new GesturePoint(x, upper), new GesturePoint(x, lower)];
        }
        this.admission(invocation, epoch);
        context.injectGestureSync(path);
      }
      return true; // Native input acknowledgement only; never verified business completion.
    } catch (error) {
      if (this.rootVerified) { this.failure('accessibility_action_unconfirmed_original_operation_preserved', error as BusinessError); }
      throw new Error('phone_accessibility_action_unconfirmed_original_operation_preserved');
    } finally { this.viewToken = ''; this.handles.clear(); this.busy = false; }
  }
}
