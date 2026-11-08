import { BusinessError } from '@kit.BasicServicesKit';
import { WantAgent } from '@kit.AbilityKit';
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

// This class presents a committed pending ticket. It never grants approval,
// changes its deadline, starts work, or treats presentation as a human decision.
export class ApprovalSurface {
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
  private data(ticket: ApprovalTicket, actions: ApprovalActions): liveViewManager.LiveView {
    const buttons: liveViewManager.ServiceButton[] = ticket.review_required ? [
      { name: '查看完整动作', clickAction: actions.review }, { name: '拒绝', clickAction: actions.deny }
    ] : [{ name: '允许这一步', clickAction: actions.approve }, { name: '拒绝', clickAction: actions.deny }];
    return { id: this.id, event: this.event, sequence: ++this.sequence, isMute: false,
      lifeCycleMode: liveViewManager.LifeCycleMode.AUTO_STOP_WHEN_APP_TERMINATE,
      liveViewData: { primary: { title: 'AMITA · 等待本人确认', content: [{ text: ticket.summary.slice(0, 320) }],
        keepTime: 0, clickAction: actions.review,
        extensionData: { text: '只允许这一步；可拒绝', type: liveViewManager.ExtensionType.EXTENSION_TYPE_COMMON_TEXT,
          clickAction: actions.review, actionType: liveViewManager.ActionType.ACTION_TYPE_OPEN_APP_PAGE },
        layoutData: { layoutType: liveViewManager.LayoutType.LAYOUT_TYPE_DEFAULT,
          isServiceButtonsDisplayed: true, serviceButtons: buttons } } } };
  }
  async present(ticket: ApprovalTicket, actions: ApprovalActions): Promise<ApprovalPresentation> {
    if (ticket.expires_at <= Date.now() || ticket.summary.length === 0) {
      throw new Error('phone_approval_ticket_expired_or_invalid');
    }
    const result: ApprovalPresentation = { surface: 'in_app_pending', presented: false, pending: true,
      liveview_error: '', notification_error: '' };
    if (this.event.length > 0) {
      try {
        if (!await liveViewManager.isLiveViewEnabled()) {
          result.liveview_error = 'liveview_disabled'; throw new Error('phone_liveview_disabled');
        }
        // Reconcile the platform sequence; a previous instance may still exist.
        let active: boolean = false;
        try {
          const previous: liveViewManager.LiveView = await liveViewManager.getActiveLiveView(this.id);
          if (previous.sequence === undefined || !Number.isSafeInteger(previous.sequence) || previous.sequence >= 2147483647) {
            throw new Error('phone_liveview_sequence_unavailable');
          }
          this.sequence = Math.max(this.sequence, previous.sequence);
          active = true;
        } catch (error) {
          if ((error as BusinessError).code !== 1003500009) {
            throw new ApprovalPlatformError((error as BusinessError).code);
          }
        }
        const view: liveViewManager.LiveView = this.data(ticket, actions);
        const outcome: liveViewManager.LiveViewResult = active ? await liveViewManager.updateLiveView(view) :
          await liveViewManager.startLiveView(view);
        if (outcome.resultCode !== 0) { throw new Error('phone_liveview_partial_failure'); }
        result.surface = 'liveview'; result.presented = true;
        return result;
      } catch (error) { if (result.liveview_error.length === 0) { result.liveview_error = liveError(error as BusinessError); } }
    } else { result.liveview_error = 'liveview_entitlement_not_configured'; }
    try {
      if (!await notificationManager.isNotificationEnabled()) {
        result.notification_error = 'notification_permission_required';
        return result;
      }
      const notification: notificationManager.NotificationRequest = { id: this.id, tapDismissed: false,
        deliveryTime: Date.now(), autoDeletedTime: ticket.expires_at,
        wantAgent: actions.review,
        content: { contentType: legacyNotification.ContentType.NOTIFICATION_CONTENT_BASIC_TEXT,
          normal: { title: 'AMITA · 等待本人确认', text: ticket.summary.slice(0, 320),
            additionalText: '单次决定 · 到期失效 · 关闭通知不会批准' } },
        actionButtons: ticket.review_required ? [{ title: '查看完整动作', wantAgent: actions.review },
          { title: '拒绝', wantAgent: actions.deny }] : [{ title: '允许这一步', wantAgent: actions.approve },
          { title: '拒绝', wantAgent: actions.deny }] };
      await notificationManager.publish(notification);
      result.surface = 'notification'; result.presented = true;
    } catch (error) { result.notification_error = 'notification_presentation_unconfirmed'; }
    // Caller keeps the original durable ticket pending even if neither surface
    // works. It must expose an explicit approval UI, never autoapprove or drop it.
    return result;
  }
  async clearNotification(): Promise<void> { await notificationManager.cancel(this.id); }
}
