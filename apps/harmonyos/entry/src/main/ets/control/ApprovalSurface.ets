import { BusinessError, deviceInfo } from '@kit.BasicServicesKit';
import { common, WantAgent } from '@kit.AbilityKit';
import { liveViewManager } from '@kit.LiveViewKit';
import { notificationManager } from '@kit.NotificationKit';
// NotificationContent in this real API26 SDK still declares the legacy enum.
import legacyNotification from '@ohos.notification';
import { ApprovalTicket } from './ControlSession';

export interface ApprovalActions {
  // Generated locally, immutable and bound to this ticket/nonce/decision.
  // A button is an owner intent only; its receiver must call ControlSession.decide.
  review: WantAgent;
  approve: WantAgent;
  deny: WantAgent;
}
export interface ApprovalPresentation {
  surface: string;
  presented: boolean;
  pending: boolean;
  liveview_error: string;
  notification_error: string;
}
export interface SurfaceAction {
  title: string;
  agent: WantAgent;
}
export interface SurfaceCard {
  title: string;
  text: string;
  detail: string;
  expiresAt: number;
  pending: boolean;
  silent: boolean;
  notificationFallback: boolean;
  open: WantAgent;
  buttons: SurfaceAction[];
  capsuleTitle?: string;
}
class ApprovalPlatformError extends Error {
  code: number;
  constructor(code: number) {
    super('phone_liveview_state_unconfirmed');
    this.code = Number.isSafeInteger(code) ? code : 0;
  }
}
function liveError(error: BusinessError): string {
  switch (error.code) {
    case 1003500004: return 'liveview_disabled';
    case 1003500005: return 'liveview_entitlement_required';
    case 801: return 'liveview_not_supported';
    default: return 'liveview_presentation_unconfirmed';
  }
}

// Presents the provider's current state. Decisions and deadlines belong to the provider.
export class ApprovalSurface {
  private static notificationContext: common.UIAbilityContext | null = null;
  private static notificationAsked: boolean = false;
  private id: number;
  private sequence: number = 0;
  private event: string;
  constructor(id: number, entitledEvent: string = '') {
    if (!Number.isSafeInteger(id) || id < 0 || id > 2147483647 ||
      (entitledEvent.length > 0 && !['TAXI', 'DELIVERY', 'FLIGHT', 'TRAIN', 'QUEUE', 'PICK_UP', 'SCORE', 'RENT',
        'TIMER', 'WORKOUT', 'NAVIGATION', 'CHECK_IN', 'EXPRESS', 'PROGRESS', 'TRADE'].includes(entitledEvent))) {
      throw new Error('phone_approval_surface_configuration_invalid');
    }
    this.id = id; this.event = entitledEvent;
  }

  static attachNotificationContext(context: common.UIAbilityContext | null): void {
    ApprovalSurface.notificationContext = context;
  }

  static async notificationEnabled(current: () => boolean): Promise<boolean> {
    if (await notificationManager.isNotificationEnabled()) { return true; }
    const context = ApprovalSurface.notificationContext;
    if (context === null || ApprovalSurface.notificationAsked || !current() ||
      AppStorage.get<boolean>('meshForeground') !== true) { return false; }
    ApprovalSurface.notificationAsked = true;
    try { await notificationManager.requestEnableNotification(context); }
    catch (_) { return false; }
    return await notificationManager.isNotificationEnabled();
  }
  private data(card: SurfaceCard): liveViewManager.LiveView {
    const buttons: liveViewManager.ServiceButton[] = card.buttons.map((action: SurfaceAction): liveViewManager.ServiceButton =>
      ({ name: action.title, clickAction: action.agent }));
    const data: liveViewManager.LiveViewData = { primary: {
      title: card.title, content: [{ text: card.text.slice(0, 320) }], keepTime: 0, clickAction: card.open,
      extensionData: { text: card.detail, type: liveViewManager.ExtensionType.EXTENSION_TYPE_COMMON_TEXT,
        clickAction: card.open, actionType: liveViewManager.ActionType.ACTION_TYPE_OPEN_APP_PAGE },
      layoutData: { layoutType: liveViewManager.LayoutType.LAYOUT_TYPE_DEFAULT,
        isServiceButtonsDisplayed: buttons.length > 0, serviceButtons: buttons }
    } };
    if (card.capsuleTitle !== undefined) {
      data.primary.aliveTime = Math.max(15, Math.min(28800, Math.ceil((card.expiresAt - Date.now()) / 1000)));
      data.capsule = { type: liveViewManager.CapsuleType.CAPSULE_TYPE_TEXT, status: 1, title: card.capsuleTitle,
        icon: 'amita_liveview_icon.png', backgroundColor: '#FF14644C' };
    }
    return { id: this.id, event: this.event, sequence: ++this.sequence, isMute: card.silent,
      lifeCycleMode: liveViewManager.LifeCycleMode.AUTO_STOP_WHEN_APP_TERMINATE,
      liveViewData: data };
  }
  async present(ticket: ApprovalTicket, actions: ApprovalActions): Promise<ApprovalPresentation> {
    if (ticket.expires_at <= Date.now() || ticket.summary.length === 0) {
      throw new Error('phone_approval_ticket_expired_or_invalid');
    }
    return this.presentCard({
      title: 'AMITA · 等待本人确认', text: ticket.summary, detail: '只允许这一步；可拒绝',
      expiresAt: ticket.expires_at, pending: true, silent: false, notificationFallback: true, open: actions.review,
      buttons: [{ title: ticket.review_required ? '查看完整动作' : '允许这一步',
        agent: ticket.review_required ? actions.review : actions.approve }, { title: '拒绝', agent: actions.deny }]
    });
  }
  private async activeView(): Promise<liveViewManager.LiveView | null> {
    try {
      const previous: liveViewManager.LiveView = await liveViewManager.getActiveLiveView(this.id);
      if (previous.sequence === undefined || !Number.isSafeInteger(previous.sequence) || previous.sequence >= 2147483647) {
        throw new Error('phone_liveview_sequence_unavailable');
      }
      this.sequence = Math.max(this.sequence, previous.sequence);
      return previous;
    } catch (error) {
      if ((error as BusinessError).code === 1003500009) { return null; }
      throw new ApprovalPlatformError((error as BusinessError).code);
    }
  }
  async presentCard(card: SurfaceCard, current: () => boolean = (): boolean => true): Promise<ApprovalPresentation> {
    const result: ApprovalPresentation = { surface: card.pending ? 'in_app_pending' : 'in_app_status',
      presented: false, pending: card.pending,
      liveview_error: '', notification_error: '' };
    if (!current() || card.expiresAt <= Date.now()) { return result; }
    if (card.capsuleTitle !== undefined && deviceInfo.sdkApiVersion < 26) {
      result.liveview_error = 'liveview_not_supported';
    } else if (this.event.length > 0) {
      try {
        if (!await liveViewManager.isLiveViewEnabled()) {
          result.liveview_error = 'liveview_disabled'; throw new Error('phone_liveview_disabled');
        }
        const previous: liveViewManager.LiveView | null = await this.activeView();
        if (!current() || card.expiresAt <= Date.now()) { return result; }
        const view: liveViewManager.LiveView = this.data(card);
        const outcome: liveViewManager.LiveViewResult = previous !== null ? await liveViewManager.updateLiveView(view) :
          await liveViewManager.startLiveView(view);
        if (outcome.resultCode !== 0) { throw new Error('phone_liveview_partial_failure'); }
        result.surface = 'liveview'; result.presented = true;
        return result;
      } catch (error) { if (result.liveview_error.length === 0) { result.liveview_error = liveError(error as BusinessError); } }
    } else { result.liveview_error = 'liveview_entitlement_not_configured'; }
    if (!card.notificationFallback || !current() || card.expiresAt <= Date.now()) { return result; }
    try {
      if (!await ApprovalSurface.notificationEnabled((): boolean => current() && card.expiresAt > Date.now())) {
        result.notification_error = 'notification_permission_required';
        return result;
      }
      if (!current() || card.expiresAt <= Date.now()) { return result; }
      const notification: notificationManager.NotificationRequest = { id: this.id, tapDismissed: false,
        deliveryTime: Date.now(), autoDeletedTime: card.expiresAt,
        wantAgent: card.open,
        content: { contentType: legacyNotification.ContentType.NOTIFICATION_CONTENT_BASIC_TEXT,
          normal: { title: card.title, text: card.text.slice(0, 320),
            additionalText: card.capsuleTitle === undefined ? '单次决定 · 到期失效 · 关闭通知不会批准' : card.detail } },
        actionButtons: card.buttons.map((action: SurfaceAction): notificationManager.NotificationActionButton =>
          ({ title: action.title, wantAgent: action.agent })) };
      await notificationManager.publish(notification);
      result.surface = 'notification'; result.presented = true;
    } catch (error) { result.notification_error = 'notification_presentation_unconfirmed'; }
    // Caller keeps the original durable ticket pending even if neither surface
    // works. It must expose an explicit approval UI, never autoapprove or drop it.
    return result;
  }
  async clearLiveView(): Promise<string> {
    if (this.event.length === 0) { return ''; }
    try {
      const previous: liveViewManager.LiveView | null = await this.activeView();
      if (previous === null) { return ''; }
      previous.sequence = ++this.sequence;
      previous.isMute = true;
      previous.liveViewData.primary.keepTime = 0;
      if (previous.liveViewData.capsule !== undefined) { previous.liveViewData.capsule.status = -1; }
      const result: liveViewManager.LiveViewResult = await liveViewManager.stopLiveView(previous);
      return result.resultCode === 0 ? '' : 'liveview_end_unconfirmed';
    } catch (error) {
      const code: number = (error as BusinessError).code;
      return code === 1003500009 || code === 1003500010 ? '' : 'liveview_end_unconfirmed';
    }
  }
  async clearNotification(): Promise<void> { await notificationManager.cancel(this.id); }
}
