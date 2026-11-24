import { common, WantAgent, wantAgent } from '@kit.AbilityKit';
import { backgroundTaskManager } from '@kit.BackgroundTasksKit';
import { BusinessError } from '@kit.BasicServicesKit';
import { NativeScreenShare, NativeScreenShareFrame, NativeScreenShareState } from './NativeScreenShare';
import { ScreenShareScope, ScreenShareFrame, ScreenShareFrameAck, ScreenSharePolicy,
  copyScreenShareScope, sameScreenShareScope } from './ScreenSharePolicy';

// Implement only in the existing trusted node/task transport adapter. No URL,
// socket destination, credential, approval boolean or noop default is accepted.
// Scope is an INTERNAL argument, never a wire payload: approvalId/nonce stay
// local. The adapter sends only an opaque binding digest plus task/op/epoch,
// frame hash/revision; after verifying the remote ACK it constructs ack.scope
// from the captured original in-process binding, not from remote scope fields.
export interface ScreenShareFrameSink {
  isLive: (scope: ScreenShareScope) => boolean;
  onDisconnect: (listener: () => void) => () => void;
  sendFrame: (scope: ScreenShareScope, frame: ScreenShareFrame) => Promise<ScreenShareFrameAck>;
  detach: (scope: ScreenShareScope) => void;
}

export interface ScreenShareAuthorization {
  isForeground: () => boolean;
  valid: (scope: ScreenShareScope) => boolean;
  onRevoked: (listener: () => void) => () => void;
  closeHdc: (scope: ScreenShareScope) => void;
}

export interface ScreenShareSessionStatus {
  state: string;
  captureActive: boolean;
  backgroundActive: boolean;
  acknowledgedFrames: number;
  lastFrameRevision: number;
  lastCaptureTimestamp: number;
  lastAckObservedAt: number;
  error: string;
  nativeCode: number;
  cleanupUnknown: boolean;
}

function emptyStatus(): ScreenShareSessionStatus {
  return { state: 'idle', captureActive: false, backgroundActive: false, acknowledgedFrames: 0,
    lastFrameRevision: 0, lastCaptureTimestamp: -1, lastAckObservedAt: 0,
    error: '', nativeCode: 0, cleanupUnknown: false };
}

export class ScreenShareSession {
  private native: NativeScreenShare = new NativeScreenShare();
  private policy: ScreenSharePolicy | null = null;
  private sink: ScreenShareFrameSink | null = null;
  private authorization: ScreenShareAuthorization | null = null;
  private context: common.UIAbilityContext | null = null;
  private value: ScreenShareSessionStatus = emptyStatus();
  private generation: number = 0;
  private monitor: number = -1;
  private dialogTimer: number = -1;
  private leaseTimer: number = -1;
  private frameTimer: number = -1;
  private sendingFrame: boolean = false;
  private backgroundPending: Promise<void> | null = null;
  private stopping: Promise<void> | null = null;
  private unsubscribeSink: (() => void) | null = null;
  private unsubscribeAuthorization: (() => void) | null = null;
  private observingBackground: boolean = false;

  private onBackgroundCancel = (event: backgroundTaskManager.ContinuousTaskCancelInfo): void => {
    if (this.policy !== null && (event.id === this.policy.backgroundTaskId || this.backgroundPending !== null)) {
      this.stop('screen_share_continuous_task_canceled');
    }
  };

  private onBackgroundSuspend = (event: backgroundTaskManager.ContinuousTaskSuspendInfo): void => {
    if (this.policy !== null && event.suspendState && event.continuousTaskId === this.policy.backgroundTaskId) {
      this.stop('screen_share_continuous_task_suspended');
    }
  };

  status(): ScreenShareSessionStatus {
    const policy: ScreenSharePolicy | null = this.policy;
    return { state: this.value.state, captureActive: policy !== null && policy.captureActive,
      backgroundActive: policy !== null && policy.active && policy.backgroundTaskId >= 0,
      acknowledgedFrames: policy === null ? 0 : policy.acknowledgedFrames,
      lastFrameRevision: policy === null ? 0 : policy.lastAcknowledgedRevision,
      lastCaptureTimestamp: policy === null ? -1 : policy.lastCaptureTimestamp,
      lastAckObservedAt: policy === null ? 0 : policy.lastAckObservedAt,
      error: this.value.error, nativeCode: this.value.nativeCode, cleanupUnknown: this.value.cleanupUnknown };
  }

  liveFrameRevision(scope: ScreenShareScope): number {
    const policy = this.policy;
    if (policy === null || !sameScreenShareScope(policy.scope, scope) || this.value.cleanupUnknown ||
      !policy.canRemainBackground(this.authorised(policy), this.sinkLive(policy), Date.now())) { return 0; }
    return policy.lastFrameRevision;
  }

  private authorised(policy: ScreenSharePolicy): boolean {
    try { return this.authorization !== null && this.authorization.valid(copyScreenShareScope(policy.scope)); }
    catch (_) { return false; }
  }

  private sinkLive(policy: ScreenSharePolicy): boolean {
    try { return this.sink !== null && this.sink.isLive(copyScreenShareScope(policy.scope)); }
    catch (_) { return false; }
  }

  private live(generation: number): boolean {
    return generation === this.generation && this.policy !== null && this.policy.active;
  }

  // Call only from the real owner's UI gesture after the existing trusted
  // adapter validated the ORIGINAL task/operation/approval and remote receiver.
  // This requests the OS privacy dialog; it is not proof of permission or frames.
  start(context: common.UIAbilityContext, scope: ScreenShareScope,
    authorization: ScreenShareAuthorization, sink: ScreenShareFrameSink): void {
    if (this.stopping !== null || this.backgroundPending !== null || this.value.cleanupUnknown ||
      (this.policy !== null && (this.policy.active || sameScreenShareScope(this.policy.scope, scope)))) {
      throw new Error('screen_share_session_busy_or_original_scope_consumed');
    }
    const policy: ScreenSharePolicy = new ScreenSharePolicy(scope, Date.now());
    if (!authorization.isForeground() || !authorization.valid(copyScreenShareScope(policy.scope)) ||
      !sink.isLive(copyScreenShareScope(policy.scope))) {
      throw new Error('screen_share_owner_scope_and_live_receiver_required');
    }
    this.context = context; this.authorization = authorization; this.sink = sink;
    this.policy = policy; this.value = emptyStatus(); this.value.state = 'awaiting_capture_consent';
    const generation: number = ++this.generation;
    try {
      this.unsubscribeSink = sink.onDisconnect((): void => { this.stop('screen_share_sink_disconnected'); });
      if (!this.live(generation)) {
        this.unsubscribeSink(); this.unsubscribeSink = null; return;
      }
      this.unsubscribeAuthorization = authorization.onRevoked((): void => { this.stop('screen_share_scope_revoked'); });
      if (!this.live(generation)) {
        this.unsubscribeAuthorization(); this.unsubscribeAuthorization = null; return;
      }
      this.observingBackground = true;
      backgroundTaskManager.on('continuousTaskCancel', this.onBackgroundCancel);
      if (!this.live(generation)) { return; }
      backgroundTaskManager.on('continuousTaskSuspend', this.onBackgroundSuspend);
      if (!this.live(generation) || !this.authorised(policy) || !this.sinkLive(policy)) {
        this.stop('screen_share_scope_or_receiver_lost'); return;
      }
      this.monitor = setInterval((): void => { this.checkCurrentScope(); }, 1000);
      this.leaseTimer = setTimeout((): void => { this.checkCurrentScope(); },
        Math.min(2147483647, Math.max(1, policy.scope.expiresAt - Date.now())));
      this.dialogTimer = setTimeout((): void => {
        if (this.live(generation) && !policy.captureActive) { this.stop('screen_share_capture_consent_timeout'); }
      }, 60000);
      // Both callbacks are provided before NativeScreenShare starts capture.
      this.native.start({ maxWidth: 640, maxHeight: 640, maxBytes: 42000 },
        (state: NativeScreenShareState): void => { this.captureState(state, generation); },
        (frame: NativeScreenShareFrame): void => { this.frame(frame, generation); });
    } catch (error) {
      this.value.nativeCode = this.sdkCode(error as BusinessError);
      this.stop('screen_share_start_failed');
    }
  }

  private sdkCode(error: BusinessError): number {
    const code: number = error.code;
    return Number.isSafeInteger(code) && code >= -99999999 && code <= 99999999 ? code : 0;
  }

  private captureState(state: NativeScreenShareState, generation: number): void {
    if (!this.live(generation) || this.policy === null) { return; }
    if (state.state === 'started') {
      if (this.policy.captureActive) { return; }
      if (!this.authorised(this.policy) || !this.sinkLive(this.policy)) {
        this.stop('screen_share_scope_or_receiver_lost'); return;
      }
      this.policy.started(); clearTimeout(this.dialogTimer); this.dialogTimer = -1;
      this.value.state = 'capture_started_waiting_frame_ack';
      this.checkCurrentScope();
      return;
    }
    if (state.state === 'start_requested') { return; }
    // Privacy interruption/pausing is not an active sharing qualification.
    // Resume requires a new owner action and original authorized session scope.
    if (['stopped', 'canceled', 'interrupted', 'error', 'paused', 'private_scene'].includes(state.state)) {
      this.value.nativeCode = Number.isSafeInteger(state.errorCode) && Math.abs(state.errorCode) <= 99999999 ?
        state.errorCode : 0;
      this.stop(state.state === 'error' ? 'screen_share_native_error' :
        state.state === 'private_scene' ? 'screen_share_private_scene' :
        state.state === 'paused' ? 'screen_share_capture_paused' : 'screen_share_capture_stopped');
      return;
    }
    if (state.state !== 'resumed' && state.state !== 'private_scene_exit') {
      this.stop('screen_share_capture_state_unconfirmed');
    }
  }

  private checkCurrentScope(): void {
    const policy: ScreenSharePolicy | null = this.policy;
    if (policy === null || !policy.active) { return; }
    if (Date.now() >= policy.scope.expiresAt) { this.stop('screen_share_scope_expired'); return; }
    if (!this.authorised(policy)) { this.stop('screen_share_scope_revoked'); return; }
    if (!this.sinkLive(policy)) { this.stop('screen_share_sink_disconnected'); return; }
    let foreground: boolean = false;
    try { foreground = this.authorization !== null && this.authorization.isForeground(); } catch (_) {}
    if (!foreground && !policy.canRemainBackground(true, true, Date.now())) {
      this.stop('screen_share_background_ticket_required');
    }
  }

  // Bind UIAbility.onForeground/onBackground to this method. The page need not
  // remain visible once a real OS continuous-task ticket has been obtained.
  foregroundChanged(): void { this.checkCurrentScope(); }

  private frame(frame: NativeScreenShareFrame, generation: number): void {
    if (!this.live(generation)) { return; }
    this.checkCurrentScope();
    if (!this.live(generation) || this.policy === null || !this.policy.captureActive || this.sendingFrame) { return; }
    const policy: ScreenSharePolicy = this.policy;
    try { policy.acceptFrame(frame, this.authorised(policy), this.sinkLive(policy), Date.now()); }
    catch (_) { this.stop('screen_share_frame_invalid_or_scope_lost'); return; }
    const sink: ScreenShareFrameSink | null = this.sink;
    if (sink === null) { this.stop('screen_share_sink_disconnected'); return; }
    this.sendingFrame = true;
    // Bound one actual delivery/ACK; no retransmission or unbounded frame queue.
    this.frameTimer = setTimeout((): void => {
      if (this.live(generation)) { this.stop('screen_share_frame_ack_unknown'); }
    }, 10000);
    let delivery: Promise<ScreenShareFrameAck>;
    try { delivery = sink.sendFrame(copyScreenShareScope(policy.scope), frame); }
    catch (_) { this.stop('screen_share_frame_ack_unknown'); return; }
    delivery.then((ack: ScreenShareFrameAck): void => {
      if (!this.live(generation)) { return; }
      if (!this.authorised(policy) || !this.sinkLive(policy) || Date.now() >= policy.scope.expiresAt) {
        this.stop('screen_share_scope_or_receiver_lost'); return;
      }
      try { policy.acknowledge(frame, ack, Date.now()); }
      catch (_) { this.stop('screen_share_frame_ack_unconfirmed'); return; }
      this.value.state = policy.backgroundTaskId >= 0 ? 'sharing' : 'frame_acknowledged';
      if (this.backgroundPending === null && policy.canRequestBackground(true, true, Date.now())) {
        this.backgroundPending = this.startBackground(policy, generation);
        this.backgroundPending.finally((): void => { this.backgroundPending = null; });
      }
    }).catch((_error: Error): void => {
      if (this.live(generation)) { this.stop('screen_share_frame_ack_unknown'); }
    }).finally((): void => {
      // A late old session must not clear a new session's delivery state.
      if (generation === this.generation) {
        clearTimeout(this.frameTimer); this.frameTimer = -1; this.sendingFrame = false;
      }
    });
  }

  private async startBackground(policy: ScreenSharePolicy, generation: number): Promise<void> {
    const context: common.UIAbilityContext | null = this.context;
    if (context === null) { this.stop('screen_share_background_context_missing'); return; }
    try {
      const agent: WantAgent = await wantAgent.getWantAgent({
        wants: [{ bundleName: 'org.personalassistant.mesh', abilityName: 'EntryAbility' }], requestCode: 0,
        operationType: wantAgent.OperationType.START_ABILITY,
        wantAgentFlags: [wantAgent.WantAgentFlags.UPDATE_PRESENT_FLAG, wantAgent.WantAgentFlags.CONSTANT_FLAG] });
      this.checkCurrentScope();
      if (!this.live(generation) || !policy.canRequestBackground(this.authorised(policy), this.sinkLive(policy), Date.now())) {
        return;
      }
      const request: backgroundTaskManager.ContinuousTaskRequest = new backgroundTaskManager.ContinuousTaskRequest();
      request.backgroundTaskModes = [backgroundTaskManager.BackgroundTaskMode.MODE_AV_PLAYBACK_AND_RECORD];
      request.backgroundTaskSubmodes = [backgroundTaskManager.BackgroundTaskSubmode.SUBMODE_SCREEN_RECORD_NORMAL_NOTIFICATION];
      request.wantAgent = agent; request.combinedTaskNotification = false;
      if (!request.isModeSupported()) { throw new Error('screen_share_background_mode_unavailable'); }
      const ticket: backgroundTaskManager.ContinuousTaskNotification = await backgroundTaskManager.startBackgroundRunning(context, request);
      const id: number | undefined = ticket.continuousTaskId;
      if (id === undefined || !Number.isSafeInteger(id) || id < 0) {
        // Never cancel all UIAbility tasks to guess which ticket belongs to us.
        this.value.cleanupUnknown = true;
        this.stop('screen_share_background_ticket_unknown'); return;
      }
      if (!this.live(generation) || !policy.canRequestBackground(this.authorised(policy), this.sinkLive(policy), Date.now())) {
        try { await backgroundTaskManager.stopBackgroundRunning(context, id); }
        catch (_) { this.value.cleanupUnknown = true; }
        return;
      }
      policy.backgroundStarted(id);
      this.value.state = 'sharing';
      this.checkCurrentScope();
    } catch (error) {
      if (this.live(generation)) {
        this.value.nativeCode = this.sdkCode(error as BusinessError);
        this.stop('screen_share_background_start_failed');
      }
    }
  }

  stop(reason: string = 'screen_share_owner_stopped'): Promise<void> {
    if (this.stopping !== null) { return this.stopping; }
    if (this.policy === null || !this.policy.active) { return Promise.resolve(); }
    const policy: ScreenSharePolicy = this.policy;
    policy.stop(); this.generation += 1;
    this.value.state = 'stopping';
    // All reasons are generated locally; arbitrary native/transport strings
    // are never copied to state. Root may display an allowlisted translation.
    this.value.error = /^screen_share_[a-z_]{1,64}$/.test(reason) ? reason : 'screen_share_stopped';
    clearInterval(this.monitor); this.monitor = -1;
    clearTimeout(this.dialogTimer); this.dialogTimer = -1;
    clearTimeout(this.leaseTimer); this.leaseTimer = -1;
    clearTimeout(this.frameTimer); this.frameTimer = -1;
    this.sendingFrame = false;
    try { this.native.stop(); } catch (_) { this.value.cleanupUnknown = true; }
    try { if (this.unsubscribeSink !== null) { this.unsubscribeSink(); } } catch (_) {}
    try { if (this.unsubscribeAuthorization !== null) { this.unsubscribeAuthorization(); } } catch (_) {}
    this.unsubscribeSink = null; this.unsubscribeAuthorization = null;
    if (this.observingBackground) {
      try { backgroundTaskManager.off('continuousTaskCancel', this.onBackgroundCancel); } catch (_) {}
      try { backgroundTaskManager.off('continuousTaskSuspend', this.onBackgroundSuspend); } catch (_) {}
      this.observingBackground = false;
    }
    try { if (this.authorization !== null) { this.authorization.closeHdc(copyScreenShareScope(policy.scope)); } }
    catch (_) { this.value.cleanupUnknown = true; }
    try { if (this.sink !== null) { this.sink.detach(copyScreenShareScope(policy.scope)); } }
    catch (_) { this.value.cleanupUnknown = true; }
    this.sink = null;
    this.stopping = this.releaseBackground(policy);
    this.stopping.finally((): void => { this.stopping = null; });
    return this.stopping;
  }

  private async releaseBackground(policy: ScreenSharePolicy): Promise<void> {
    const pending: Promise<void> | null = this.backgroundPending;
    if (pending !== null) {
      try { await pending; } catch (_) { this.value.cleanupUnknown = true; }
    }
    const context: common.UIAbilityContext | null = this.context;
    if (policy.backgroundTaskId >= 0 && context !== null) {
      try {
        await backgroundTaskManager.stopBackgroundRunning(context, policy.backgroundTaskId);
        policy.backgroundReleased();
      } catch (_) { this.value.cleanupUnknown = true; }
    }
    this.value.state = this.value.cleanupUnknown ? 'stopped_cleanup_unknown' : 'stopped';
  }
}
