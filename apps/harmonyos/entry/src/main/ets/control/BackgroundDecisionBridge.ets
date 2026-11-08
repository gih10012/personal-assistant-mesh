import { BusinessError, commonEventManager } from '@kit.BasicServicesKit';
import { OwnerControlDecision } from './ControlSession';
import { CONTROL_BUNDLE, CONTROL_DECISION_EVENT, CONTROL_DECISION_PAYLOAD, decodeBackgroundDecision } from './BackgroundDecisionProtocol';

export interface BackgroundDecisionStatus {
  subscribed: boolean; callback_verified: boolean; observed_publisher: string; error: string;
}
export class BackgroundDecisionBridge {
  private subscriber: commonEventManager.CommonEventSubscriber | null = null;
  private state: BackgroundDecisionStatus = { subscribed: false, callback_verified: false, observed_publisher: '', error: '' };
  private receive: (decision: OwnerControlDecision) => Promise<void>;
  private generation: number = 0;
  constructor(receive: (decision: OwnerControlDecision) => Promise<void>) { this.receive = receive; }
  status(): BackgroundDecisionStatus {
    return { subscribed: this.state.subscribed, callback_verified: this.state.callback_verified,
      observed_publisher: this.state.observed_publisher, error: this.state.error };
  }
  async start(): Promise<void> {
    await this.stop();
    const generation: number = this.generation;
    try {
      const options: commonEventManager.CommonEventSubscribeInfo = {
        events: [CONTROL_DECISION_EVENT], publisherBundleName: CONTROL_BUNDLE
      };
      const subscriber: commonEventManager.CommonEventSubscriber = await commonEventManager.createSubscriber(options);
      if (generation !== this.generation) { await this.unsubscribe(subscriber); return; }
      this.subscriber = subscriber;
      await commonEventManager.subscribeToEvent(subscriber, (event: commonEventManager.CommonEventData): void => {
        if (generation === this.generation && this.subscriber === subscriber) {
          this.dispatch(event, generation).catch((): void => {});
        }
      });
      if (generation !== this.generation || this.subscriber !== subscriber) { await this.unsubscribe(subscriber); return; }
      this.state.subscribed = true; this.state.error = '';
    } catch (error) {
      this.state.subscribed = false; this.state.error = 'background_decision_subscription_unavailable';
      await this.stop();
    }
  }
  private async dispatch(event: commonEventManager.CommonEventData, generation: number): Promise<void> {
    if (!this.state.subscribed) { return; }
    this.state.observed_publisher = typeof event.bundleName === 'string' && /^[A-Za-z0-9._-]{1,180}$/.test(event.bundleName) ?
      event.bundleName : 'not_reported';
    if (event.bundleName !== CONTROL_BUNDLE) {
      this.state.error = event.bundleName === undefined || event.bundleName === '' ?
        'background_decision_publisher_missing' : 'background_decision_unsupported_publisher';
      return;
    }
    try {
      if (event.bundleName !== CONTROL_BUNDLE || event.event !== CONTROL_DECISION_EVENT ||
        event.parameters === undefined || typeof event.parameters[CONTROL_DECISION_PAYLOAD] !== 'string') {
        throw new Error('phone_control_untrusted_background_decision');
      }
      const raw: string = event.parameters[CONTROL_DECISION_PAYLOAD] as string;
      const decision: OwnerControlDecision = decodeBackgroundDecision(event.bundleName, event.event, raw);
      await this.receive(decision); // Runtime still checks the exact live pending tuple and Source prepared state.
      if (generation !== this.generation || !this.state.subscribed) { return; }
      this.state.callback_verified = true; this.state.error = '';
    } catch (error) { this.state.error = 'background_decision_rejected_or_stale'; }
  }
  async stop(): Promise<void> {
    this.generation++;
    this.state.subscribed = false; this.state.callback_verified = false;
    const subscriber: commonEventManager.CommonEventSubscriber | null = this.subscriber;
    this.subscriber = null;
    if (subscriber !== null) { await this.unsubscribe(subscriber); }
  }
  private async unsubscribe(subscriber: commonEventManager.CommonEventSubscriber): Promise<void> {
    await new Promise<void>((resolve: () => void): void => {
        try {
          commonEventManager.unsubscribe(subscriber, (error: BusinessError): void => {
            if (error !== undefined && error !== null) { this.state.error = 'background_decision_unsubscribe_unconfirmed'; }
            resolve();
          });
        } catch (error) { this.state.error = 'background_decision_unsubscribe_unconfirmed'; resolve(); }
    });
  }
}
