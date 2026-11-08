import { common, Want } from '@kit.AbilityKit';
import { preferences } from '@kit.ArkData';
import { util } from '@kit.ArkTS';
import { ApprovalTicket, OwnerControlDecision } from './ControlSession';

export class PhoneControlOwnerOptions {
  enabled: boolean = false;
  gesture_fallback: boolean = false;
  screen_capture: boolean = false;
  liveview_event: string = '';
  grant_id: string = '';
}
export class PhoneControlRuntimeStatus {
  format: string = 'amita-phone-control-runtime/1';
  node_id: string = '';
  principal: string = '';
  control_session_id: string = '';
  configured: boolean = false;
  service_connected: boolean = false;
  root_verified: boolean = false;
  owner_enabled: boolean = false;
  connection: string = 'unconfigured';
  presence_expires_at: number = 0;
  pending_operation_id: string = '';
  pending_task_id: string = '';
  pending_task_epoch: number = 0;
  pending_expires_at: number = 0;
  pending_summary: string = '';
  pending_review_required: boolean = false;
  pending_revalidation: boolean = false;
  background_decision_subscribed: boolean = false;
  background_decision_callback_verified: boolean = false;
  background_agents_verified: boolean = false;
  background_decision_error: string = '';
  background_decision_publisher: string = '';
  approval_surface: string = '';
  tasks: number = 0;
  settled: number = 0;
  unknown: number = 0;
  error: string = '';
}
export interface PhoneControlPending {
  ticket: ApprovalTicket;
  task_epoch: number;
  input_json: string; // App-private review only, never included in public status/logs.
}
export interface PhoneControlReviewAcknowledgement {
  approval_id: string; nonce: string; operation_id: string; task_id: string;
  revision: number; expires_at: number; input_json: string;
}
export function freshControlPreferences(context: common.Context, name: string): preferences.Preferences {
  preferences.removePreferencesFromCacheSync(context, name);
  return preferences.getPreferencesSync(context, { name: name });
}
export function readPhoneControlOwner(context: common.Context): PhoneControlOwnerOptions {
  const store: preferences.Preferences = freshControlPreferences(context, 'amita_control_owner_v1');
  const raw: string = store.getSync('options', '') as string;
  if (raw.length === 0) { return new PhoneControlOwnerOptions(); }
  try {
    const value: PhoneControlOwnerOptions = JSON.parse(raw) as PhoneControlOwnerOptions;
    if (typeof value.enabled !== 'boolean' || typeof value.gesture_fallback !== 'boolean' ||
      typeof value.screen_capture !== 'boolean' || typeof value.liveview_event !== 'string' ||
      !/^[A-Za-z0-9-]{0,80}$/.test(value.grant_id) || value.enabled && value.grant_id.length === 0) { throw new Error('invalid'); }
    return value;
  } catch (error) { throw new Error('phone_control_owner_state_unavailable'); }
}

// UI-only narrow bridge. Separate files have one writer each: owner options /
// decision mailbox are UI-written; runtime status/pending are service-written.
// Do not register these local authority methods as remote Mesh tools.
export class PhoneControlBridge {
  static async configure(context: common.UIAbilityContext, options: PhoneControlOwnerOptions): Promise<void> {
    const old: PhoneControlOwnerOptions = readPhoneControlOwner(context);
    if (typeof options.enabled !== 'boolean' || typeof options.gesture_fallback !== 'boolean' ||
      typeof options.screen_capture !== 'boolean' || (options.liveview_event !== '' &&
        !['TAXI', 'DELIVERY', 'FLIGHT', 'TRAIN', 'QUEUE', 'PICK_UP', 'SCORE', 'RENT', 'TIMER', 'WORKOUT',
          'NAVIGATION', 'CHECK_IN', 'EXPRESS', 'PROGRESS', 'TRADE'].includes(options.liveview_event))) {
      throw new Error('phone_control_owner_configuration_invalid');
    }
    if (old.enabled === options.enabled && old.gesture_fallback === options.gesture_fallback &&
      old.screen_capture === options.screen_capture && old.liveview_event === options.liveview_event) { return; }
    const value: PhoneControlOwnerOptions = { enabled: options.enabled, gesture_fallback: options.gesture_fallback,
      screen_capture: options.screen_capture, liveview_event: options.liveview_event, grant_id: util.generateRandomUUID() };
    const store: preferences.Preferences = freshControlPreferences(context, 'amita_control_owner_v1');
    store.putSync('options', JSON.stringify(value)); await store.flush();
  }
  static async ownerToggle(context: common.UIAbilityContext, enabled: boolean): Promise<void> {
    const value: PhoneControlOwnerOptions = readPhoneControlOwner(context);
    value.enabled = enabled; await PhoneControlBridge.configure(context, value);
  }
  static status(context: common.Context): PhoneControlRuntimeStatus {
    const store: preferences.Preferences = freshControlPreferences(context, 'amita_control_status_v1');
    const raw: string = store.getSync('status', '') as string;
    if (raw.length === 0) { return new PhoneControlRuntimeStatus(); }
    try { return JSON.parse(raw) as PhoneControlRuntimeStatus; }
    catch (error) { throw new Error('phone_control_status_unavailable'); }
  }
  static pendingForOwner(context: common.UIAbilityContext): PhoneControlPending | null {
    const store: preferences.Preferences = freshControlPreferences(context, 'amita_control_status_v1');
    const raw: string = store.getSync('pending', '') as string;
    if (raw.length === 0) { return null; }
    try { return JSON.parse(raw) as PhoneControlPending; }
    catch (error) { throw new Error('phone_control_pending_unavailable'); }
  }
  static async decidePending(context: common.UIAbilityContext, decision: string): Promise<void> {
    const pending: PhoneControlPending | null = PhoneControlBridge.pendingForOwner(context);
    if (pending === null) { throw new Error('phone_control_no_pending_approval'); }
    const t: ApprovalTicket = pending.ticket;
    await PhoneControlBridge.postDecision(context, { approval_id: t.approval_id, nonce: t.nonce,
      operation_id: t.operation_id, task_id: t.task_id, revision: t.revision,
      expires_at: t.expires_at, decision: decision });
  }
  static async markPendingReviewed(context: common.UIAbilityContext): Promise<void> {
    const pending: PhoneControlPending | null = PhoneControlBridge.pendingForOwner(context);
    if (pending === null || pending.ticket.expires_at <= Date.now()) { throw new Error('phone_control_no_pending_review'); }
    const t: ApprovalTicket = pending.ticket;
    const ack: PhoneControlReviewAcknowledgement = { approval_id: t.approval_id, nonce: t.nonce,
      operation_id: t.operation_id, task_id: t.task_id, revision: t.revision,
      expires_at: t.expires_at, input_json: pending.input_json };
    const store: preferences.Preferences = freshControlPreferences(context, 'amita_control_decision_v1');
    store.putSync('review_ack', JSON.stringify(ack)); await store.flush();
    // This records reading the complete local body, never an execution decision.
  }
  static async postDecision(context: common.Context, value: OwnerControlDecision): Promise<void> {
    if (!['approve', 'deny'].includes(value.decision) || value.expires_at <= Date.now()) {
      throw new Error('phone_control_owner_decision_invalid');
    }
    const store: preferences.Preferences = freshControlPreferences(context, 'amita_control_decision_v1');
    store.putSync('decision', JSON.stringify(value)); await store.flush();
  }
  static async onNewWant(context: common.UIAbilityContext, want: Want): Promise<boolean> {
    const p: Record<string, Object> | undefined = want.parameters;
    if (p === undefined || typeof p['amita.control.approval_id'] !== 'string') { return false; }
    if (typeof p['amita.control.nonce'] !== 'string' || typeof p['amita.control.operation_id'] !== 'string' ||
      typeof p['amita.control.task_id'] !== 'string' || typeof p['amita.control.revision'] !== 'number' ||
      typeof p['amita.control.expires_at'] !== 'number' || typeof p['amita.control.decision'] !== 'string') {
      throw new Error('phone_control_owner_callback_invalid');
    }
    const value: OwnerControlDecision = { approval_id: p['amita.control.approval_id'] as string,
      nonce: p['amita.control.nonce'] as string, operation_id: p['amita.control.operation_id'] as string,
      task_id: p['amita.control.task_id'] as string, revision: p['amita.control.revision'] as number,
      expires_at: p['amita.control.expires_at'] as number, decision: p['amita.control.decision'] as string };
    if (want.action !== 'org.personalassistant.mesh.control.' + value.approval_id + '.' + value.decision) {
      throw new Error('phone_control_owner_callback_invalid');
    }
    if (value.decision !== 'review') { await PhoneControlBridge.postDecision(context, value); }
    return true; // Review opens the real local UI; it never grants a decision.
  }
}
